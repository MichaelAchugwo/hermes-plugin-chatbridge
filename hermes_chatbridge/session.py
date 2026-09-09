from __future__ import annotations

import codecs
import os
import secrets
import shlex
import subprocess
import threading
import time
from pathlib import Path

MAX_EXEC_SECONDS = 300
MAX_EXEC_BYTES = 64 * 1024
MAX_SESSION_BYTES = 256 * 1024
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


class ExecSession:
    def __init__(self, proc: subprocess.Popen, cmd: str):
        self.proc = proc
        self.cmd = cmd[:500]
        self.started = time.time()
        self.lock = threading.Lock()
        self.parts: list[str] = []
        self.buffered_bytes = 0
        self.truncated = False
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._reader = threading.Thread(target=self._pump, name=f"chatbridge-exec-{id(self)}", daemon=True)
        self._reader.start()

    def _pump(self) -> None:
        try:
            stream = self.proc.stdout
            assert stream is not None
            read1 = getattr(stream, "read1", None)
            while True:
                # read1 returns available bytes without waiting for a full buffer;
                # plain read() on a pipe blocks until full-or-EOF and would starve polls.
                chunk = read1(65536) if read1 else stream.read(65536)
                if not chunk:
                    break
                with self.lock:
                    room = MAX_SESSION_BYTES - self.buffered_bytes
                    if room <= 0:
                        self.truncated = True
                        continue
                    piece = chunk[:room]
                    self.buffered_bytes += len(piece)
                    if len(chunk) > room:
                        self.truncated = True
                    self.parts.append(self._decoder.decode(piece, final=False))
        except (OSError, ValueError):
            pass
        finally:
            with self.lock:
                try:
                    self.parts.append(self._decoder.decode(b"", final=True))
                except (OSError, ValueError):
                    pass

    def take_output(self) -> tuple[str, bool]:
        with self.lock:
            text = "".join(self.parts)
            self.parts.clear()
        running = self.proc.poll() is None
        return text, running
