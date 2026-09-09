from __future__ import annotations

import json
import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path


def _home() -> Path:
    raw = os.environ.get("HERMES_HOME") or str(Path.home() / ".hermes")
    return Path(raw).expanduser()


@dataclass
class BridgeConfig:
    approved_roots: list[str] = field(default_factory=list)
    read_only: bool = True
    allow_exec: bool = False
    allow_patch: bool = False
    allow_save: bool = False
    exec_allowlist: list[str] = field(default_factory=list)
    secret_token: str = ""
    port: int = 0  # 0 = ephemeral
    tunnel_host: str = ""  # public tunnel hostname (e.g. xxx.trycloudflare.com); empty = loopback only
    agents_enabled: bool = False  # multi-agent workers (hermes -z runs); default OFF
    agents_max_workers: int = 2  # concurrency cap, hard max 8
    worker_provider: str = ""  # empty = configured defaults
    worker_model: str = ""  # empty = configured defaults
    worker_argv: list[str] = field(default_factory=list)  # extra argv after the binary (wrappers)
    hermes_bin: str = "hermes"  # worker runner (tests point at a stub)

    @classmethod
    def load(cls, home: Path | None = None) -> "BridgeConfig":
        base = home or _home()
        path = base / "chatbridge.yaml"
        cfg = cls()
        if path.exists():
            for line in path.read_text(encoding="utf-8-sig").splitlines():
                s = line.strip()
                if not s or s.startswith("#") or ":" not in s:
                    continue
                k, v = (p.strip() for p in s.split(":", 1))
                if k == "read_only":
                    cfg.read_only = v.lower() == "true"
                elif k == "allow_exec":
                    cfg.allow_exec = v.lower() == "true"
                elif k == "allow_patch":
                    cfg.allow_patch = v.lower() == "true"
                elif k == "port":
                    cfg.port = int(v or 0)
                elif k == "approved_roots":
                    cfg.approved_roots = [r.strip() for r in v.split(",") if r.strip()]
                elif k == "exec_allowlist":
                    cfg.exec_allowlist = [r.strip() for r in v.split(";") if r.strip()]
                elif k == "allow_save":
                    cfg.allow_save = v.lower() == "true"
                elif k == "tunnel_host":
                    cfg.tunnel_host = v.strip().lower()
                elif k == "agents_enabled":
                    cfg.agents_enabled = v.lower() == "true"
                elif k == "agents_max_workers":
                    try:
                        cfg.agents_max_workers = int(v or 2)
                    except ValueError:
                        pass
                elif k == "worker_provider":
                    cfg.worker_provider = v.strip()
                elif k == "worker_model":
                    cfg.worker_model = v.strip()
                elif k == "hermes_bin":
                    cfg.hermes_bin = v.strip() or "hermes"
                elif k == "worker_argv":
                    cfg.worker_argv = [a.strip() for a in v.split(";") if a.strip()]
        tok_path = base / "chatbridge.token"
        if tok_path.exists():
            cfg.secret_token = tok_path.read_text(encoding="utf-8").strip()
        else:
            cfg.secret_token = secrets.token_urlsafe(32)
            tok_path.write_text(cfg.secret_token + "\n", encoding="utf-8")
            try:
                os.chmod(tok_path, 0o600)
            except OSError:
                pass
        return cfg

    def tool_names(self) -> list[str]:
        # Monotonic-discovery safe: read-only tools always listed; write tools
        # listed only when explicitly enabled (fresh endpoint starts without them).
        names = ["read", "view_image", "find", "session"]
        if self.agents_enabled:
            names.append("agents")
        if self.allow_save and not self.read_only:
            names.append("download_artifact")
        if self.allow_patch and not self.read_only:
            names.append("apply_patch")
        if self.allow_exec and not self.read_only:
            names.extend(["exec_command", "write_stdin"])
        return names


def resolve_under_roots(path: str, roots: list[str]) -> Path | None:
    """Return canonical path iff inside an approved root, else None (fail closed)."""
    try:
        cand = Path(path).expanduser()
        if not cand.is_absolute():
            return None
        real = cand.resolve()
        for r in roots:
            try:
                root = Path(r).expanduser().resolve()
            except OSError:
                continue
            if real == root or root in real.parents:
                return real
    except (OSError, RuntimeError, ValueError):
        return None
    return None


def resolve_virtual(virtual: str, roots: list[str]) -> Path | None:
    """Map a tool path to native: `/rootname/rel` disambiguates multi-root,
    bare `rel` resolves against the first approved root, absolute natives pass through."""
    if virtual.startswith("/"):
        direct = resolve_under_roots(virtual, roots)
        if direct is not None:
            return direct
        for r in roots:
            try:
                base = Path(r).expanduser().resolve()
            except OSError:
                continue
            if virtual == f"/{base.name}" or virtual.startswith(f"/{base.name}/"):
                return resolve_under_roots(str(base / virtual[len(base.name) + 2:]), roots)
        return None
    if not roots:
        return None
    try:
        base = Path(roots[0]).expanduser().resolve()
    except OSError:
        return None
    return resolve_under_roots(str(base / virtual), roots)
