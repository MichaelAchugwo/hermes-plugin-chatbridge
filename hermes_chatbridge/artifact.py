from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .config import resolve_virtual

MAX_ARTIFACT_BYTES = 20 * 1024 * 1024
ALLOWED_HOSTS = {"files.oaiusercontent.com"}


def _reject(reason: str) -> dict:
    return {"error": reason}


def save_artifact(url: str, dest: str, roots: list[str], *, opener=urlopen) -> dict:
    """Save an allowlisted remote artifact into an approved folder.

    Only HTTPS URLs on the OpenAI files host are accepted; signed query
    material is never written to the transcript (callers must redact it).
    Destination parents must already exist; an existing destination is refused.
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return _reject("malformed url")
    if parsed.scheme != "https":
        return _reject("only https artifact sources accepted")
    if (parsed.hostname or "").lower() not in ALLOWED_HOSTS:
        return _reject(f"artifact host not allowlisted: {parsed.hostname}")
    real = resolve_virtual(dest, roots)
    if real is None:
        return _reject(f"destination outside approved roots: {dest}")
    if not real.parent.is_dir():
        return _reject("destination parent must already exist")
    if real.exists():
        return _reject("destination already exists; refusing overwrite")
    req = Request(url, headers={"User-Agent": "hermes-chatbridge/0.5"}, method="GET")
    try:
        with opener(req, timeout=120) as resp:
            final_host = (urlparse(getattr(resp, "url", url)).hostname or "").lower()
            if final_host not in ALLOWED_HOSTS:
                return _reject(f"redirect left allowlisted host: {final_host}")
            chunks: list[bytes] = []
            total = 0
            while True:
                piece = resp.read(65536)
                if not piece:
                    break
                total += len(piece)
                if total > MAX_ARTIFACT_BYTES:
                    return _reject("artifact exceeds 20 MiB cap")
                chunks.append(piece)
    except OSError as exc:
        return _reject(f"download failed: {exc}"[:200])
    try:
        real.write_bytes(b"".join(chunks))
    except OSError as exc:
        return _reject(str(exc)[:200])
    return {"saved": dest, "bytes": total}
