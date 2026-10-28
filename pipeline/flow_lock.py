"""Legal-flow lock (deterministic plumbing, NOT logic).

While a draft / filing flow runs, the continuous-learning keeper (and its self-directed-
learning-local-gpu / -local-gpu crons, which all invoke continuous_learner_keeper.sh) skip their GPU
lanes so nothing contends for the single local-gpu drafter slot on :8000. This is the
"crons auto-pause when a draft or the full legal flow is running" rule.

Cooperative advisory lock: the drafter WRITES a small lock file (pid + epoch + tag); the
keeper READS it (freshness + optional pid-liveness) and yields its tick. No hard mutex,
no new pipeline — it just lets the existing keeper's existing lock discipline see drafts.

Entry points that hold the local-gpu (m0_draft_reply, filing_pipeline, run_monday_mailing) call
`with flow_lock.hold("draft"): ...` (or acquire()/release() around the run).
"""
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
import os
import time
import contextlib


def _root() -> str:
    # m0 runs under WSL where the repo mounts at <home>; honor that so the
    # bash keeper and this python resolve the SAME file. Fall back to BEEZLE_ROOT / default.
    if os.path.isdir(str(_eroot())):
        return str(_eroot())
    return os.environ.get("BEEZLE_ROOT", str(_eroot()))


def _path() -> str:
    return os.path.join(_root(), "output", ".legal_flow_active.lock")


def acquire(tag: str = "draft") -> None:
    """Create/refresh the lock (mtime = now). Call before each unit of work so a long
    multi-case run never goes stale mid-flight. Never raises."""
    try:
        p = _path()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(f"{os.getpid()} {int(time.time())} {tag}")
    except Exception:
        pass


def release() -> None:
    """Remove the lock. Never raises (missing file is fine)."""
    try:
        os.remove(_path())
    except Exception:
        pass


@contextlib.contextmanager
def hold(tag: str = "draft"):
    acquire(tag)
    try:
        yield
    finally:
        release()


def is_active(max_age: int = 1800) -> bool:
    """True iff a fresh lock exists (mtime within max_age seconds). Stale locks from a
    crashed flow auto-clear by age. Never raises."""
    try:
        p = _path()
        if not os.path.exists(p):
            return False
        return (time.time() - os.path.getmtime(p)) <= max_age
    except Exception:
        return False
