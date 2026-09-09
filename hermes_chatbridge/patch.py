from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .config import resolve_virtual


@dataclass
class Hunk:
    old_start: int
    old_lines: list[str] = field(default_factory=list)
    new_lines: list[str] = field(default_factory=list)


@dataclass
class FilePatch:
    virtual: str
    is_new: bool = False
    is_delete: bool = False
    hunks: list[Hunk] = field(default_factory=list)


def _strip_prefix(path: str) -> str:
    for prefix in ("a/", "b/"):
        if path.startswith(prefix):
            return path[len(prefix):]
    return path


def _clean_filename(token: str) -> str:
    return token.split("\t", 1)[0].strip().strip('"')


def _split_lines(raw: bytes, virtual: str) -> tuple[list[str], str] | tuple[None, dict]:
    """Strict-decode and normalize CRLF→LF; returns (lines, ending) or (None, error)."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None, {"error": f"non-utf8 file refused: {virtual}"}
    if "\x00" in text:
        return None, {"error": f"binary file refused: {virtual}"}
    ending = "\r\n" if "\r\n" in text else "\n"
    lines = text.replace("\r\n", "\n").split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines, ending


def parse_patch(text: str) -> list[FilePatch]:
    """Parse a unified diff into file patches. Strict: garbage fails closed."""
    files: list[FilePatch] = []
    old_name: str | None = None
    current: FilePatch | None = None
    hunk: Hunk | None = None
    for raw in text.splitlines():
        line = raw
        if line.startswith("--- "):
            old_name, current, hunk = _clean_filename(line[4:]), None, None
        elif line.startswith("+++ "):
            if old_name is None:
                raise ValueError("+++ without ---")
            new_name = _clean_filename(line[4:])
            if old_name in ("/dev/null", "dev/null"):
                current = FilePatch(virtual=_strip_prefix(new_name), is_new=True)
            elif new_name in ("/dev/null", "dev/null"):
                current = FilePatch(virtual=_strip_prefix(old_name), is_delete=True)
            else:
                current = FilePatch(virtual=_strip_prefix(new_name))
            if not current.virtual or current.virtual.startswith("/"):
                raise ValueError(f"bad target path: {current.virtual!r}")
            files.append(current)
            old_name, hunk = None, None
        elif line.startswith("@@ "):
            if current is None:
                raise ValueError("hunk outside file")
            try:
                start = int(line.split(" ", 3)[1][1:].split(",", 1)[0])
            except (IndexError, ValueError):
                raise ValueError(f"bad hunk header: {line!r}") from None
            hunk = Hunk(old_start=start)
            current.hunks.append(hunk)
        elif hunk is not None and (not line or line[:1] in (" ", "-", "+", "\\")):
            if not line or line.startswith("\\"):
                continue
            marker, body = line[0], line[1:]
            if marker == " " or marker == "-":
                hunk.old_lines.append(body)
            if marker == " " or marker == "+":
                hunk.new_lines.append(body)
        elif not line.strip():
            continue
        else:
            raise ValueError(f"unrecognized diff line: {line[:60]!r}")
    if old_name is not None:
        raise ValueError("--- without +++")
    for f in files:
        if not f.virtual:
            raise ValueError("empty file patch")
        if not f.hunks and not (f.is_new or f.is_delete):
            raise ValueError(f"no hunks for {f.virtual}")
    return files


def apply_patches(files: list[FilePatch], roots: list[str]) -> dict:
    """Two-phase apply: resolve + preflight every file first, then write. Any failure writes nothing."""
    if not files:
        return {"error": "empty patch"}
    planned: list[tuple[Path, str | None, str]] = []
    for fp in files:
        real = resolve_virtual(fp.virtual, roots)
        if real is None:
            return {"error": f"target outside approved roots: {fp.virtual}"}
        if fp.is_delete:
            if not real.is_file():
                return {"error": f"delete target missing: {fp.virtual}"}
            try:
                raw = real.read_bytes()
            except OSError:
                return {"error": f"delete target unreadable: {fp.virtual}"}
            split = _split_lines(raw, fp.virtual)
            if split[0] is None:
                return split[1]
            work, _ending = split
            for h in fp.hunks:
                idx = h.old_start - 1
                if work[idx:idx + len(h.old_lines)] != h.old_lines:
                    return {"error": f"context mismatch in {fp.virtual} @-{h.old_start}"}
            planned.append((real, None, "\n"))
            continue
        if fp.is_new:
            if real.exists():
                return {"error": f"create target exists: {fp.virtual}"}
            if not real.parent.is_dir():
                return {"error": f"parent dir must exist: {fp.virtual}"}
            work = []
            ending = "\n"
        else:
            if not real.is_file():
                return {"error": f"target missing: {fp.virtual}"}
            try:
                raw = real.read_bytes()
            except OSError as exc:
                return {"error": str(exc)[:200]}
            split = _split_lines(raw, fp.virtual)
            if split[0] is None:
                return split[1]
            work, ending = split
        for h in fp.hunks:
            if fp.is_new:
                if h.old_lines:
                    return {"error": f"new file with context in {fp.virtual}"}
                work.extend(h.new_lines)
                continue
            idx = h.old_start - 1
            if idx < 0 or idx > len(work):
                return {"error": f"hunk out of range in {fp.virtual} @-{h.old_start}"}
            end = idx + len(h.old_lines)
            if work[idx:end] != h.old_lines:
                return {"error": f"context mismatch in {fp.virtual} @-{h.old_start}"}
            work[idx:end] = h.new_lines
        planned.append((real, ending.join(work) + (ending if work else ""), ending))
    for real, text, ending in planned:
        if text is None:
            real.unlink()
        else:
            # newline="" keeps our normalized endings byte-exact (no OS translation).
            with open(real, "w", encoding="utf-8", newline="") as fh:
                fh.write(text)
    return {"applied": [f"/{p[0].name}" for p in planned], "files": len(planned)}
