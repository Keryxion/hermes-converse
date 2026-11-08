"""Real OTEL SDK/exporter coverage; the plugin itself never requires the SDK."""
import concurrent.futures
from pathlib import Path
import subprocess
import sys

import pytest

from hermes_converse import tracing


@pytest.fixture
def otel(monkeypatch):
    trace = pytest.importorskip('opentelemetry.trace')
    sdk = pytest.importorskip('opentelemetry.sdk.trace')
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = sdk.TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(trace, 'get_tracer_provider', lambda: provider)
    yield trace, provider, exporter
    provider.shutdown()


def test_import_is_lazy():
    path = Path(tracing.__file__)
    result = subprocess.run([sys.executable, '-c',
        "import importlib.util, sys; "
        "s = importlib.util.spec_from_file_location('probe', sys.argv[1]); "
        "m = importlib.util.module_from_spec(s); s.loader.exec_module(m); "
        "assert not any(k.startswith('opentelemetry') for k in sys.modules)", str(path)],
        capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_missing_otel_is_inert(monkeypatch):
    monkeypatch.setitem(sys.modules, 'opentelemetry', None)
    monkeypatch.setitem(sys.modules, 'opentelemetry.context', None)
    recorder = tracing.PhaseRecorder(input_rate=16000)
    with recorder.phase('voice.capture') as phase:
        phase.set(**{'endpoint.mode': 'fixed'})
    turn = tracing.start_turn(recorder=recorder, turn_id='no-sdk')
    assert turn.traceparent is None
    turn.add_phase('voice.agent', 1, 2)
    turn.set(outcome='ok')
    turn.end()
    turn.end()
    tracing.emit_turn_trace(recorder)
    tracing.force_flush()


def test_unconfigured_provider_is_inert(monkeypatch):
    trace = pytest.importorskip('opentelemetry.trace')
    monkeypatch.setattr(trace, 'get_tracer_provider', trace.NoOpTracerProvider)
    turn = tracing.start_turn(recorder=None, turn_id='no-provider')
    assert turn.traceparent is None
    turn.end()
    tracing.force_flush()


def test_phase_timestamps_parenting_and_safe_metadata(otel, monkeypatch):
    trace, provider, exporter = otel
    clock = iter([100, 200, 300, 400, 450, 500])
    monkeypatch.setattr(tracing.time, 'time_ns', lambda: next(clock))
    recorder = tracing.PhaseRecorder(input_rate=16000, transcript='private speech')
    with recorder.phase('voice.capture', **{'endpoint.mode': 'fixed'}):
        pass
    with recorder.phase('voice.stt') as phase:
        phase.set(**{'stt.transcript_chars': 14, 'transcript': 'private speech'})
    turn = tracing.start_turn(recorder=recorder, turn_id='turn-1', **{'session.id': 'socket-1'})
    assert turn.traceparent is not None
    turn.add_phase('voice.agent', 410, 600, **{'llm.ttft_ms': 10, 'reply': 'private reply'})
    turn.add_phase('voice.tts', 420, 580, **{'tts.pcm_chunks': 3})
    turn.add_phase('voice.tts', None, 580)  # No speech produced: no invented span.
    turn.set(outcome='ok', next_action='listen', invalid=[], **{'llm.ttft_ms': float('nan')})
    turn.end()
    turn.end()  # Safe when the driver's finally block closes an already-finished turn.
    spans = {s.name: s for s in exporter.get_finished_spans()}
    assert len(exporter.get_finished_spans()) == 5
    parent = spans['voice.turn']
    assert parent.start_time == 100 and parent.end_time == 600
    assert parent.attributes['turn_id'] == 'turn-1'
    assert parent.attributes['session.id'] == 'socket-1'
    assert parent.attributes['next_action'] == 'listen'
    assert parent.instrumentation_scope.name == tracing.INSTRUMENTATION
    version, trace_id, span_id, flags = turn.traceparent.split('-')
    assert version == '00' and len(trace_id) == 32 and len(span_id) == 16 and int(flags, 16) & 1
    assert int(trace_id, 16) == parent.context.trace_id
    assert int(span_id, 16) == parent.context.span_id
    for name, start, end in [('voice.capture', 100, 200), ('voice.stt', 300, 400),
                             ('voice.agent', 410, 600), ('voice.tts', 420, 580)]:
        span = spans[name]
        assert (span.start_time, span.end_time) == (start, end)
        assert span.parent.span_id == parent.context.span_id
        assert span.context.trace_id == parent.context.trace_id
    assert 'private' not in repr([dict(s.attributes) for s in spans.values()])
    assert 'llm.ttft_ms' not in parent.attributes
    assert trace.get_current_span().get_span_context().is_valid is False


def test_recorder_carries_worker_context_without_polluting_driver(otel):
    trace, provider, exporter = otel
    host = provider.get_tracer('host')
    capture_parent = host.start_span('capture-parent')

    def capture():
        with trace.use_span(capture_parent, end_on_exit=False):
            recorder = tracing.PhaseRecorder()
            with recorder.phase('voice.capture'):
                pass
            return recorder

    with concurrent.futures.ThreadPoolExecutor(1) as pool:
        recorder = pool.submit(capture).result()
    with host.start_as_current_span('unrelated-driver') as unrelated:
        turn = tracing.start_turn(recorder=recorder, turn_id='worker')
        assert trace.get_current_span() is unrelated
        turn.end()
    capture_parent.end()
    parent = next(s for s in exporter.get_finished_spans() if s.name == 'voice.turn')
    assert parent.parent.span_id == capture_parent.get_span_context().span_id


def test_unsampled_context_propagates_without_export(otel, monkeypatch):
    trace, _, exporter = otel
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.sampling import ALWAYS_OFF
    provider = TracerProvider(sampler=ALWAYS_OFF)
    monkeypatch.setattr(trace, 'get_tracer_provider', lambda: provider)
    try:
        turn = tracing.start_turn(recorder=None, turn_id='unsampled')
        assert not int(turn.traceparent.rsplit('-', 1)[1], 16) & 1
        turn.end()
        assert not exporter.get_finished_spans()
    finally:
        provider.shutdown()


def test_discarded_capture_and_phase_errors_export_without_exception_text(otel):
    _, _, exporter = otel
    recorder = tracing.PhaseRecorder(outcome='discarded_after_signoff')
    with pytest.raises(ValueError):
        with recorder.phase('voice.stt'):
            raise ValueError('sensitive exception text')
    tracing.emit_turn_trace(recorder)
    spans = exporter.get_finished_spans()
    assert {s.name for s in spans} == {'voice.turn', 'voice.stt'}
    assert next(s for s in spans if s.name == 'voice.turn').attributes['outcome'] == 'discarded_after_signoff'
    stt = next(s for s in spans if s.name == 'voice.stt')
    assert stt.status.status_code.name == 'ERROR'
    assert not stt.events and stt.status.description is None


def test_flush_is_bounded_and_does_not_shutdown_host(otel, monkeypatch):
    _, provider, _ = otel
    seen = []
    monkeypatch.setattr(provider, 'force_flush', lambda **kw: seen.append(kw))
    monkeypatch.setattr(provider, 'shutdown', lambda: pytest.fail('must not own host shutdown'))
    tracing.force_flush(123)
    assert seen == [{'timeout_millis': 123}]
    monkeypatch.undo()  # Restore the fixture's shutdown method before teardown.


def test_broken_provider_cannot_break_voice(otel, monkeypatch):
    trace, provider, exporter = otel
    turn = tracing.start_turn(recorder=None, turn_id='broken')
    monkeypatch.setattr(turn._span, 'set_attributes', lambda *a: (_ for _ in ()).throw(RuntimeError()))
    turn.end()  # Even attribute failures close the parent.
    assert len(exporter.get_finished_spans()) == 1
    monkeypatch.setattr(trace, 'get_tracer', lambda *a: (_ for _ in ()).throw(RuntimeError()))
    assert tracing.start_turn(recorder=None, turn_id='still-works').traceparent is None
    monkeypatch.setattr(provider, 'force_flush', lambda **kw: (_ for _ in ()).throw(RuntimeError()))
    tracing.force_flush()
