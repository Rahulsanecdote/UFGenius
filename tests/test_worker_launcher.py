"""Tests for the in-process worker launcher (worker_launcher.py, Phase 7/8 glue).

The launcher only starts a thread when RUN_WORKER_IN_PROCESS is on, starts at
most once per process, and never lets a worker crash reach the caller.
"""

import threading

import pytest

import src.utils.config as cfg
from src.scanner import worker_launcher as wl


@pytest.fixture(autouse=True)
def _reset():
    wl._reset_for_tests()
    yield
    wl._reset_for_tests()


def test_disabled_returns_none(monkeypatch):
    monkeypatch.setattr(cfg, "RUN_WORKER_IN_PROCESS", False)
    called = []
    assert wl.maybe_start_worker_thread(run=lambda: called.append(1)) is None
    assert called == []


def test_enabled_starts_once(monkeypatch):
    monkeypatch.setattr(cfg, "RUN_WORKER_IN_PROCESS", True)
    ran = threading.Event()
    runs = []

    def fake_run():
        runs.append(1)
        ran.set()

    t1 = wl.maybe_start_worker_thread(run=fake_run)
    assert t1 is not None
    assert ran.wait(timeout=2.0)
    t1.join(timeout=2.0)
    # A second call must not spawn another worker.
    t2 = wl.maybe_start_worker_thread(run=fake_run)
    assert t2 is t1
    assert runs == [1]


def test_thread_is_daemon(monkeypatch):
    monkeypatch.setattr(cfg, "RUN_WORKER_IN_PROCESS", True)
    done = threading.Event()
    t = wl.maybe_start_worker_thread(run=done.set)
    assert t is not None and t.daemon is True
    assert done.wait(timeout=2.0)


def test_default_run_swallows_worker_errors(monkeypatch):
    # _default_run must not raise even if run_worker blows up.
    monkeypatch.setattr("src.scanner.movers_worker.run_worker",
                        lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    wl._default_run()   # should log and return, not raise
