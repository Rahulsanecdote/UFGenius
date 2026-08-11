"""Tests for the P1.1 look-ahead / staleness guards."""

from __future__ import annotations

from datetime import datetime

import pandas as pd
import pytest

from src.data import lookahead as la


def _frame(times: list[str]) -> pd.DataFrame:
    idx = pd.to_datetime(times)
    n = len(times)
    return pd.DataFrame(
        {"Open": range(n), "High": range(n), "Low": range(n),
         "Close": range(n), "Volume": [10] * n},
        index=idx,
    )


NOW = datetime(2026, 1, 2, 15, 30, 0)


def test_interval_seconds():
    assert la.interval_seconds("1m") == 60
    assert la.interval_seconds("5m") == 300
    assert la.interval_seconds("1h") == 3600
    assert la.interval_seconds("1d") is None  # daily is not intraday


def test_sort_dedupe_orders_and_removes_duplicates():
    df = _frame(["2026-01-02 15:05", "2026-01-02 15:00", "2026-01-02 15:05"])
    out = la.sort_dedupe(df)
    assert list(out.index) == list(pd.to_datetime(["2026-01-02 15:00", "2026-01-02 15:05"]))
    # keep="last": the later duplicate row wins.
    assert out.loc[pd.Timestamp("2026-01-02 15:05"), "Open"] == 2


def test_drop_future_bars_removes_bars_after_now():
    df = _frame(["2026-01-02 15:00", "2026-01-02 15:25", "2026-01-02 16:00"])
    out = la.drop_future_bars(df, now=NOW)
    assert list(out.index) == list(pd.to_datetime(["2026-01-02 15:00", "2026-01-02 15:25"]))


def test_drop_future_bars_tolerance_absorbs_clock_skew():
    df = _frame(["2026-01-02 15:30:03"])  # 3s ahead of NOW
    assert len(la.drop_future_bars(df, now=NOW, tolerance_sec=5)) == 1  # within grace
    assert len(la.drop_future_bars(df, now=NOW, tolerance_sec=0)) == 0  # strict


def test_bar_age_seconds():
    df = _frame(["2026-01-02 15:00", "2026-01-02 15:25"])
    assert la.bar_age_seconds(df, now=NOW) == 300.0  # 15:30 - 15:25


def test_as_of_clamps_to_cutoff():
    df = _frame(["2026-01-02 15:00", "2026-01-02 15:05", "2026-01-02 15:10"])
    assert len(la.as_of(df, "2026-01-02 15:05")) == 2
    assert len(la.as_of(df, datetime(2026, 1, 2, 14, 0))) == 0


def test_is_stale():
    fresh = _frame(["2026-01-02 15:25"])   # 5 min old
    stale = _frame(["2026-01-02 14:00"])   # 90 min old
    assert la.is_stale(fresh, "5m", max_staleness_intervals=3, now=NOW) is False  # 300s < 900s
    assert la.is_stale(stale, "5m", max_staleness_intervals=3, now=NOW) is True
    # daily/unknown interval is not evaluated here
    assert la.is_stale(stale, "1d", max_staleness_intervals=3, now=NOW) is False


def test_is_stale_empty_frame_is_stale():
    assert la.is_stale(pd.DataFrame(), "5m", max_staleness_intervals=3, now=NOW) is True


def test_sanitize_intraday_combines_guards():
    df = _frame(["2026-01-02 15:05", "2026-01-02 15:00", "2026-01-02 15:05", "2026-01-02 16:00"])
    out = la.sanitize_intraday(df, now=NOW, future_tolerance_sec=5)
    # ordered, de-duplicated, and the 16:00 future bar dropped.
    assert list(out.index) == list(pd.to_datetime(["2026-01-02 15:00", "2026-01-02 15:05"]))


def test_guards_tolerate_empty_and_non_datetime_index():
    empty = pd.DataFrame()
    assert la.sort_dedupe(empty).empty
    assert la.drop_future_bars(empty, now=NOW).empty
    assert la.bar_age_seconds(empty, now=NOW) is None
    plain = pd.DataFrame({"Close": [1, 2]})  # RangeIndex, not datetime
    assert la.sort_dedupe(plain).equals(plain)


def test_tz_aware_index_is_handled():
    idx = pd.to_datetime(["2026-01-02 15:00", "2026-01-02 16:00"]).tz_localize("UTC")
    df = pd.DataFrame({"Open": [1, 2], "High": [1, 2], "Low": [1, 2],
                       "Close": [1, 2], "Volume": [10, 10]}, index=idx)
    out = la.drop_future_bars(df, now=NOW)  # 16:00 UTC is in the future vs NOW
    assert len(out) == 1
