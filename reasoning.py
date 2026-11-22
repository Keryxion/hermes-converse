"""The effort levels the selected provider actually advertises for a model."""
from __future__ import annotations


def capability(model: str | None, provider: str | None) -> tuple[str, list[str]]:
    if not model or not provider:
        return "unknown", []
    try:
        if provider == "openai-codex":
            from agent.reasoning_effort import codex_supported_efforts
            return "supported", list(codex_supported_efforts(model))
        from hermes_cli.models_reasoning_caps import (
            nous_model_reasoning_capabilities, openrouter_model_reasoning_capabilities,
            warm_nous_reasoning_caps_async, warm_openrouter_reasoning_caps_async,
        )
        from hermes_constants import parse_reasoning_effort
    except ImportError:
        return "unknown", []
    readers = {"openrouter": (warm_openrouter_reasoning_caps_async, openrouter_model_reasoning_capabilities),
               "nous": (warm_nous_reasoning_caps_async, nous_model_reasoning_capabilities)}
    if provider not in readers:
        return "unknown", []
    warm, lookup = readers[provider]
    try:
        warm()
        caps = lookup(model)
    except (OSError, ValueError, RuntimeError):
        return "unknown", []
    if not caps or caps.get("supports_reasoning") is None:
        return "unknown", []
    if caps["supports_reasoning"] is False:
        return "unsupported", []
    levels = [level for level in (caps.get("supported_efforts") or [])
              if isinstance(level, str) and parse_reasoning_effort(level) is not None
              and not (caps.get("mandatory") and level == "none")]
    return ("supported", levels) if levels else ("unknown", [])


def level(config):
    if not isinstance(config, dict):
        return None
    return "none" if config.get("enabled") is False else config.get("effort")
