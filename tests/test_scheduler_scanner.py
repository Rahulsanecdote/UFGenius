"""Tests for scheduler wiring (audit M12, L4) and gap-scanner bar floor (L8)."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd

import bot
from src.scanner import gap_scanner


# ── M12: every configured slot is wired ──────────────────────────────────────

def _mock_schedule(monkeypatch, zones: list | None = None):
    at_times: list[str] = []
    mock = MagicMock()

    def _at(time_str, tz=None):
        at_times.append(time_str)
        if zones is not None:
            zones.append(tz)
        return MagicMock()

    mock.every.return_value.day.at.side_effect = _at
    monkeypatch.setattr(bot, "schedule", mock)
    return at_times


def test_all_config_slots_are_wired_including_intraday(monkeypatch):
    at_times = _mock_schedule(monkeypatch)
    sched = {
        "pre_market": "06:00", "market_open": "09:25",
        "intraday_1": "11:00", "intraday_2": "14:00",
        "post_market": "16:30", "overnight": "21:00",
    }
    wired = bot._wire_schedule(sched, lambda: None)
    assert len(wired) == 6
    assert "11:00" in at_times and "14:00" in at_times  # previously dropped


def test_invalid_times_are_skipped_not_fatal(monkeypatch):
    at_times = _mock_schedule(monkeypatch)
    wired = bot._wire_schedule({"good": "09:30", "bad": "25:99", "worse": None}, lambda: None)
    assert wired == ["good=09:30"]
    assert at_times == ["09:30"]


def test_empty_schedule_falls_back_to_defaults(monkeypatch):
    at_times = _mock_schedule(monkeypatch)
    wired = bot._wire_schedule({}, lambda: None)
    assert len(wired) == 4
    assert "06:00" in at_times


def test_slots_are_wired_in_market_time_not_host_time(monkeypatch):
    """`market_open: 09:25` means 09:25 in New York. Read as host-local time on
    a UTC container, every slot fired four hours early under EDT."""
    zones: list = []
    _mock_schedule(monkeypatch, zones)
    bot._wire_schedule({"market_open": "09:25", "intraday_2": "14:00"}, lambda: None)
    assert zones == ["America/New_York", "America/New_York"]


def test_a_real_job_lands_on_the_new_york_wall_clock():
    """Against the real `schedule` library, not a mock: the next run of a
    09:25 slot is 09:25 in New York whatever timezone the host is in."""
    import schedule as real
    from zoneinfo import ZoneInfo

    sched = real.Scheduler()
    job = sched.every().day.at("09:25", bot._SCHEDULE_TZ).do(lambda: None)
    nxt = job.next_run
    # next_run is naive host-local time; pin it to the host zone, then convert.
    local = nxt.astimezone() if nxt.tzinfo else nxt.replace(tzinfo=datetime.now().astimezone().tzinfo)
    ny = local.astimezone(ZoneInfo("America/New_York"))
    assert (ny.hour, ny.minute) == (9, 25)


# ── L4: weekend gate ─────────────────────────────────────────────────────────

def test_weekend_is_not_a_trading_day():
    assert not bot._is_trading_day(datetime(2026, 8, 8))   # Saturday
    assert not bot._is_trading_day(datetime(2026, 8, 9))   # Sunday
    assert bot._is_trading_day(datetime(2026, 8, 10))      # Monday


# ── L8: gap scanner requires a real 20-day sample ────────────────────────────

def _gap_frame(rows: int) -> pd.DataFrame:
    dates = pd.date_range("2024-01-01", periods=rows, freq="D")
    close = np.full(rows, 100.0)
    open_ = np.full(rows, 100.0)
    volume = np.full(rows, 1_000_000.0)
    open_[-1] = 110.0     # 10% gap up on the last bar
    close[-1] = 108.0
    volume[-1] = 5_000_000.0
    return pd.DataFrame(
        {"Open": open_, "High": close * 1.01, "Low": close * 0.99,
         "Close": close, "Volume": volume},
        index=dates,
    )


def test_gap_scan_skips_thin_history(monkeypatch):
    monkeypatch.setattr(gap_scanner, "fetch_ohlcv", lambda *a, **k: _gap_frame(10))
    assert gap_scanner.scan_for_gaps(["AAA"]) == []  # 10 bars < 21 floor


def test_gap_scan_reports_with_enough_bars_and_wider_window(monkeypatch):
    calls = {}

    def _fetch(ticker, period=None, interval=None):
        calls["period"] = period
        return _gap_frame(30)

    monkeypatch.setattr(gap_scanner, "fetch_ohlcv", _fetch)
    results = gap_scanner.scan_for_gaps(["AAA"])
    assert calls["period"] == "3mo"  # window widened so the 21-bar floor is satisfiable
    assert len(results) == 1
    assert results[0]["direction"] == "UP"
    assert results[0]["gap_pct"] == 10.0
    # baseline = 20 prior bars @ 1M (today's 5M spike excluded) → 5.0x, not the
    # ~4.17x an inclusive tail(20) would report
    assert results[0]["volume_ratio"] == 5.0
