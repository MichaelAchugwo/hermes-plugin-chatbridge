from __future__ import annotations

try:
    from .hermes_chatbridge.cli import command, register_cli
except ImportError:
    from hermes_chatbridge.cli import command, register_cli


def register(ctx) -> None:
    ctx.register_cli_command(
        name="chatbridge",
        help="Hermes-native local MCP workbench",
        setup_fn=register_cli,
        handler_fn=command,
        description="Loopback-local executor (approved roots, read-only default) managed by Hermes.",
    )
