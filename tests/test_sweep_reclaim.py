"""Tests for the sweep-reclaim reversal entry (liquidity-grab timing)."""

from __future__ import annotations

import pandas as pd
import pytest

from src.signals.sweep_reclaim import build_sweep_reclaim_plan, evaluate_sweep_reclaim
from src.utils import config


# ── session-frame builder ─────────────────────────────────────────────────────
def _session(lows, closes, vols, highs=None, opens=None, date="2023-06-01"):
    """Build a single-session intraday OHLCV frame (naive-UTC 5m index)."""
    n = len(closes)
    idx = pd.date_range(f"{date} 13:30", periods=n, freq="5min")  # naive UTC
    highs = highs or [max(c, l) * 1.001 for c, l in zip(closes, lows)]
    opens = opens or list(closes)
    return pd.DataFrame(
        {"Open": opens, "High": highs, "Low": lows, "Close": closes, "Volume": vols},
        index=idx,
    )


def _sweep_reclaim_frame(*, last_close=9.95, sweep_low=9.60, last_vol=1_500_000,
                         base_vol=500_000, swing_low=9.80, n_base=22):
    """A frame that sweeps `swing_low` on the 2nd-to-last bar then reclaims it.

    Base bars hover so their min Low is `swing_low`; the recent 2-bar window dips
    to `sweep_low` (below the level) and the last bar closes at `last_close`
    (above the level) on `last_vol`.
    """
    # base bars: lows never below swing_low; the minimum is exactly swing_low.
    base_closes = [10.0] * n_base
    base_lows = [swing_low + 0.05] * n_base
    base_lows[10] = swing_low          # the swing low itself, inside the lookback
    base_vols = [base_vol] * n_base
    # recent window (2 bars): a sweep bar then the reclaim bar.
    closes = base_closes + [9.70, last_close]
    lows = base_lows + [sweep_low, sweep_low + 0.30]
    vols = base_vols + [base_vol, last_vol]
    return _session(lows, closes, vols)


# ── firing ────────────────────────────────────────────────────────────────────
def test_sweep_reclaim_fires_strong_buy():
    d = evaluate_sweep_reclaim(_sweep_reclaim_frame())
    assert d["signal"] == "STRONG_BUY" and d["enter"]
    s = d["sweep"]
    assert s["sweep_low"] == pytest.approx(9.60)
    assert s["swing_low"] == pytest.approx(9.80)
    assert s["swept"] and s["reclaimed"] and s["volume_ok"]
    assert s["stop_hint"] < s["sweep_low"]          # stop below the swept wick


def test_thin_volume_downgrades_to_buy():
    # Same setup but the reclaim bar carries no extra volume → BUY, not STRONG.
    d = evaluate_sweep_reclaim(_sweep_reclaim_frame(last_vol=500_000))
    assert d["signal"] == "BUY" and d["enter"]
    assert d["sweep"]["volume_ok"] is False


def test_require_volume_holds_on_thin(monkeypatch):
    monkeypatch.setattr(config, "SWEEP_REQUIRE_VOLUME", True)
    d = evaluate_sweep_reclaim(_sweep_reclaim_frame(last_vol=500_000))
    assert d["signal"] == "HOLD" and not d["enter"]
    assert any("thin volume" in r for r in d["reasons"])


# ── non-setups ────────────────────────────────────────────────────────────────
def test_no_sweep_holds():
    # Recent lows never pierce the swing low → no liquidity grab.
    f = _sweep_reclaim_frame(sweep_low=9.90)   # 9.90 > swing_low 9.80 → not swept
    d = evaluate_sweep_reclaim(f)
    assert d["signal"] == "HOLD" and not d["enter"]
    assert any("No sweep" in r for r in d["reasons"])


def test_swept_but_not_reclaimed_holds():
    # Dips below the level and STAYS below (close under swing low) → breakdown.
    d = evaluate_sweep_reclaim(_sweep_reclaim_frame(last_close=9.70))
    assert d["signal"] == "HOLD" and not d["enter"]
    assert any("not reclaimed" in r for r in d["reasons"])


def test_over_extended_reclaim_holds():
    # Reclaim closes far above the swept level → chasing, R:R too poor.
    d = evaluate_sweep_reclaim(_sweep_reclaim_frame(last_close=10.60))  # ~10% above 9.80
    assert d["signal"] == "HOLD" and not d["enter"]
    assert any("over-extended" in r for r in d["reasons"])


def test_insufficient_bars_holds():
    d = evaluate_sweep_reclaim(_session([10, 10, 10], [10, 10, 10], [1000, 1000, 1000]))
    assert d["signal"] == "HOLD" and not d["enter"]
    assert any("insufficient" in r for r in d["reasons"])


def test_never_raises_on_empty():
    d = evaluate_sweep_reclaim(pd.DataFrame())
    assert d["signal"] == "HOLD" and not d["enter"]


# ── plan building ─────────────────────────────────────────────────────────────
def test_build_plan_places_stop_below_swept_wick():
    f = _sweep_reclaim_frame()
    d = evaluate_sweep_reclaim(f)
    plan = build_sweep_reclaim_plan("AAA", f, d, account_size=100_000)
    assert plan.get("source") == "sweep_reclaim"
    assert "error" not in plan and not plan.get("skip"), plan
    stop = plan["stop_loss"]["price"]
    assert stop < d["sweep"]["sweep_low"]           # stop sits below the swept low
    assert plan["position"]["shares"] >= 1
    assert "sweep-reclaim" in plan["stop_loss"]["method"]
    assert plan["sweep"]["swing_low"] == pytest.approx(9.80)


def test_build_plan_skips_non_entry():
    hold = {"signal": "HOLD", "enter": False, "sweep": {}}
    plan = build_sweep_reclaim_plan("AAA", pd.DataFrame(), hold)
    assert plan["skip"] and "not an entry" in plan["reason"]
