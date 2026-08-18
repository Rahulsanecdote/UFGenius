"""Tests for the background full-scan job registry (src/scanner/scan_jobs.py).

Offline and fast: the scan callable is injected, so nothing fetches. What is
under test is the registry's contract — that a scan never blocks the caller,
that a crash surfaces as a failed job rather than one that never finishes, that
two callers cannot stack two 500-ticker fan-outs on a small box, and that a poll
which cannot find its job says why instead of 404-ing blankly.
"""

from __future__ import annotations

import threading
import time
from datetime import timedelta

import pytest

from src.scanner import scan_jobs


@pytest.fixture(autouse=True)
def _clean_registry():
    scan_jobs.reset()
    yield
    scan_jobs.reset()


def _wait_for(job_id, status, timeout=5.0):
    """Poll until the job reaches `status` (or fail loudly)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = scan_jobs.get_job(job_id)
        if job["status"] == status:
            return job
        time.sleep(0.01)
    raise AssertionError(f"job never reached {status}: {scan_jobs.get_job(job_id)}")


class TestLifecycle:
    def test_start_returns_immediately_while_the_scan_runs(self):
        release = threading.Event()

        def slow_scan(progress=None, **kw):
            release.wait(timeout=5)
            return {"total_scanned": 503}

        started = time.time()
        job = scan_jobs.start_scan(scan=slow_scan)
        # The whole point: the caller is not waiting on the scan.
        assert time.time() - started < 1.0
        assert job["status"] == scan_jobs.STATUS_RUNNING
        assert scan_jobs.get_job(job["job_id"])["status"] == scan_jobs.STATUS_RUNNING

        release.set()
        done = _wait_for(job["job_id"], scan_jobs.STATUS_DONE)
        assert done["result"] == {"total_scanned": 503}
        assert done["elapsed_sec"] >= 0

    def test_kwargs_reach_the_scan(self):
        seen = {}

        def scan(progress=None, **kw):
            seen.update(kw)
            return {}

        job = scan_jobs.start_scan(scan=scan, account_size=25000.0)
        _wait_for(job["job_id"], scan_jobs.STATUS_DONE)
        assert seen == {"account_size": 25000.0}

    def test_result_is_absent_until_done(self):
        release = threading.Event()
        job = scan_jobs.start_scan(scan=lambda progress=None, **kw: (release.wait(5), {})[1])
        assert "result" not in scan_jobs.get_job(job["job_id"])
        release.set()
        assert "result" in _wait_for(job["job_id"], scan_jobs.STATUS_DONE)

    def test_a_crash_becomes_a_failed_job(self):
        # A scan that raises must not leave a job "running" forever — that is
        # indistinguishable from a slow one and spins the UI indefinitely.
        def boom(progress=None, **kw):
            raise ValueError("provider exploded")

        job = scan_jobs.start_scan(scan=boom)
        failed = _wait_for(job["job_id"], scan_jobs.STATUS_ERROR)
        assert "provider exploded" in failed["error"]
        assert "result" not in failed


class TestProgress:
    def test_progress_updates_are_visible_to_a_poller(self):
        gate, seen = threading.Event(), threading.Event()

        def scan(progress=None, **kw):
            progress("prefilter", 120, 503)
            seen.set()
            gate.wait(timeout=5)
            return {}

        job = scan_jobs.start_scan(scan=scan)
        assert seen.wait(timeout=5)
        view = scan_jobs.get_job(job["job_id"])
        assert (view["stage"], view["done"], view["total"]) == ("prefilter", 120, 503)
        gate.set()
        _wait_for(job["job_id"], scan_jobs.STATUS_DONE)

    def test_scan_without_progress_support_still_works(self):
        # progress is optional everywhere; a scan that ignores it must not break.
        job = scan_jobs.start_scan(scan=lambda **kw: {"ok": True})
        assert _wait_for(job["job_id"], scan_jobs.STATUS_DONE)["result"] == {"ok": True}


class TestSingleFlight:
    def test_second_caller_joins_the_running_scan(self):
        release = threading.Event()
        calls = []

        def scan(progress=None, **kw):
            calls.append(1)
            release.wait(timeout=5)
            return {}

        first = scan_jobs.start_scan(scan=scan)
        second = scan_jobs.start_scan(scan=scan)
        assert second["job_id"] == first["job_id"]
        assert second["joined_existing"] is True
        assert first["joined_existing"] is False
        release.set()
        _wait_for(first["job_id"], scan_jobs.STATUS_DONE)
        # Two concurrent 500-ticker fan-outs do not finish twice as fast on a
        # 0.1-CPU instance; they thrash. Exactly one scan ran.
        assert len(calls) == 1

    def test_a_new_scan_can_start_once_the_previous_finished(self):
        first = scan_jobs.start_scan(scan=lambda progress=None, **kw: {})
        _wait_for(first["job_id"], scan_jobs.STATUS_DONE)
        second = scan_jobs.start_scan(scan=lambda progress=None, **kw: {})
        assert second["job_id"] != first["job_id"]
        assert second["joined_existing"] is False

    def test_active_job_exposes_the_running_scan(self):
        release = threading.Event()
        assert scan_jobs.active_job() is None
        job = scan_jobs.start_scan(scan=lambda progress=None, **kw: (release.wait(5), {})[1])
        assert scan_jobs.active_job()["job_id"] == job["job_id"]
        release.set()
        _wait_for(job["job_id"], scan_jobs.STATUS_DONE)
        assert scan_jobs.active_job() is None


class TestBounds:
    def test_unknown_job_names_the_likely_cause(self):
        out = scan_jobs.get_job("deadbeef")
        assert out["status"] == scan_jobs.STATUS_UNKNOWN
        # Multi-worker gunicorn is the non-obvious failure here, so it is named
        # rather than left as a bare "not found".
        assert "--workers 1" in out["error"]

    def test_blank_job_id_is_unknown_not_a_crash(self):
        assert scan_jobs.get_job("")["status"] == scan_jobs.STATUS_UNKNOWN

    def test_finished_jobs_expire(self):
        job = scan_jobs.start_scan(scan=lambda progress=None, **kw: {})
        _wait_for(job["job_id"], scan_jobs.STATUS_DONE)
        with scan_jobs._lock:
            record = scan_jobs._jobs[job["job_id"]]
            record["finished_at"] = record["finished_at"] - timedelta(
                seconds=scan_jobs._RESULT_TTL_SEC + 60)
        assert scan_jobs.get_job(job["job_id"])["status"] == scan_jobs.STATUS_UNKNOWN

    def test_registry_is_trimmed_and_never_evicts_a_runner(self):
        for _ in range(scan_jobs._MAX_JOBS + 4):
            done = scan_jobs.start_scan(scan=lambda progress=None, **kw: {})
            _wait_for(done["job_id"], scan_jobs.STATUS_DONE)
        release = threading.Event()
        running = scan_jobs.start_scan(
            scan=lambda progress=None, **kw: (release.wait(5), {})[1])
        for _ in range(3):
            scan_jobs.get_job("x")          # each poll reaps
        assert scan_jobs.get_job(running["job_id"])["status"] == scan_jobs.STATUS_RUNNING
        with scan_jobs._lock:
            assert len(scan_jobs._jobs) <= scan_jobs._MAX_JOBS + 1
        release.set()


class TestLatestJob:
    """What a client with no job id needs — including after the scan finished."""

    def test_prefers_the_running_job(self):
        release = threading.Event()
        done = scan_jobs.start_scan(scan=lambda progress=None, **kw: {"n": 1})
        _wait_for(done["job_id"], scan_jobs.STATUS_DONE)
        running = scan_jobs.start_scan(
            scan=lambda progress=None, **kw: (release.wait(5), {})[1])
        assert scan_jobs.latest_job()["job_id"] == running["job_id"]
        release.set()
        _wait_for(running["job_id"], scan_jobs.STATUS_DONE)

    def test_falls_back_to_the_most_recent_result(self):
        # A tab reloaded a second after the scan finished would otherwise see
        # "idle" while a usable result sat in the registry, unreachable because
        # the job id went away with the old page.
        first = scan_jobs.start_scan(scan=lambda progress=None, **kw: {"n": 1})
        _wait_for(first["job_id"], scan_jobs.STATUS_DONE)
        second = scan_jobs.start_scan(scan=lambda progress=None, **kw: {"n": 2})
        _wait_for(second["job_id"], scan_jobs.STATUS_DONE)
        latest = scan_jobs.latest_job()
        assert latest["job_id"] == second["job_id"]
        assert latest["result"] == {"n": 2}

    def test_none_when_nothing_has_ever_run(self):
        assert scan_jobs.latest_job() is None
