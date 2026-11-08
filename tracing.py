"""Optional voice spans using the host's OpenTelemetry configuration.

Capture/STT timings travel with their utterance across the worker/driver boundary.
The driver opens ``voice.turn`` at capture start, exposes its W3C traceparent,
then materializes phase spans with their original epoch-nanosecond timestamps.
A configured global provider takes priority. Otherwise the host's opted-in monitoring
OTLP destination gets a private voice provider; the global provider is never replaced.
Missing OTEL, disabled export, and telemetry failures leave voice working.
"""
from __future__ import annotations

import logging
import math
import os
import threading
import time
from contextlib import contextmanager
from typing import Any, Iterator, Optional

logger = logging.getLogger(__name__)
INSTRUMENTATION = "hermes.voice.converse"
_providers: dict[tuple, Any] = {}
_provider_lock = threading.Lock()
# Explicit metadata only: never export audio, transcripts, reply text, or exception messages.
_ATTRIBUTE_KEYS = frozenset({
    "turn_id", "session.id", "input_rate", "session_mode", "outcome", "interrupted", "next_action",
    "vad.model", "endpoint.mode", "endpoint.probability", "endpoint.threshold", "endpoint.decision", "endpoint.reason",
    "endpoint.holds", "stt.success", "stt.model", "stt.transcript_chars", "llm.ttft_ms", "reply.chars",
    "input.audio_ms", "endpoint.silence_ms",
    "llm.deltas", "error", "llm.timed_out", "tts.pcm_chunks", "tts.first_pcm_ms",
})


def _monitoring_provider() -> Any:
    """Reuse the host's explicit OTLP opt-in without depending on its private provider.

    Imports stay lazy and never install packages or write configuration. Exporter secrets
    come from the same headers_env mapping as Hermes monitoring, and are never logged.
    """
    from hermes_cli.config import load_config_readonly
    config = load_config_readonly()
    otlp = ((config.get("monitoring") or {}).get("export") or {}).get("otlp") or {}
    if otlp.get("enabled") is not True or not otlp.get("endpoint"):
        return None
    headers = {str(name): os.environ[env] for name, env in (otlp.get("headers_env") or {}).items()
               if isinstance(env, str) and os.environ.get(env)}
    key = (str(otlp["endpoint"]), tuple(sorted(headers.items())))
    with _provider_lock:
        if key not in _providers:
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
            exporter = OTLPSpanExporter(endpoint=key[0], headers=headers or None)
            provider = TracerProvider(resource=Resource.create({
                "service.name": "hermes-gateway", "telemetry.scope": "voice",
            }))
            provider.add_span_processor(BatchSpanProcessor(exporter))
            _providers[key] = provider
        return _providers[key]


def _attributes(attrs: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in attrs.items()
            if key in _ATTRIBUTE_KEYS and isinstance(value, (str, bool, int, float))
            and (not isinstance(value, float) or math.isfinite(value))}


def _current_context() -> Any:
    try:
        from opentelemetry.context import get_current
        return get_current()
    except Exception:
        return None


class _Phase:
    def __init__(self, name: str, start_ns: int, **attrs: Any) -> None:
        self.name, self.start_ns = name, start_ns
        self.end_ns = start_ns
        self.attrs = _attributes(attrs)
        self.events: list[tuple[str, int, dict[str, Any]]] = []

    def set(self, **attrs: Any) -> None:
        self.attrs.update(_attributes(attrs))

    def event(self, name: str, **attrs: Any) -> None:
        self.events.append((name, time.time_ns(), _attributes(attrs)))


class PhaseRecorder:
    """One utterance's timings; only the capturing worker mutates it before handoff."""

    def __init__(self, **turn_attrs: Any) -> None:
        self.attrs = _attributes(turn_attrs)
        self.phases: list[_Phase] = []
        self.parent_context = _current_context()

    def set(self, **attrs: Any) -> None:
        self.attrs.update(_attributes(attrs))

    @contextmanager
    def phase(self, name: str, **attrs: Any) -> Iterator[_Phase]:
        phase = _Phase(name, time.time_ns(), **attrs)
        try:
            yield phase
        except BaseException:
            phase.set(error=True)
            raise
        finally:
            phase.end_ns = max(phase.start_ns, time.time_ns())
            self.phases.append(phase)


class TurnTrace:
    """An explicit parent span, never attached as ambient context on the event loop."""

    def __init__(self, recorder: Optional[PhaseRecorder] = None, *, tracer: Any = None,
                 span: Any = None, api: Any = None) -> None:
        self._recorder = recorder if recorder is not None else PhaseRecorder()
        self._tracer, self._span, self._api = tracer, span, api
        self._ended = False
        self.traceparent: Optional[str] = None
        if span is not None:
            try:
                ctx = span.get_span_context()
                if ctx.is_valid:
                    self.traceparent = f"00-{ctx.trace_id:032x}-{ctx.span_id:016x}-{int(ctx.trace_flags):02x}"
            except Exception:
                logger.debug("voice trace context unavailable", exc_info=True)

    def add_phase(self, name: str, start_ns: Optional[int], end_ns: Optional[int], **attrs: Any) -> None:
        if self._ended or start_ns is None or end_ns is None:
            return
        phase = _Phase(name, start_ns, **attrs)
        phase.end_ns = max(start_ns, end_ns)
        self._recorder.phases.append(phase)

    def set(self, **attrs: Any) -> None:
        if not self._ended:
            self._recorder.set(**attrs)

    def end(self, end_ns: Optional[int] = None) -> None:
        if self._ended:
            return
        self._ended = True
        if self._span is None:
            return
        try:
            self._span.set_attributes(self._recorder.attrs)
            if self._recorder.attrs.get("outcome") in ("error", "timeout"):
                self._span.set_status(self._api.StatusCode.ERROR)
            context = self._api.set_span_in_context(self._span)
            for phase in self._recorder.phases:
                self._emit_phase(phase, context)
        except Exception:
            logger.debug("voice trace emission failed", exc_info=True)
        finally:
            try:
                # Include driver/queue time as well as capture, STT, agent and overlapping TTS.
                end_ns = max([end_ns or time.time_ns(), *(p.end_ns for p in self._recorder.phases)])
                self._span.end(end_time=end_ns)
            except Exception:
                logger.debug("voice trace end failed", exc_info=True)

    def _emit_phase(self, phase: _Phase, context: Any) -> None:
        child = None
        try:
            child = self._tracer.start_span(phase.name, context=context, start_time=phase.start_ns,
                                            attributes=phase.attrs)
            if phase.attrs.get("error"):
                child.set_status(self._api.StatusCode.ERROR)
            for name, timestamp, attrs in phase.events:
                child.add_event(name, attributes=attrs, timestamp=timestamp)
        except Exception:
            logger.debug("voice phase emission failed", exc_info=True)
        finally:
            if child is not None:
                try:
                    child.end(end_time=phase.end_ns)
                except Exception:
                    logger.debug("voice phase end failed", exc_info=True)


def start_turn(*, recorder: Optional[PhaseRecorder], turn_id: str, **attrs: Any) -> TurnTrace:
    """Open a live parent; try again on later turns if optional tracing is unavailable."""
    recorder = recorder if recorder is not None else PhaseRecorder()
    recorder.set(turn_id=turn_id, **attrs)
    span = None
    try:
        from opentelemetry import trace

        provider = trace.get_tracer_provider()
        fallback = (_monitoring_provider()
                    if isinstance(provider, (trace.ProxyTracerProvider, trace.NoOpTracerProvider)) else None)
        tracer = fallback.get_tracer(INSTRUMENTATION) if fallback is not None else trace.get_tracer(INSTRUMENTATION)
        start_ns = min((p.start_ns for p in recorder.phases), default=time.time_ns())
        span = tracer.start_span("voice.turn", context=recorder.parent_context,
                                 kind=trace.SpanKind.SERVER, start_time=start_ns, attributes=recorder.attrs)
        if span.get_span_context().is_valid:
            return TurnTrace(recorder, tracer=tracer, span=span, api=trace)
    except Exception:
        logger.debug("voice tracing unavailable", exc_info=True)
    if span is not None:
        try:
            span.end()
        except Exception:
            pass
    return TurnTrace(recorder)


def emit_turn_trace(recorder: Optional[PhaseRecorder]) -> None:
    """Emit capture/STT metadata for an utterance that never reached an agent turn."""
    if recorder is not None and recorder.phases:
        turn = start_turn(recorder=recorder, turn_id="")
        turn.end(end_ns=max(p.end_ns for p in recorder.phases))


def force_flush(timeout_millis: int = 5000) -> None:
    """Best-effort bounded flush; never shut down a shared provider."""
    try:
        from opentelemetry import trace
        with _provider_lock:
            providers = [trace.get_tracer_provider(), *_providers.values()]
        deadline = time.monotonic() + timeout_millis / 1000
        for provider in providers:
            flush = getattr(provider, "force_flush", None)
            remaining = max(0, math.ceil((deadline - time.monotonic()) * 1000))
            if flush is not None and remaining:
                flush(timeout_millis=remaining)
    except Exception:
        logger.debug("voice tracing flush failed", exc_info=True)
