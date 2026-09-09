from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import Field
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from .artifact import save_artifact
from .config import BridgeConfig, _home
from .exec import run_exec
from .exec import write_stdin as _exec_write_stdin
from .patch import apply_patches, parse_patch
from .tools import record_event, search_events, tool_find, tool_read, view_image


def _instructions(cfg: BridgeConfig) -> str:
    roots = ", ".join(f"/{Path(r).name}" for r in cfg.approved_roots) or "none approved yet"
    mode = "Read only." if cfg.read_only else "Read/write for listed tools only."
    return (
        "Local coding bridge: read files in approved folders, search them without a shell, "
        "and review this bridge's own local transcript.\n"
        f"Roots: {roots}\nMode: {mode}\n"
        "Paths are virtual under the live roots above (/root/...); native absolute paths "
        "inside an approved folder are also accepted."
    )


def build_server(cfg: BridgeConfig | None = None, home: Path | None = None) -> MCPServer:
    cfg = cfg or BridgeConfig.load()
    home = home or _home()
    server = MCPServer(name="chatbridge-core", instructions=_instructions(cfg))

    @server.tool(description="Read approved files/dirs (bounded). Directories list one level.")
    def read(paths: Annotated[list[str], Field(description="Virtual (/root/...) or native paths")] ) -> dict[str, Any]:
        entry = {"tool": "read", "paths": paths}
        try:
            return tool_read(paths[:20], cfg.approved_roots)
        finally:
            record_event(home, entry)

    @server.tool(description="Filename/glob + optional text search inside approved roots. No shell.")
    def find(
        pattern: Annotated[str, Field(description="Glob like *.py")] = "*",
        text: Annotated[str, Field(description="Optional substring filter")] = "",
    ) -> dict[str, Any]:
        entry = {"tool": "find", "pattern": pattern}
        try:
            if not cfg.approved_roots:
                return {"matches": [], "error": "no approved roots"}
            return tool_find(pattern, cfg.approved_roots, text=text)
        finally:
            record_event(home, entry)

    @server.tool(description="Search this bridge's own local transcript (bounded).")
    def session(
        query: Annotated[str, Field(description="Substring filter; empty lists newest")] = "",
    ) -> dict[str, Any]:
        return search_events(home, query)

    @server.tool(name="view_image", description="View an approved image file (delivered as vision).")
    def view_image_tool(
        path: Annotated[str, Field(description="Virtual (/root/...) or native path")] = "",
    ):
        entry = {"tool": "view_image", "path": path[:300]}
        try:
            return view_image(path, cfg.approved_roots)
        finally:
            record_event(home, entry)

    if cfg.allow_save and not cfg.read_only:
        @server.tool(description="Save a ChatGPT-generated file (files.oaiusercontent.com only) into an approved folder.")
        def download_artifact(
            url: Annotated[str, Field(description="HTTPS files.oaiusercontent.com URL")] = "",
            dest: Annotated[str, Field(description="Destination virtual path; parents must exist")] = "",
        ) -> dict[str, Any]:
            live = BridgeConfig.load()
            # Never log the signed URL — only host + destination.
            entry = {"tool": "download_artifact", "dest": dest[:300]}
            try:
                if live.read_only or not live.allow_save:
                    return {"error": "TOOL_DISABLED"}
                return save_artifact(url, dest, live.approved_roots)
            finally:
                record_event(home, entry)

    if cfg.allow_patch and not cfg.read_only:
        @server.tool(description="Apply a unified diff inside approved roots (preflighted, atomic).")
        def apply_patch(patch: Annotated[str, Field(description="Unified diff text")] = "") -> dict[str, Any]:
            live = BridgeConfig.load()
            entry = {"tool": "apply_patch", "bytes": len(patch)}
            try:
                if live.read_only or not live.allow_patch:
                    return {"error": "TOOL_DISABLED"}
                try:
                    files = parse_patch(patch[:512 * 1024])
                except ValueError as exc:
                    return {"error": f"bad patch: {exc}"}
                return apply_patches(files, live.approved_roots)
            finally:
                record_event(home, entry)

    if cfg.allow_exec and not cfg.read_only:
        @server.tool(description="Run one allowlisted command in the first approved root.")
        def exec_command(cmd: Annotated[str, Field(description="Single command")] = "") -> dict[str, Any]:
            live = BridgeConfig.load()
            entry = {"tool": "exec_command", "cmd": cmd[:500]}
            try:
                if live.read_only or not live.allow_exec:
                    return {"error": "TOOL_DISABLED"}
                if not live.approved_roots:
                    return {"error": "no approved roots"}
                return run_exec(cmd[:2000], live.exec_allowlist, live.approved_roots[0])
            finally:
                record_event(home, entry)

        @server.tool(description="Poll a running command, answer its stdin prompt, or kill it.")
        def write_stdin(
            session_id: Annotated[str, Field(description="Session id from exec_command")] = "",
            chars: Annotated[str, Field(description="Input for the process; blank polls")] = "",
            kill: Annotated[bool, Field(description="Terminate the process")] = False,
        ) -> dict[str, Any]:
            live = BridgeConfig.load()
            try:
                if live.read_only or not live.allow_exec:
                    return {"error": "TOOL_DISABLED"}
                return _exec_write_stdin(session_id, chars[:8000], kill=kill)
            finally:
                record_event(home, {"tool": "write_stdin", "session": session_id[:16]})

    if cfg.agents_enabled:
        @server.tool(description="Spawn and manage worker subagents (background hermes -z runs).")
        def agents(
            action: Annotated[str, Field(description="spawn | message | status | finish")] = "status",
            worker_id: Annotated[str, Field(description="Worker id (message/status/finish)")] = "",
            text: Annotated[str, Field(description="Task (spawn) or follow-up (message)")] = "",
            kill: Annotated[bool, Field(description="finish: stop a running worker first")] = False,
        ) -> dict[str, Any]:
            from . import agents as _agents

            live = BridgeConfig.load()
            entry = {"tool": "agents", "action": action[:20], "worker": worker_id[:16]}
            try:
                if not live.agents_enabled:
                    return {"error": "TOOL_DISABLED"}
                a = (action or "status").strip().lower()
                if a == "spawn":
                    return _agents.spawn(text, live, home)
                if a == "message":
                    return _agents.message(worker_id, text, live, home)
                if a == "finish":
                    return _agents.finish(worker_id, kill=kill)
                return _agents.status(worker_id)
            finally:
                record_event(home, entry)

    return server


def is_loopback_peer(host: object) -> bool:
    """True iff *host* is a loopback address (v4 127/8 incl. mapped, or ::1)."""
    import ipaddress

    if not isinstance(host, str) or not host:
        return False
    try:
        return ipaddress.ip_address(host.strip().lower()).is_loopback
    except ValueError:
        return False


class _Guard(BaseHTTPMiddleware):
    """Loopback TCP-peer + Origin enforcement in front of the MCP app.

    The check is on the TCP peer (``request.client``), NOT the Host header:
    a tunnel (cloudflared etc.) connects from 127.0.0.1 but forwards the
    public hostname as Host, so a Host allowlist would 403 all tunneled
    traffic. The socket itself binds 127.0.0.1, so a loopback peer check is
    equivalent and tunnel-compatible. The Origin check stays as the
    DNS-rebinding/CSRF defense for browser-issued requests.
    """

    async def dispatch(self, request: Request, call_next):
        peer = request.client.host if request.client else ""
        if not is_loopback_peer(peer):
            return JSONResponse({"error": "peer not loopback"}, status_code=403)
        origin = request.headers.get("origin")
        if origin:
            from urllib.parse import urlparse
            oh = (urlparse(origin).hostname or "").lower()
            if oh not in {"127.0.0.1", "localhost", "::1"}:
                return JSONResponse({"error": "origin not loopback"}, status_code=403)
        return await call_next(request)


def build_app(cfg: BridgeConfig | None = None, home: Path | None = None) -> Starlette:
    """MCP app guarded by loopback checks and a secret path prefix.

    The token is stripped in middleware (not nested Mounts, which 307-redirect
    slash variants), so a wrong token is a plain 404 with no oracle.
    """
    cfg = cfg or BridgeConfig.load()
    # The MCP SDK's DNS-rebinding check stays ON; when a public tunnel front
    # is configured, its hostname is allowlisted explicitly. The real
    # enforcement remains the loopback TCP-peer guard + secret path above.
    security = (TransportSecuritySettings(allowed_hosts=[cfg.tunnel_host])
                if cfg.tunnel_host else None)
    app = build_server(cfg, home).streamable_http_app(
        streamable_http_path="/mcp", stateless_http=True, json_response=True,
        transport_security=security,
    )
    token = cfg.secret_token

    async def health(_request: Request) -> JSONResponse:
        return JSONResponse({"ok": True, "tools": cfg.tool_names()})

    app.routes.append(Route("/health", health))

    class _StripToken(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next):
            path = request.scope["path"]
            prefix = f"/{token}"
            if path != prefix and not path.startswith(prefix + "/"):
                return JSONResponse({"error": "not found"}, status_code=404)
            rest = path[len(prefix):] or "/"
            request.scope["path"] = rest
            raw = request.scope.get("raw_path", b"")
            try:
                suffix = raw.split(prefix.encode(), 1)[1] or b"/"
            except IndexError:
                suffix = b"/"
            request.scope["raw_path"] = suffix
            return await call_next(request)

    app.add_middleware(_StripToken)
    app.add_middleware(_Guard)
    return app
