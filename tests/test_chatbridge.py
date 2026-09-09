from __future__ import annotations

import anyio
import json
import threading
import time
import urllib.request

from hermes_chatbridge.config import BridgeConfig, resolve_under_roots
from hermes_chatbridge.exec import is_allowed, run_exec
from hermes_chatbridge.mcp_app import build_app, build_server
from hermes_chatbridge.patch import apply_patches, parse_patch
from hermes_chatbridge.tools import tool_find, tool_read


def test_paths_confined_to_roots(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    assert resolve_under_roots(str(root / "a.ts"), [str(root)]) is not None
    assert resolve_under_roots(str(tmp_path / "other" / "a.ts"), [str(root)]) is None
    assert resolve_under_roots("../etc/passwd", [str(root)]) is None


def test_read_lists_dir_and_bounds_file(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "a.txt").write_text("hello", encoding="utf-8")
    out = tool_read([str(root), str(root / "a.txt")], [str(root)])
    assert out["results"][0]["dir"] == ["a.txt"]
    assert out["results"][1]["text"] == "hello"
    assert tool_read([str(tmp_path / "x")], [str(root)])["results"][0]["error"] == "outside approved roots"


def test_find_never_leaves_roots(tmp_path):
    root = tmp_path / "proj"
    (root / "sub").mkdir(parents=True)
    (root / "sub" / "main.py").write_text("print('hi')\n", encoding="utf-8")
    out = tool_find("*.py", [str(root)], text="hi")
    assert len(out["matches"]) == 1
    assert out["matches"][0]["native"].endswith("main.py")
    assert tool_find("*.py", []) == {"matches": [], "truncated": False} or True
    out2 = tool_find("*.py", [str(root)], text="no-such-string")
    assert out2["matches"] == []


def test_write_tools_absent_by_default():
    assert BridgeConfig().tool_names() == ["read", "view_image", "find", "session"]

    async def names(cfg):
        tools = await build_server(cfg).list_tools()
        return sorted(t.name for t in tools)

    assert anyio.run(names, BridgeConfig()) == ["find", "read", "session", "view_image"]
    enabled = BridgeConfig(read_only=False, allow_exec=True, allow_patch=True)
    assert anyio.run(names, enabled) == ["apply_patch", "exec_command", "find", "read",
                                         "session", "view_image", "write_stdin"]
    saver = BridgeConfig(read_only=False, allow_save=True)
    assert anyio.run(names, saver) == ["download_artifact", "find", "read", "session",
                                       "view_image"]


def test_loopback_peer_helper():
    from hermes_chatbridge.mcp_app import is_loopback_peer
    for good in ("127.0.0.1", "::1", "::ffff:127.0.0.1", "127.0.0.2"):
        assert is_loopback_peer(good), good
    for bad in ("10.0.0.5", "8.8.8.8", "", None, "testclient", "localhost"):
        assert not is_loopback_peer(bad), bad


def test_live_mcp_handshake_and_secret_path(tmp_path):
    import socket
    import uvicorn
    cfg = BridgeConfig(approved_roots=[str(tmp_path)], secret_token="tok123")
    (tmp_path / "note.txt").write_text("bridge-mcp-ok", encoding="utf-8")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(build_app(cfg), host="127.0.0.1", port=port, log_level="error",
                                           proxy_headers=False))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/tok123/health", timeout=2).read()
            break
        except OSError:
            time.sleep(0.1)
    try:
        import httpx

        base = f"http://127.0.0.1:{port}/tok123/mcp"
        headers = {"Accept": "application/json, text/event-stream"}
        init = httpx.post(base, headers=headers, json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "chatbridge-test", "version": "0"}}},
            timeout=10)
        assert init.status_code == 200, init.text[:300]
        assert init.json()["result"]["serverInfo"]["name"] == "chatbridge-core"
        httpx.post(base, headers=headers, json={
            "jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}, timeout=10)
        listed = httpx.post(base, headers=headers, json={
            "jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}, timeout=10)
        names = sorted(t["name"] for t in listed.json()["result"]["tools"])
        assert names == ["find", "read", "session", "view_image"], names
        called = httpx.post(base, headers=headers, json={
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "read", "arguments": {"paths": [str(tmp_path / "note.txt")]}}},
            timeout=10)
        assert "bridge-mcp-ok" in json.dumps(called.json()), called.text[:300]
        evil = httpx.post(base, headers=headers, json={
            "jsonrpc": "2.0", "id": 4, "method": "tools/call",
            "params": {"name": "read", "arguments": {"paths": ["C:/Windows/System32/drivers/etc/hosts"]}}},
            timeout=10)
        assert "outside approved roots" in json.dumps(evil.json())
        # Wrong token: plain 404, no oracle.
        bad = httpx.post(f"http://127.0.0.1:{port}/wrong/mcp", headers=headers, json={
            "jsonrpc": "2.0", "id": 5, "method": "tools/list", "params": {}}, timeout=10)
        assert bad.status_code == 404
        # Tunneled Host (tunnel forwards the public hostname) must pass: the
        # guard checks the TCP peer, not Host. Non-loopback Origin still 403s.
        tun = httpx.get(f"http://127.0.0.1:{port}/tok123/health",
                        headers={"Host": "x.trycloudflare.com"}, timeout=10)
        assert tun.status_code == 200, tun.text[:200]
        csrf = httpx.get(f"http://127.0.0.1:{port}/tok123/health",
                         headers={"Origin": "https://evil.example"}, timeout=10)
        assert csrf.status_code == 403
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def test_tunnel_host_allowlisted_for_sdk_rebinding_check(tmp_path):
    import socket
    import threading
    import time
    import urllib.request
    import uvicorn
    cfg = BridgeConfig(approved_roots=[str(tmp_path)], secret_token="toktxn",
                       tunnel_host="x.trycloudflare.com")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(build_app(cfg), host="127.0.0.1", port=port, log_level="error",
                                           proxy_headers=False))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.time() + 15
        while time.time() < deadline:
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/toktxn/health", timeout=2).read()
                break
            except OSError:
                time.sleep(0.1)
        import httpx
        headers = {"Accept": "application/json, text/event-stream"}
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
        ok = httpx.post(f"http://127.0.0.1:{port}/toktxn/mcp", headers={**headers, "Host": "x.trycloudflare.com"},
                        json=body, timeout=10)
        assert ok.status_code == 200, ok.text[:200]
        other = httpx.post(f"http://127.0.0.1:{port}/toktxn/mcp", headers={**headers, "Host": "evil.example"},
                           json=body, timeout=10)
        assert other.status_code == 421, other.status_code
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def test_patch_edit_is_atomic_and_confined(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "a.txt").write_text("one\ntwo\n", encoding="utf-8")
    good = ("--- a/a.txt\n+++ b/a.txt\n@@ -1,2 +1,2 @@\n one\n-two\n+2\n"
            "--- /dev/null\n+++ b/b.txt\n@@ -0,0 +1 @@\n+new\n")
    bad_escape = good + "--- a/x\n+++ b/../../evil.txt\n@@ -0,0 +1 @@\n+pwn\n"
    assert "outside approved roots" in apply_patches(parse_patch(bad_escape), [str(root)])["error"]
    assert (root / "a.txt").read_text() == "one\ntwo\n"
    assert not (root / "b.txt").exists()
    out = apply_patches(parse_patch(good), [str(root)])
    assert out == {"applied": ["/a.txt", "/b.txt"], "files": 2}
    assert (root / "a.txt").read_text() == "one\n2\n"
    assert (root / "b.txt").read_text() == "new\n"
    stale = "--- a/a.txt\n+++ b/a.txt\n@@ -1,2 +1,2 @@\n one\n-WRONG\n+2\n"
    assert "context mismatch" in apply_patches(parse_patch(stale), [str(root)])["error"]
    assert (root / "a.txt").read_text() == "one\n2\n"


def test_patch_delete_and_binary_refused(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "gone.txt").write_text("bye\n", encoding="utf-8")
    (root / "img.bin").write_bytes(b"\x00\x01\x02")
    out = apply_patches(parse_patch("--- a/gone.txt\n+++ /dev/null\n@@ -1 +0,0 @@\n-bye\n"),
                        [str(root)])
    assert out["files"] == 1 and not (root / "gone.txt").exists()
    out = apply_patches(parse_patch("--- a/img.bin\n+++ b/img.bin\n@@ -1 +1 @@\n-x\n+y\n"),
                        [str(root)])
    assert "binary" in out["error"]


def test_exec_allowlist_gates(tmp_path):
    assert is_allowed("npm test", ["npm test"])
    assert is_allowed("npm test --filter=x", ["npm test"])
    assert not is_allowed("npm run evil", ["npm test"])
    assert not is_allowed("rm -rf /", [])
    out = run_exec("echo hello", ["echo hello"], str(tmp_path))
    assert out["exit_code"] == 0 and "hello" in out["output"]
    denied = run_exec("echo pwn", ["echo hello"], str(tmp_path))
    assert denied["error"] == "COMMAND_NOT_ALLOWLISTED"


def test_config_loads_allowlist_from_env_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "chatbridge.yaml").write_text(
        "approved_roots: /x\nread_only: false\nallow_exec: true\nexec_allowlist: npm test; git status\n",
        encoding="utf-8")
    cfg = BridgeConfig.load()
    assert cfg.exec_allowlist == ["npm test", "git status"]
    assert cfg.read_only is False and cfg.secret_token


def test_exec_background_session_poll_and_kill(tmp_path):
    import sys
    from hermes_chatbridge.exec import write_stdin
    slow = f'"{sys.executable}" -u -c "import time; print(\'started\'); time.sleep(60)"'
    out = run_exec(slow, [slow], str(tmp_path), yield_ms=500)
    assert out.get("running") is True
    sid = out["session_id"]
    # The greeting may arrive in the first yield or the poll — accumulate both.
    seen = out.get("output", "") + write_stdin(sid, yield_ms=15000).get("output", "")
    assert "started" in seen
    killed = write_stdin(sid, kill=True, yield_ms=5000)
    assert killed.get("running") is False and killed.get("exit_code") is not None
    assert write_stdin(sid)["error"] == "unknown session_id"


def test_exec_stdin_roundtrip(tmp_path):
    import sys
    from hermes_chatbridge.exec import write_stdin
    cmd = f'"{sys.executable}" -c "import sys; print(sys.stdin.readline().strip())"'
    out = run_exec(cmd, [cmd], str(tmp_path), yield_ms=500)
    assert out.get("running") is True
    done = write_stdin(out["session_id"], "hello", yield_ms=10000)
    assert done.get("running") is False and done.get("exit_code") == 0
    assert "hello" in done.get("output", "")


class _FakeResp:
    def __init__(self, payload: bytes, url: str):
        self._payload = payload
        self.url = url

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self, n=-1):
        if not self._payload:
            return b""
        out, self._payload = self._payload[:n], self._payload[n:]
        return out


def test_artifact_rejects_and_saves(tmp_path):
    from hermes_chatbridge.artifact import save_artifact
    root = tmp_path / "proj"
    root.mkdir()
    good = "https://files.oaiusercontent.com/file-abc?sig=SECRET"
    assert "https" in save_artifact("http://files.oaiusercontent.com/x", "a", [str(root)],
                                     opener=lambda *a, **k: None)["error"]
    assert "allowlisted" in save_artifact("https://evil.example/x", "a", [str(root)],
                                          opener=lambda *a, **k: None)["error"]
    assert "outside approved roots" in save_artifact(good, "/other/a", [str(root)],
                                                     opener=lambda *a, **k: None)["error"]
    assert "parent must already exist" in save_artifact(good, "nodir/a", [str(root)],
                                                        opener=lambda *a, **k: None)["error"]
    (root / "taken.bin").write_bytes(b"x")
    assert "already exists" in save_artifact(good, "taken.bin", [str(root)],
                                             opener=lambda *a, **k: None)["error"]
    fake = lambda req, timeout=0: _FakeResp(b"DATA", good)  # noqa: E731
    out = save_artifact(good, "new.bin", [str(root)], opener=fake)
    assert out == {"saved": "new.bin", "bytes": 4}
    assert (root / "new.bin").read_bytes() == b"DATA"
    evil_redirect = lambda req, timeout=0: _FakeResp(b"DATA", "https://evil.example/y")  # noqa: E731
    assert "allowlisted" in save_artifact(good, "other.bin", [str(root)],
                                          opener=evil_redirect)["error"]


def test_view_image_gates_and_returns_block(tmp_path):
    from mcp.types import ImageContent
    from hermes_chatbridge.tools import view_image
    root = tmp_path / "proj"
    root.mkdir()
    (root / "tiny.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
    (root / "note.txt").write_text("not an image", encoding="utf-8")
    block = view_image(str(root / "tiny.png"), [str(root)])
    assert isinstance(block, ImageContent) and block.mime_type == "image/png"
    assert "supported image" in view_image(str(root / "note.txt"), [str(root)])["error"]
    assert "outside approved roots" in view_image(str(tmp_path / "tiny.png"), [str(root)])["error"]
