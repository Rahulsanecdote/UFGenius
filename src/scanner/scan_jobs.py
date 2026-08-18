"""Background job registry for the full-universe scan.

``/api/scan`` used to call ``run_daily_scan`` inline. That scans the whole
configured universe — ~500 tickers for SP500 — inside one HTTP request, against
``gunicorn --timeout 120``. The deployment's own comment sized the work at
"60-120s", i.e. the documented worst case *equalled* the kill deadline, and on a
free 0.1-CPU instance sharing four threads with the in-process movers worker it
is comfortably past it. gunicorn killed the worker mid-scan every time and the
browser reported a dropped connection ("Failed to fetch"), which reads like a
network fault rather than a timeout.

Capping the universe would have made it finish by making it not a full-market
scan. Instead the scan moves off the request: :func:`start_scan` hands it to a
daemon thread and returns immediately, and the client polls :func:`get_job`.
Nothing about the scan itself changes — it takes as long as it takes, and the
request path stops caring.

Two properties this registry has to hold on a small box:

* **Single-flight.** One scan at a time. A second caller joins the running job
  rather than starting another; on 0.1 CPU, two concurrent 500-ticker fan-outs
  do not finish twice as fast, they thrash. It also means a user mashing the
  button cannot pile up work.
* **Bounded.** Finished jobs expire, and the registry is trimmed, so a
  long-running process does not accumulate scan results forever.

**Deployment constraint:** state is per-process, so the poll must land on the
process that started the job. That holds under ``--workers 1`` (which
``render.yaml`` already requires for the in-process worker) and breaks under
multiple workers — so an unknown job id is reported as ``unknown`` with that
cause named, rather than as a bare 404.
"""

from __future__ import annotations

import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from src.utils.logger import get_logger

log = get_logger(__name__)

# Finished jobs stay readable for a while so a slow poller (a phone that slept)
# still collects its result, then are reaped.
_RESULT_TTL_SEC = 900.0
_MAX_JOBS = 8

STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_ERROR = "error"
STATUS_UNKNOWN = "unknown"

_lock = threading.Lock()
_jobs: dict[str, dict] = {}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _age_sec(job: dict, now: Optional[datetime] = None) -> float:
    finished = job.get("finished_at")
    if finished is None:
        return 0.0
    return ((now or _utcnow()) - finished).total_seconds()


def _reap(now: Optional[datetime] = None) -> None:
    """Drop expired finished jobs, then trim to the newest _MAX_JOBS. Caller holds the lock."""
    now = now or _utcnow()
    for job_id in [j for j, job in _jobs.items()
                   if job["status"] != STATUS_RUNNING and _age_sec(job, now) > _RESULT_TTL_SEC]:
        _jobs.pop(job_id, None)
    if len(_jobs) > _MAX_JOBS:
        # Never evict a running job — it has no result to re-fetch.
        finished = sorted((j for j, job in _jobs.items() if job["status"] != STATUS_RUNNING),
                          key=lambda j: _jobs[j]["started_at"])
        for job_id in finished[: len(_jobs) - _MAX_JOBS]:
            _jobs.pop(job_id, None)


def _view(job: dict, now: Optional[datetime] = None) -> dict:
    """JSON-safe snapshot of a job (the result is included only once done)."""
    now = now or _utcnow()
    started = job["started_at"]
    finished = job.get("finished_at")
    out = {
        "job_id": job["job_id"],
        "status": job["status"],
        "stage": job.get("stage"),
        "done": job.get("done", 0),
        "total": job.get("total", 0),
        "started_at": started.isoformat(),
        "elapsed_sec": round(((finished or now) - started).total_seconds(), 1),
    }
    if job["status"] == STATUS_DONE:
        out["result"] = job.get("result")
    elif job["status"] == STATUS_ERROR:
        out["error"] = job.get("error")
    return out


def _run(job_id: str, scan: Callable[..., dict], kwargs: dict) -> None:
    """Thread body. Every outcome lands in the job record — nothing escapes."""
    def progress(stage: str, done: int = 0, total: int = 0) -> None:
        with _lock:
            job = _jobs.get(job_id)
            if job is not None:
                job.update({"stage": stage, "done": int(done), "total": int(total)})

    try:
        result = scan(progress=progress, **kwargs)
        with _lock:
            job = _jobs.get(job_id)
            if job is not None:
                job.update({"status": STATUS_DONE, "result": result,
                            "stage": "done", "finished_at": _utcnow()})
    except Exception as exc:
        # A crashed scan must be reported as a failed job, not as a job that
        # silently never finishes — the second is indistinguishable from a slow
        # one and would leave the UI spinning forever.
        log.exception("Background scan failed")
        with _lock:
            job = _jobs.get(job_id)
            if job is not None:
                job.update({"status": STATUS_ERROR,
                            "error": f"{type(exc).__name__}: {exc}",
                            "finished_at": _utcnow()})


def start_scan(scan: Optional[Callable[..., dict]] = None, **kwargs: Any) -> dict:
    """Start a scan in the background, or join the one already running.

    Returns the job view immediately — never blocks on the scan. ``scan`` is
    injectable for tests; it is called as ``scan(progress=cb, **kwargs)``.
    """
    if scan is None:
        from src.scanner.daily_scan import run_daily_scan

        scan = run_daily_scan

    with _lock:
        _reap()
        for job in _jobs.values():
            if job["status"] == STATUS_RUNNING:
                view = _view(job)
                view["joined_existing"] = True
                return view
        job_id = uuid.uuid4().hex[:12]
        _jobs[job_id] = {
            "job_id": job_id, "status": STATUS_RUNNING, "stage": "starting",
            "done": 0, "total": 0, "started_at": _utcnow(), "finished_at": None,
        }
        view = _view(_jobs[job_id])

    thread = threading.Thread(target=_run, args=(job_id, scan, kwargs),
                              name=f"scan-{job_id}", daemon=True)
    thread.start()
    view["joined_existing"] = False
    return view


def get_job(job_id: str) -> dict:
    """Poll a job. An id this process never had is ``unknown``, with the cause."""
    with _lock:
        _reap()
        job = _jobs.get(str(job_id or ""))
        if job is None:
            return {
                "job_id": job_id,
                "status": STATUS_UNKNOWN,
                "error": "No such scan job in this process. It may have expired, "
                         "or the poll reached a different worker — scan jobs are "
                         "per-process and need gunicorn --workers 1.",
            }
        return _view(job)


def active_job() -> Optional[dict]:
    """The running job, if any (so a reloaded page can rejoin instead of restarting)."""
    with _lock:
        for job in _jobs.values():
            if job["status"] == STATUS_RUNNING:
                return _view(job)
    return None


def latest_job() -> Optional[dict]:
    """The running job, else the most recently finished one.

    What a client with no job id actually wants. Returning only the *running*
    job would mean a page reloaded a second after the scan finished sees "idle"
    while a perfectly good result sits in the registry for another 15 minutes,
    unreachable because the id was in the tab that went away.
    """
    running = active_job()
    if running is not None:
        return running
    with _lock:
        _reap()
        if not _jobs:
            return None
        newest = max(_jobs.values(), key=lambda job: job.get("finished_at") or job["started_at"])
        return _view(newest)


def reset() -> None:
    """Drop all jobs (tests only)."""
    with _lock:
        _jobs.clear()
