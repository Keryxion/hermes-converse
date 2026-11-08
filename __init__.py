"""hermes-converse: server-owned realtime voice over one WebSocket on the Hermes API server.

``register()`` wires the final-response observer, the ``GET /v1/audio/converse`` route on the
``api_server`` platform (through ``ctx.register_platform_handler``) and the ``hermes converse``
CLI tree. Everything heavy (numpy, PyAV, the speech providers) is imported per connection, so
loading the plugin costs nothing until a client connects.
"""

from __future__ import annotations

__version__ = "0.1.0"


def register(ctx) -> None:
    from .cli import run_cli, setup_cli
    from .conversation import ConversationHub
    from .route import make_platform_factory

    hub = ConversationHub()
    ctx.register_hook("post_llm_call", hub.on_response)
    ctx.register_platform_handler("api_server", make_platform_factory(ctx, hub=hub))
    ctx.register_cli_command(
        name="converse",
        help="Realtime voice endpoint (/v1/audio/converse) tools",
        setup_fn=setup_cli,
        handler_fn=run_cli,
        description=("Checks the realtime voice endpoint's prerequisites (audio libraries, STT/TTS "
                     "configuration, the API server) and prints the WebSocket URL a client dials."),
    )
