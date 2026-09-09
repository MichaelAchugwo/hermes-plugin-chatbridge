from __future__ import annotations

"""Hermes-native worker delegation for the calling Hermes workflow.

CoS spawns worker *browser chats* via extension automation — deliberately not
ported (no browser scraping). The Hermes-native equivalent: a worker is a
background ``hermes -z`` one-shot run whose report the prime collects with
the ``agents`` tool. Workers spend Hermes-side model quota; the prime driving
them uses the configured Hermes model/provider quota.

Security notes:
- Disabled unless ``agents_enabled: true`` in chatbridge.yaml (default OFF).
- Workers never receive the connector URL, so they cannot spawn workers or
  call back into the bridge. As a backstop, the bridge refuses ``spawn``
  when it itself runs inside a worker (``HERMES_CHATBRIDGE_WORKER`` set).
- Worker cwd is locked to the first approved root; tasks cannot change it.
- Concurrency capped (default 2, hard max 8).
"""

import os
import subprocess
import threading
import time
from dataclasses import dataclass, field

from .session import ExecSession

WORKER_ENV_MARKER = "HERMES_CHATBRIDGE_WORKER"
HARD_MAX_WORKERS = 8


@dataclass
class Worker:
    worker_id: str
    label: str
    task: str
    session_id: str
    usage_path: str
    history: list[dict] = field(default_factory=list)


_workers: dict[str, Worker] = {}
_workers_lock = threading.Lock()
_exec_sessions: dict[str, ExecSession] = {}


def _running_workers() -> int:
    from . import exec as _exec

    with _workers_lock:
        sessions = [w.session_id for w in _workers.values()]
    n = 0
    for sid in sessions:
        sess = _exec._sessions.get(sid)
        if sess is not None and sess.proc.poll() is None:
            n += 1
    return n


def spawn(task: str, cfg, home) -> dict:
    """Start a worker running ``hermes -z <task>`` in the first approved root."""
    import secrets as _secrets

    if os.environ.get(WORKER_ENV_MARKER):
        return {"error": "workers cannot spawn workers"}
    if not getattr(cfg, "agents_enabled", False):
        return {"error": "TOOL_DISABLED"}
    if not task or not task.strip():
        return {"error": "empty task"}
    roots = list(getattr(cfg, "approved_roots", []))
    if not roots:
        return {"error": "no approved roots"}
    maxw = max(1, min(int(getattr(cfg, "agents_max_workers", 2) or 2), HARD_MAX_WORKERS))
    if _running_workers() >= maxw:
        return {"error": f"worker cap reached ({maxw})"}
    from . import exec as _exec

    wid = "w_" + _secrets.token_hex(4)
    label = f"worker-{len(_workers) + 1}"
    workers_dir = home / "workers"
    try:
        workers_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return {"error": f"cannot create workers dir: {exc}"}
    usage_path = str(workers_dir / f"{wid}.usage.json")
    hermes_bin = getattr(cfg, "hermes_bin", "") or "hermes"
    argv = [hermes_bin] + [a for a in getattr(cfg, "worker_argv", []) if a] + ["-z", task.strip()]
    if getattr(cfg, "worker_provider", ""):
        argv += ["--provider", cfg.worker_provider]
    if getattr(cfg, "worker_model", ""):
        argv += ["-m", cfg.worker_model]
    argv += ["--usage-file", usage_path]
    env = dict(os.environ)
    env[WORKER_ENV_MARKER] = "1"
    try:
        proc = subprocess.Popen(argv, cwd=roots[0], stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                env=env)
    except OSError as exc:
        return {"error": f"cannot start worker: {exc}"}
    sess = ExecSession(proc, f"hermes -z worker {wid}")
    sid = "sess_" + _secrets.token_hex(8)
    with _exec._sessions_lock:
        _exec._sessions[sid] = sess
    worker = Worker(worker_id=wid, label=label, task=task.strip()[:2000],
                    session_id=sid, usage_path=usage_path)
    worker.history.append({"role": "task", "text": task.strip()[:2000]})
    with _workers_lock:
        _workers[wid] = worker
    return {"worker_id": wid, "label": label, "state": "running"}


def _collect(sess: ExecSession, yield_ms: int) -> tuple[str, bool]:
    collected = ""
    deadline = time.time() + max(100, min(yield_ms, 300 * 1000)) / 1000.0
    running = True
    while time.time() < deadline:
        chunk, running = sess.take_output()
        collected += chunk
        if not running:
            break
        time.sleep(0.05)
    return collected, running


def status(worker_id: str = "", tail_chars: int = 2000) -> dict:
    from . import exec as _exec

    with _workers_lock:
        workers = dict(_workers) if not worker_id else ({worker_id: _workers[worker_id]} if worker_id in _workers else {})
    if worker_id and worker_id not in workers:
        return {"error": "unknown worker_id"}
    out = []
    for wid, w in workers.items():
        sess = _exec._sessions.get(w.session_id)
        if sess is None:
            state, tail = "done", ""
            for h in reversed(w.history):
                if h["role"] == "report":
                    tail = h["text"][-tail_chars:]
                    break
        else:
            chunk, running = sess.take_output()
            if chunk:
                w.history.append({"role": "output", "text": chunk})
            state = "running" if running else "done"
            tail = "".join(h["text"] for h in w.history if h["role"] in ("output", "report"))[-tail_chars:]
            if not running:
                w.history.append({"role": "report", "text": tail})
                with _exec._sessions_lock:
                    _exec._sessions.pop(w.session_id, None)
        out.append({"worker_id": wid, "label": w.label, "state": state, "report_tail": tail})
    return {"workers": out}


def message(worker_id: str, text: str, cfg, home,
            yield_ms: int = 2000) -> dict:
    """Talk to a worker. Finished workers revive: a follow-up run is seeded
    with (original task, prior report, new message). Running workers cannot
    take injected input — returns current state and output instead."""
    with _workers_lock:
        w = _workers.get(worker_id)
    if w is None:
        return {"error": "unknown worker_id"}
    if not text or not text.strip():
        return {"error": "empty message"}
    from . import exec as _exec

    sess = _exec._sessions.get(w.session_id)
    if sess is not None and sess.proc.poll() is None:
        chunk, _ = sess.take_output()
        if chunk:
            w.history.append({"role": "output", "text": chunk})
        tail = "".join(h["text"] for h in w.history if h["role"] in ("output", "report"))[-2000:]
        return {"worker_id": worker_id, "state": "running",
                "note": "worker is running; input cannot be injected — wait for done, then message again",
                "report_tail": tail}
    prior = "".join(h["text"] for h in w.history if h["role"] in ("output", "report"))[-4000:]
    followup = (f"Continuing earlier task: {w.task}\n\nPrior report:\n{prior}\n\n"
                f"New instruction from the prime chat: {text.strip()[:2000]}")
    w.history.append({"role": "prime", "text": text.strip()[:2000]})
    res = spawn(followup, cfg, home)
    if "error" in res:
        return res
    # Keep the stable worker_id; adopt the fresh run's session.
    with _workers_lock:
        new_w = _workers.pop(res["worker_id"])
        new_w.worker_id = worker_id
        new_w.label = w.label
        new_w.history = w.history + [{"role": "task", "text": followup[:2000]}]
        _workers[worker_id] = new_w
    from . import exec as _exec2
    with _exec2._sessions_lock:
        _exec2._sessions.pop(w.session_id, None)  # retired prior run, if lingering
        sess2 = _exec2._sessions.get(new_w.session_id)
    collected, running = ("", True) if sess2 is None else _collect(sess2, yield_ms)
    if collected:
        new_w.history.append({"role": "output", "text": collected})
    tail = "".join(h["text"] for h in new_w.history if h["role"] in ("output", "report"))[-2000:]
    return {"worker_id": worker_id, "state": "running" if running else "done", "report_tail": tail}


def finish(worker_id: str, kill: bool = False) -> dict:
    """Collect a worker's final report (and retire it). kill=True stops a
    running worker first."""
    with _workers_lock:
        w = _workers.get(worker_id)
    if w is None:
        return {"error": "unknown worker_id"}
    from . import exec as _exec

    sess = _exec._sessions.get(w.session_id)
    if sess is None:
        report = "".join(h["text"] for h in w.history if h["role"] in ("output", "report"))
        with _workers_lock:
            _workers.pop(worker_id, None)
        return {"worker_id": worker_id, "state": "done", "report": report[-8000:]}
    if kill:
        try:
            sess.proc.kill()
        except OSError:
            pass
    collected, _ = _collect(sess, 15000)
    if collected:
        w.history.append({"role": "output", "text": collected})
    report = "".join(h["text"] for h in w.history if h["role"] in ("output", "report"))
    with _exec._sessions_lock:
        _exec._sessions.pop(w.session_id, None)
    with _workers_lock:
        _workers.pop(worker_id, None)
    return {"worker_id": worker_id, "state": "done", "report": report[-8000:]}
