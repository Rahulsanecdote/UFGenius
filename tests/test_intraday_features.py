"""Tests for the P1.3 intraday features (VWAP, opening range, rel-vol, ATR)."""

from __future__ import annotations

import pandas as pd
import pytest

from src.technical import intraday_features as f


def _session(prices, vols, day="2026-01-02", start="09:30"):
    n = len(prices)
    idx = pd.date_range(f"{day} {start}", periods=n, freq="5min")
    return pd.DataFrame(
        {"Open": prices, "High": [p + 0.2 for p in prices], "Low": [p - 0.2 for p in prices],
         "Close": prices, "Volume": vols},
        index=idx,
    )


def test_current_session_slices_latest_day():
    s1 = _session([100] * 5, [1000] * 5, day="2026-01-01")
    s2 = _session([101] * 5, [1000] * 5, day="2026-01-02")
    df = pd.concat([s1, s2])
    out = f.current_session_bars(df)
    assert len(out) == 5
    assert all(ts.date().isoformat() == "2026-01-02" for ts in out.index)


def test_vwap_is_volume_weighted_over_session():
    # Two bars: typical prices 10 and 20, volumes 1 and 3 → VWAP = (10*1+20*3)/4.
    df = pd.DataFrame(
        {"Open": [10, 20], "High": [10, 20], "Low": [10, 20], "Close": [10, 20], "Volume": [1, 3]},
        index=pd.date_range("2026-01-02 09:30", periods=2, freq="5min"),
    )
    assert f.vwap(df) == pytest.approx((10 * 1 + 20 * 3) / 4)


def test_vwap_none_on_zero_volume():
    df = _session([100, 101], [0, 0])
    assert f.vwap(df) is None


def test_opening_range_first_n_minutes():
    # 30-min window at 5m bars = first 6 bars.
    prices = [100, 101, 102, 99, 103, 98] + [110] * 6  # later bars must not widen the range
    orange = f.opening_range(_session(prices, [1000] * 12), minutes=30)
    assert orange["bars"] == 6
    assert orange["high"] == pytest.approx(103 + 0.2)  # high = close+0.2 on the 103 bar
    assert orange["low"] == pytest.approx(98 - 0.2)


def test_relative_volume_excludes_current_bar():
    df = _session([100] * 5, [1000, 1000, 1000, 1000, 4000])
    assert f.relative_volume(df) == pytest.approx(4.0)  # 4000 / mean(1000)


def test_intraday_atr_positive_and_none_when_short():
    df = _session([100 + (i % 3) for i in range(30)], [1000] * 30)
    atr = f.intraday_atr(df, period=14)
    assert atr is not None and atr > 0
    assert f.intraday_atr(_session([100] * 5, [1000] * 5), period=14) is None  # too few bars


def test_features_tolerate_empty():
    empty = pd.DataFrame()
    assert f.vwap(empty) is None
    assert f.opening_range(empty) is None
    assert f.relative_volume(empty) is None
    assert f.intraday_atr(empty) is None
