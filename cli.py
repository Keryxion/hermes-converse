"""``hermes converse doctor`` — checks the endpoint's prerequisites and prints the URL to dial."""

from __future__ import annotations

import importlib.util
from typing import Any


def setup_cli(subparser: Any) -> None:
    subs = subparser.add_subparsers(dest="converse_command")
    subs.add_parser("doctor", help="Check the realtime voice endpoint's prerequisites")


def run_cli(args: Any) -> int:
    if getattr(args, "converse_command", None) == "doctor":
        return doctor()
    print("Usage: hermes converse doctor")
    return 2


def _ok(label: str, detail: str = "") -> None:
    print(f"  ✓ {label}" + (f" — {detail}" if detail else ""))


def _bad(label: str, detail: str = "") -> None:
    print(f"  ✗ {label}" + (f" — {detail}" if detail else ""))


def doctor() -> int:
    """Audio libraries, STT/TTS configuration, the API server platform, Smart Turn deps."""
    failures = 0
    print("hermes-converse doctor")
    for module, why in (("numpy", "VAD and capture"), ("av", "PCM resampling / one-shot TTS transcode"),
                        ("aiohttp", "the API server and the loopback client")):
        if importlib.util.find_spec(module) is None:
            _bad(module, f"not importable ({why})")
            failures += 1
        else:
            _ok(module)

    try:
        from hermes_cli.config import load_config

        cfg = load_config() or {}
    except Exception as exc:
        _bad("config", f"could not load config.yaml ({type(exc).__name__})")
        return 1
    stt = cfg.get("stt") or {}
    tts = cfg.get("tts") or {}
    _ok("STT provider", str(stt.get("provider") or "(default)"))
    _ok("TTS provider", str(tts.get("provider") or "(default: edge, one-shot path)"))

    try:
        from gateway.config import Platform, load_gateway_config

        gw = load_gateway_config()
        api = gw.platforms.get(Platform.API_SERVER)
        enabled = bool(api is not None and api.enabled)
        extra = (api.extra if api is not None else {}) or {}
        host, port = extra.get("host", "127.0.0.1"), extra.get("port", 8642)
    except Exception as exc:
        _bad("api_server platform", f"could not read gateway config ({type(exc).__name__})")
        return 1
    if enabled:
        _ok("api_server platform", "enabled")
    else:
        _bad("api_server platform", "not enabled (hermes gateway setup → api_server); the route needs it")
        failures += 1
    try:
        from agent.secret_scope import get_secret

        has_key = bool(get_secret("API_SERVER_KEY", ""))
    except Exception:
        has_key = False
    if has_key:
        _ok("API_SERVER_KEY", "set (clients present it as start.key or the hermes-key. subprotocol)")
    else:
        _bad("API_SERVER_KEY", "not set; the API server refuses to start without one")
        failures += 1

    from .settings import load_settings

    settings = load_settings(lambda key, default=None: _plugin_setting(cfg, key, default))
    if settings.vad_model == "silero":
        from .silero_vad import load_silero_session

        session, reason = load_silero_session()
        if session is None:
            _bad("VAD", f"Silero unavailable ({reason}); using energy. Check ONNX Runtime and model cache/network, "
                 "or set vad_model: energy.")
            failures += 1
        else:
            _ok("VAD", "Silero (CPU); energy fallback on inference failure")
    else:
        _ok("VAD", "energy (configured)")
    if settings.endpoint_model:
        from .turn_detector import load_turn_detector, missing_modules

        missing = missing_modules()
        if missing:
            _bad("Smart Turn endpointer", "enabled but missing: " + ", ".join(missing)
                 + "; using fixed silence. Reinstall/update hermes-converse through the plugin manager "
                 "to install its declared dependencies, then restart the gateway. "
                 "ONNX Runtime has no current Intel macOS or Android wheels; use the fixed endpoint there.")
            failures += 1
        elif load_turn_detector(settings.endpoint_threshold) is None:
            _bad("Smart Turn endpointer", "dependencies found but model could not load; using fixed silence. "
                 "Check the smart-turn warning above (imports, ONNX runtime, or model download/cache). "
                 "First use needs access to Hugging Face; offline use needs cached weights.")
            failures += 1
        else:
            _ok("Smart Turn endpointer", f"enabled (threshold {settings.endpoint_threshold:g})")
    else:
        _ok("endpointer", "fixed silence (set endpoint_model to enable Smart Turn)")
    print(f"\n  WebSocket URL: ws://{host}:{port}/v1/audio/converse")
    print("  First frame:   {\"type\":\"start\",\"key\":\"<API_SERVER_KEY>\",\"input_rate\":16000,\"output_rate\":24000}")
    return 1 if failures else 0


def _plugin_setting(cfg: dict, key: str, default: Any) -> Any:
    entries = ((cfg.get("plugins") or {}).get("entries") or {})
    entry = entries.get("hermes-converse") or {}
    settings = entry.get("settings") or {}
    return settings.get(key, default)
