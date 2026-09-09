from __future__ import annotations

import fnmatch
import json
import os
import time
from pathlib import Path
from typing import Any

from .config import resolve_virtual

MAX_FILE_BYTES = 256 * 1024
MAX_TOTAL_BYTES = 512 * 1024
MAX_FIND_MATCHES = 50
FIND_EXCLUDES = {".git", "node_modules", "__pycache__", ".venv", "dist", "build"}


def tool_read(paths: list[str], roots: list[str]) -> dict:
    """Read approved paths: files whole (bounded), directories one level."""
    out: list[dict] = []
    total = 0
    for p in paths:
        real = resolve_virtual(p, roots)
        if real is None:
            out.append({"path": p, "error": "outside approved roots"})
            continue
        if real.is_dir():
            try:
                kids = sorted(x.name + ("/" if x.is_dir() else "") for x in real.iterdir())
            except OSError as exc:
                out.append({"path": p, "error": str(exc)[:200]})
                continue
            out.append({"path": p, "dir": kids[:200]})
        elif real.is_file():
            try:
                data = real.read_bytes()[:MAX_FILE_BYTES]
            except OSError as exc:
                out.append({"path": p, "error": str(exc)[:200]})
                continue
            total += len(data)
            if total > MAX_TOTAL_BYTES:
                out.append({"path": p, "error": "aggregate result budget exceeded"})
                break
            try:
                out.append({"path": p, "text": data.decode("utf-8")})
            except UnicodeDecodeError:
                out.append({"path": p, "error": "binary file not shown"})
        else:
            out.append({"path": p, "error": "not found"})
    return {"results": out}


def tool_find(pattern: str, roots: list[str], *, text: str = "", limit: int = MAX_FIND_MATCHES) -> dict:
    """Search fallback (no shell): filename/glob match, optional text substring filter."""
    matches: list[dict] = []
    for root in roots:
        try:
            base = Path(root).expanduser().resolve()
        except OSError:
            continue
        if not base.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = sorted(d for d in dirnames if d not in FIND_EXCLUDES)
            for name in sorted(filenames):
                if len(matches) >= min(limit, MAX_FIND_MATCHES):
                    return {"matches": matches, "truncated": True}
                if not fnmatch.fnmatch(name, pattern):
                    continue
                full = Path(dirpath) / name
                rel = str(full.relative_to(base))
                if text:
                    try:
                        if full.stat().st_size > MAX_FILE_BYTES:
                            continue
                        content = full.read_text(encoding="utf-8", errors="strict")
                    except (OSError, ValueError):
                        continue
                    if text not in content:
                        continue
                matches.append({"path": f"/{base.name}/{rel}", "native": str(full)})
    return {"matches": matches, "truncated": False}


def disabled(name: str) -> dict:
    return {"error": "TOOL_DISABLED", "tool": name,
            "hint": "Enable it in chatbridge.yaml (allow_exec/allow_patch + read_only:false) — off by default."}


def record_event(home: Path, entry: dict[str, Any]) -> None:
    """Append one bounded tool-call record to the bridge's own transcript log."""
    try:
        logdir = home / "chatbridge-sessions"
        logdir.mkdir(parents=True, exist_ok=True)
        line = json.dumps({k: str(v)[:2000] for k, v in entry.items()})[:4000]
        with open(logdir / "events.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": time.time(), "event": json.loads(line)}) + "\n")
    except OSError:
        pass


def search_events(home: Path, query: str = "", limit: int = 30) -> dict:
    path = home / "chatbridge-sessions" / "events.jsonl"
    hits: list[dict] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()[-500:]
    except OSError:
        return {"sessions": []}
    for line in reversed(lines):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if query and query not in json.dumps(row):
            continue
        hits.append(row)
        if len(hits) >= min(limit, 30):
            break
    return {"sessions": hits}


MAX_IMAGE_BYTES = 10 * 1024 * 1024
_IMAGE_MAGIC = {
    b"\x89PNG\r\n\x1a\n": "image/png",
    b"\xff\xd8\xff": "image/jpeg",
    b"GIF87a": "image/gif",
    b"GIF89a": "image/gif",
    b"RIFF": None,  # maybe WebP; checked below
}


def view_image(path: str, roots: list[str]):
    """Dedicated image-read tool, gated by the same approved roots as read.

    Returns an MCP ImageContent block on success (delivered as vision, not text),
    or a redacted error dict otherwise. Transport and decode remain bounded.
    """
    from mcp.types import ImageContent

    real = resolve_virtual(path, roots)
    if real is None:
        return {"error": f"outside approved roots: {path}"}
    if not real.is_file():
        return {"error": f"not found: {path}"}
    try:
        if real.stat().st_size > MAX_IMAGE_BYTES:
            return {"error": "image exceeds 10 MiB cap"}
        raw = real.read_bytes()[: MAX_IMAGE_BYTES + 1]
    except OSError as exc:
        return {"error": str(exc)[:200]}
    mime: str | None = None
    for magic, kind in _IMAGE_MAGIC.items():
        if raw.startswith(magic):
            mime = kind
            break
    if mime is None and raw.startswith(b"RIFF") and raw[8:12] == b"WEBP":
        mime = "image/webp"
    if mime is None:
        return {"error": "not a supported image (png/jpeg/gif/webp)"}
    import base64
    return ImageContent(data=base64.b64encode(raw).decode("ascii"), mime_type=mime)
