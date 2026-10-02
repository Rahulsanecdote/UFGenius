"""Which pre-filter survivors get analysed.

run_daily_scan analyses only the first `max_signals` (15) survivors, and the
pre-filter used to return them in universe order — alphabetical for SP500. On
the paper trader's first scheduled scan (2026-10-01 21:00 ET) 90 names passed
and the 15 analysed were A, ABBV, ABNB … BIIB, BLK: a ticker's NAME decided
whether it could ever trade. Survivors are now ranked by relative volume, the
pre-filter's own qualifying measure.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

import src.utils.config as cfg
from src.scanner import daily_scan


def _frame(last_volume_mult: float) -> pd.DataFrame:
    """120 bars with a mid-range RSI and a last-bar volume spike of `mult`×."""
    idx = pd.date_range("2026-03-02", periods=120, freq="B")
    steps = np.where(np.arange(120) % 2 == 0, 1.0, -0.8)   # RSI settles mid-range
    close = 100 + np.cumsum(steps)
    vol = np.full(120, 1_000_000.0)
    vol[-1] = 1_000_000.0 * last_volume_mult
    return pd.DataFrame({"Open": close, "High": close + 1, "Low": close - 1,
                         "Close": close, "Volume": vol}, index=idx)


# Alphabetical order is the reverse of RVOL order, so the two rankings disagree
# on every position.
FRAMES = {"AAA": _frame(1.5), "BBB": _frame(2.0), "CCC": _frame(3.0), "ZZZ": _frame(6.0)}


@pytest.fixture
def universe(monkeypatch):
    monkeypatch.setattr(daily_scan, "fetch_ohlcv_batch",
                        lambda tickers, period="1y", max_workers=8: {t: FRAMES[t] for t in tickers})
    return list(FRAMES)


def test_the_frames_really_pass_the_prefilter(universe):
    passed = [t for t, _ in daily_scan.technical_pre_filter(universe)]
    assert sorted(passed) == sorted(universe)


def test_survivors_are_ranked_strongest_relative_volume_first(universe, monkeypatch):
    monkeypatch.setattr(cfg, "SCAN_CANDIDATE_RANKING", "rvol")
    assert [t for t, _ in daily_scan.technical_pre_filter(universe)] == ["ZZZ", "CCC", "BBB", "AAA"]


def test_universe_order_is_still_available(universe, monkeypatch):
    monkeypatch.setattr(cfg, "SCAN_CANDIDATE_RANKING", "universe")
    assert [t for t, _ in daily_scan.technical_pre_filter(universe)] == ["AAA", "BBB", "CCC", "ZZZ"]


def test_the_shape_callers_see_is_unchanged(universe):
    out = daily_scan.technical_pre_filter(universe)
    assert all(isinstance(t, str) and isinstance(df, pd.DataFrame) for t, df in out)
    assert all(len(item) == 2 for item in out)


def test_the_analysis_cap_now_keeps_the_strongest_not_the_earliest_names(universe, monkeypatch):
    monkeypatch.setattr(cfg, "SCAN_CANDIDATE_RANKING", "rvol")
    analysed = []
    monkeypatch.setattr(daily_scan, "get_universe", lambda name=None: universe)
    monkeypatch.setattr(daily_scan, "detect_market_regime",
                        lambda: {"regime": "MILD_BULL", "regime_score": 40, "vix": 18.0,
                                 "strategy": "test"})
    monkeypatch.setattr(daily_scan, "_analyze_ticker",
                        lambda t, regime, account_size, df: analysed.append(t) or None)
    daily_scan.run_daily_scan(account_size=10_000, max_signals=2, pre_filter=True)
    assert sorted(analysed) == ["CCC", "ZZZ"]


def test_the_config_value_is_validated():
    import importlib
    import os
    import src.utils.config as live
    old = os.environ.get("SCAN_CANDIDATE_RANKING")
    try:
        os.environ["SCAN_CANDIDATE_RANKING"] = "alphabetical-please"
        importlib.reload(live)
        assert live.SCAN_CANDIDATE_RANKING == "rvol"
        del os.environ["SCAN_CANDIDATE_RANKING"]
        importlib.reload(live)
        assert live.SCAN_CANDIDATE_RANKING == "rvol"      # the shipped default
    finally:
        if old is not None:
            os.environ["SCAN_CANDIDATE_RANKING"] = old
        importlib.reload(live)
