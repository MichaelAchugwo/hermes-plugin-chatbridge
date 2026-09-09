from __future__ import annotations

import os
import secrets
import shlex
import subprocess
import threading
import time
from pathlib import Path

from .session import ExecSession

MAX_EXEC_SECONDS = 300
MAX_EXEC_BYTES = 64 * 1024
SESSION_TTL_SECONDS = 3600
DEFAULT_YIELD_MS = 2000


def is_allowed(cmd: str, allowlist: list[str]) -> bool:
    """Exact match or prefix-plus-space match against allowlist entries."""
    norm = " ".join(cmd.strip().split())
    for entry in allowlist:
        want = " ".join(entry.strip().split())
        if not want:
            continue
        if norm == want or norm.startswith(want + " "):
            return True
    return False


_sessions: dict[str, ExecSession] = {}
_sessions_lock = threading.Lock()


def _reap() -> None:
    now = time.time()
    with _sessions_lock:
        dead = [sid for sid, s in _sessions.items() if now - s.started > SESSION_TTL_SECONDS]
        for sid in dead:
            sess = _sessions.pop(sid)
            try:
                if sess.proc.poll() is None:
                    sess.proc.kill()
            except OSError:
                pass


def session_count() -> int:
    _reap()
    with _sessions_lock:
        return len(_sessions)


def _spawn(cmd: str, cwd: str) -> subprocess.Popen:
    kwargs: dict = dict(cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        stdin=subprocess.PIPE)
    if os.name == "nt":
        return subprocess.Popen(cmd, shell=True, **kwargs)
    return subprocess.Popen(shlex.split(cmd), shell=False, **kwargs)


def run_exec(cmd: str, allowlist: list[str], cwd: str | None,
             yield_ms: int = DEFAULT_YIELD_MS) -> dict:
    """Run one allowlisted command. Returns fast; long-runners become a session id."""
    if not is_allowed(cmd, allowlist):
        return {"error": "COMMAND_NOT_ALLOWLISTED", "cmd": cmd[:200],
                "hint": "Add the exact command to exec_allowlist in chatbridge.yaml."}
    workdir = Path(cwd or ".")
    if not workdir.is_dir():
        return {"error": f"cwd not a dir: {cwd}"}
    _reap()
    try:
        proc = _spawn(cmd, str(workdir))
    except OSError as exc:
        return {"error": str(exc)[:200]}
    sess = ExecSession(proc, cmd)
    deadline = time.time() + max(100, min(yield_ms, MAX_EXEC_SECONDS * 1000)) / 1000.0
    while time.time() < deadline:
        if proc.poll() is not None:
            break
        time.sleep(0.05)
    text, running = sess.take_output()
    if not running:
        return {"exit_code": proc.returncode, "output": text[-MAX_EXEC_BYTES:],
                "truncated": sess.truncated}
    sid = secrets.token_hex(8)
    with _sessions_lock:
        _sessions[sid] = sess
    return {"session_id": sid, "output": text[-MAX_EXEC_BYTES:], "running": True,
            "truncated": sess.truncated,
            "hint": "Poll or write with write_stdin(session_id=...)."}


def write_stdin(session_id: str, chars: str = "", yield_ms: int = DEFAULT_YIELD_MS,
                kill: bool = False) -> dict:
    """Blank chars = poll. Non-empty = write one response, then collect. kill ends the process."""
    with _sessions_lock:
        sess = _sessions.get(session_id)
    if sess is None:
        return {"error": "unknown session_id"}
    if kill:
        try:
            sess.proc.kill()
        except OSError:
            pass
    elif chars and sess.proc.poll() is None:
        try:
            assert sess.proc.stdin is not None
            sess.proc.stdin.write((chars if chars.endswith("\n") else chars + "\n").encode("utf-8"))
            sess.proc.stdin.flush()
        except (OSError, ValueError, AttributeError):
            pass
    text, running = "", True
    deadline = time.time() + max(100, min(yield_ms, MAX_EXEC_SECONDS * 1000)) / 1000.0
    while time.time() < deadline:
        if sess.proc.poll() is not None:
            text, running = sess.take_output()
            break
        text, running = sess.take_output()
        if text:
            break
        time.sleep(0.05)
    if not running:
        # Process ended: this collection is final; the session retires.
        with _sessions_lock:
            _sessions.pop(session_id, None)
        return {"exit_code": sess.proc.returncode, "output": text[-MAX_EXEC_BYTES:],
                "running": False, "truncated": sess.truncated}
    return {"output": text[-MAX_EXEC_BYTES:], "running": running, "truncated": sess.truncated}
