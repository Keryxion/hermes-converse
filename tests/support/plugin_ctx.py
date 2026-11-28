"""The plugin context a test gives ``register_tools``.

Hermes hands the plugin one context object that registers tools *and* hooks, and
``voice_tools.register_tools`` uses both. A test that wants only the tools still has to accept the
hooks: a context missing ``register_hook`` makes the registration raise, and the tool the test was
about is never registered. Build the stand-in here so one plugin-side registration never has to be
mirrored into several test files.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Callable


def tool_ctx(registry: Any) -> SimpleNamespace:
    """A context registering the plugin's tools into *registry* and collecting its hooks.

    The hooks land in ``ctx.hooks``, name -> callbacks in registration order, the way Hermes keeps
    them (``hermes_cli.plugins_dispatch.track_hook`` appends, so two plugins sharing a hook name
    both run).
    """
    hooks: dict[str, list[Callable]] = {}
    return SimpleNamespace(
        register_tool=registry.register,
        register_hook=lambda name, fn, **kw: hooks.setdefault(name, []).append(fn),
        hooks=hooks)
