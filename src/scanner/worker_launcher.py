"""In-process launcher for the always-on movers worker (Phase 7/8 production glue).

On Render (and most PaaS) a separate Background Worker runs in its **own
container with its own filesystem**, so the Phase 7 shared-state file it writes
(``data/movers_worker.json``) is invisible to the web service — the dashboard's
worker strip would stay empty. Running the worker loop *inside the web process*
fixes that: worker and dashboard share one filesystem, so the panel (and the
Phase 8 live-streaming prices) populate.

This module starts the worker in a **daemon thread** when
``RUN_WORKER_IN_PROCESS`` is set. It is:

- **opt-in** — default off, so local ``python dashboard.py`` and the plain web
  deploy are unchanged;
- **start-once** — guarded so it launches at most once per process (gunicorn
  runs a single worker here; the guard also covers accidental double-imports);
- **fail-soft** — a crash in the worker thread is logged and never touches the
  web request path; the dashboard keeps serving.
"""

from __future__ import annotations

import threading

from src.utils import config
from src.utils.logger import get_logger

log = get_logger(__name__)

_lock = threading.Lock()
_started = False
_thread: threading.Thread | None = None


def _default_run() -> None:
    """Run the worker loop; log and swallow anything so the thread dies quietly."""
    try:
        from src.scanner.movers_worker import run_worker

        run_worker()
    except Exception as exc:  # never propagate out of the daemon thread
        log.error(f"in-process movers worker exited ({type(exc).__name__}: {exc})",
                  exc_info=True)


def maybe_start_worker_thread(run=None) -> threading.Thread | None:
    """Start the worker in a daemon thread iff ``RUN_WORKER_IN_PROCESS`` is on.

    Returns the thread (or the existing one) when running in-process, else None.
    Idempotent: repeated calls never spawn a second worker. ``run`` is injectable
    for tests.
    """
    global _started, _thread
    if not config.RUN_WORKER_IN_PROCESS:
        return None
    with _lock:
        if _started:
            return _thread
        _started = True
        _thread = threading.Thread(
            target=run or _default_run, name="movers-worker-inproc", daemon=True,
        )
        _thread.start()
    log.info("in-process movers worker thread started (RUN_WORKER_IN_PROCESS)")
    return _thread


def _reset_for_tests() -> None:
    """Test hook — clear the start-once guard so a fresh call can spawn again."""
    global _started, _thread
    with _lock:
        _started = False
        _thread = None
