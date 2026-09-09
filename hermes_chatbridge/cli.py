from __future__ import annotations

import argparse
import json

from .config import BridgeConfig
from .exec import session_count
from .mcp_app import build_app


def register_cli(parser: argparse.ArgumentParser) -> None:
    parser.description = "ChatBridge — local MCP executor for ChatGPT web chat"
    subs = parser.add_subparsers(dest="cb_action")
    subs.add_parser("status", help="Show bridge config (no secrets)")
    srv = subs.add_parser("serve", help="Run loopback MCP bridge in foreground")
    srv.add_argument("--port", type=int, default=0)
    parser.set_defaults(func=command)


def command(args: argparse.Namespace) -> int:
    cfg = BridgeConfig.load()
    action = getattr(args, "cb_action", None) or "status"
    if action == "status":
        print(json.dumps({
            "read_only": cfg.read_only,
            "allow_exec": cfg.allow_exec,
            "allow_patch": cfg.allow_patch,
            "approved_roots": cfg.approved_roots,
            "tools": cfg.tool_names(),
            "protocol": "mcp-streamable-http",
            "exec_sessions": session_count(),
            "note": "Approve a folder in chatbridge.yaml before connecting. Exec/patch default OFF.",
        }, indent=2))
        return 0
    if action == "serve":
        import uvicorn
        app = build_app(cfg)
        # Bind loopback only; the secret path segment is the credential.
        uvicorn.run(app, host="127.0.0.1", port=args.port or cfg.port or 0,
                    log_level="warning")
        return 0
    return 2
