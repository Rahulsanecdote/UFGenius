"""Tests for the sweep-reclaim reversal entry (liquidity-grab timing)."""

from __future__ import annotations

import pandas as pd
import pytest

from src.signals.sweep_reclaim import (
    build_sweep_reclaim_plan,
    evaluate_sweep_reclaim,
    sweep_reclaim_present,
)
from src.utils import config

# The frame's last bar sits here; producer tests pass a `now` just after it so the
# staleness guard in scan_intraday doesn't reject the (fixed-date) fixture.
_FRAME_DATE = "2023-06-01"
_FRAME_LAST = pd.Timestamp("2023-06-01 15:26")   # 24 bars from 13:30 @ 5m → 15:25


# ── session-frame builder ─────────────────────────────────────────────────────
def _session(lows, closes, vols, highs=None, opens=None, date=_FRAME_DATE):
    """Build a single-session intraday OHLCV frame (naive-UTC 5m index)."""
    n = len(closes)
    idx = pd.date_range(f"{date} 13:30", periods=n, freq="5min")  # naive UTC
    highs = highs or [max(c, l) * 1.001 for c, l in zip(closes, lows)]
    opens = opens or list(closes)
    return pd.DataFrame(
        {"Open": opens, "High": highs, "Low": lows, "Close": closes, "Volume": vols},
        index=idx,
    )


def _sweep_reclaim_frame(*, swing_low=9.80, sweep_low=9.60, last_close=9.95,
                         base_close=10.0, last_vol=1_500_000, base_vol=500_000,
                         n_base=22):
    """A frame that sweeps `swing_low` on the 2nd-to-last bar then reclaims it.

    Base bars hover so their min Low is `swing_low`; the recent 2-bar window dips
    to `sweep_low` (below the level) and the last bar closes at `last_close`
    (above the level) on `last_vol`.
    """
    base_closes = [base_close] * n_base
    base_lows = [swing_low + 0.05] * n_base
    base_lows[10] = swing_low          # the swing low itself, inside the lookback
    base_vols = [base_vol] * n_base
    # recent window (2 bars): a sweep bar (dips to sweep_low) then the reclaim bar.
    closes = base_closes + [swing_low - 0.05, last_close]
    lows = base_lows + [sweep_low, sweep_low + 0.02]
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


def test_build_plan_skips_shallow_reclaim_geometry():
    # Razor-thin reclaim: the planner's 0.2% entry discount would drop entry to/
    # below the swept-low stop → no valid stop-below-entry. Must SKIP, not fall
    # back to a generic ATR stop (Codex P2).
    f = _sweep_reclaim_frame(swing_low=100.0, sweep_low=99.95, last_close=100.01,
                             base_close=100.2)
    d = evaluate_sweep_reclaim(f)
    assert d["enter"]                                   # it IS a graded entry...
    plan = build_sweep_reclaim_plan("AAA", f, d, account_size=100_000)
    assert plan.get("skip") and "too shallow" in plan["reason"]  # ...but geometry is degenerate
    assert "source" not in plan                         # never produced a plan


# ── producer prefilter (sweep candidates get enqueued) ────────────────────────
def test_sweep_reclaim_present_true_on_setup():
    assert sweep_reclaim_present(_sweep_reclaim_frame()) is True


def test_sweep_reclaim_present_false_without_sweep():
    assert sweep_reclaim_present(_sweep_reclaim_frame(sweep_low=9.90)) is False


def test_producer_enqueues_sweep_candidate_when_enabled(monkeypatch):
    from src.scanner import intraday_scan

    monkeypatch.setattr(config, "SWEEP_RECLAIM_ENABLED", True)
    f = _sweep_reclaim_frame()
    cands = intraday_scan.scan_intraday(
        ["AAA"], now=_FRAME_LAST, fetch=lambda t, interval=None: f
    )
    assert "sweep" in {c.kind for c in cands}


def test_producer_no_sweep_candidate_when_disabled(monkeypatch):
    from src.scanner import intraday_scan

    monkeypatch.setattr(config, "SWEEP_RECLAIM_ENABLED", False)
    f = _sweep_reclaim_frame()
    cands = intraday_scan.scan_intraday(
        ["AAA"], now=_FRAME_LAST, fetch=lambda t, interval=None: f
    )
    assert "sweep" not in {c.kind for c in cands}


# ── level-anchored variant + entry window (opt-in; defaults byte-preserving) ──
#
# Fixtures here are ET-aware: naive-UTC index, June dates ⇒ ET = UTC−4, so
# 13:30 UTC = 09:30 ET. Compact detector params are monkeypatched so multi-day
# frames stay small.

from src.signals.sweep_reclaim import _anchored_levels


def _bar_rows(rows):
    """Frame from (naive-UTC 'YYYY-MM-DD HH:MM', low, close, volume) rows."""
    idx = [pd.Timestamp(t) for t, *_ in rows]
    lows = [r[1] for r in rows]
    closes = [r[2] for r in rows]
    vols = [r[3] for r in rows]
    highs = [max(c, l) + 0.01 for c, l in zip(closes, lows)]
    return pd.DataFrame(
        {"Open": closes, "High": highs, "Low": lows, "Close": closes, "Volume": vols},
        index=pd.DatetimeIndex(idx),
    )


def _compact(monkeypatch, *, anchors=None, win=("", "")):
    monkeypatch.setattr(config, "SWEEP_LOOKBACK_BARS", 4)
    monkeypatch.setattr(config, "SWEEP_RECLAIM_WINDOW_BARS", 2)
    monkeypatch.setattr(config, "SWEEP_MIN_SESSION_BARS", 6)
    monkeypatch.setattr(config, "SWEEP_LEVEL_ANCHORS", anchors or [])
    monkeypatch.setattr(config, "SWEEP_ENTRY_WINDOW_START", win[0])
    monkeypatch.setattr(config, "SWEEP_ENTRY_WINDOW_END", win[1])


def _pdl_frame():
    """Prior-day RTH low 9.50; today swing ref lows 9.80; wick 9.45; close 9.55.

    Close reclaims the PDL (9.50) but NOT the swing low (9.80) — so the swing
    path reads breakdown while the PDL anchor reads a valid sweep-reclaim.
    """
    rows = [
        # previous session (RTH, 2023-05-31): establishes PDL = 9.50
        ("2023-05-31 13:30", 9.55, 9.90, 500_000),
        ("2023-05-31 15:00", 9.50, 9.85, 500_000),
        ("2023-05-31 19:55", 9.60, 9.80, 500_000),
        # today: 4 ref bars (lookback), lows at 9.80
        ("2023-06-01 13:30", 9.80, 10.00, 500_000),
        ("2023-06-01 13:35", 9.82, 10.00, 500_000),
        ("2023-06-01 13:40", 9.80, 10.00, 500_000),
        ("2023-06-01 13:45", 9.81, 10.00, 500_000),
        # recent window (2 bars): sweep to 9.45, reclaim close 9.55
        ("2023-06-01 13:50", 9.45, 9.47, 500_000),
        ("2023-06-01 13:55", 9.46, 9.55, 1_500_000),
    ]
    return _bar_rows(rows)


class TestLevelAnchors:
    def test_defaults_report_swing_source(self):
        d = evaluate_sweep_reclaim(_sweep_reclaim_frame())
        assert d["sweep"]["level_source"] == "swing"

    def test_pdl_only_reclaim_holds_without_anchors(self, monkeypatch):
        _compact(monkeypatch)
        d = evaluate_sweep_reclaim(_pdl_frame())
        assert d["signal"] == "HOLD"
        assert any("not reclaimed" in r for r in d["reasons"])

    def test_pdl_anchor_fires_and_discloses_source(self, monkeypatch):
        _compact(monkeypatch, anchors=["pdl"])
        d = evaluate_sweep_reclaim(_pdl_frame())
        assert d["enter"]
        assert d["sweep"]["level_source"] == "pdl"
        assert d["sweep"]["swing_low"] == pytest.approx(9.50)  # holds the PDL value
        assert any("PDL level" in r for r in d["reasons"])

    def test_pml_anchor_from_todays_premarket(self, monkeypatch):
        _compact(monkeypatch, anchors=["pml"])
        rows = [
            # today's pre-market (08:00–09:25 ET = 12:00–13:25 UTC): PML = 9.40
            ("2023-06-01 12:00", 9.42, 9.60, 100_000),
            ("2023-06-01 12:30", 9.40, 9.58, 100_000),
            # RTH ref bars, lows 9.80
            ("2023-06-01 13:30", 9.80, 10.00, 500_000),
            ("2023-06-01 13:35", 9.82, 10.00, 500_000),
            ("2023-06-01 13:40", 9.80, 10.00, 500_000),
            ("2023-06-01 13:45", 9.81, 10.00, 500_000),
            # recent window: sweep to 9.35, reclaim close 9.45 (> PML, < swing)
            ("2023-06-01 13:50", 9.35, 9.38, 500_000),
            ("2023-06-01 13:55", 9.37, 9.45, 1_500_000),
        ]
        d = evaluate_sweep_reclaim(_bar_rows(rows))
        assert d["enter"]
        assert d["sweep"]["level_source"] == "pml"
        assert d["sweep"]["swing_low"] == pytest.approx(9.40)

    def test_highest_qualifying_level_wins(self, monkeypatch):
        # Wick 9.45 sweeps BOTH the PDL (9.50) and the swing (9.80); close 9.85
        # reclaims both. The higher level (swing) is the stricter reclaim and
        # must be chosen.
        _compact(monkeypatch, anchors=["pdl"])
        frame = _pdl_frame().copy()
        frame.iloc[-1, frame.columns.get_loc("Close")] = 9.85
        d = evaluate_sweep_reclaim(frame)
        assert d["enter"]
        assert d["sweep"]["level_source"] == "swing"
        assert d["sweep"]["swing_low"] == pytest.approx(9.80)

    def test_level_established_before_the_recent_window(self, monkeypatch):
        # A pre-market low printed INSIDE the recent window must not define the
        # PML it then "sweeps" — the level would be self-referential.
        monkeypatch.setattr(config, "SWEEP_LEVEL_ANCHORS", ["pml"])
        rows = [
            ("2023-06-01 12:00", 9.60, 9.62, 100_000),   # PM, before window
            ("2023-06-01 13:00", 9.35, 9.38, 100_000),   # PM, inside recent window
            ("2023-06-01 13:05", 9.37, 9.45, 100_000),   # PM, inside recent window
        ]
        levels = _anchored_levels(_bar_rows(rows), window=2)
        assert levels["pml"] == pytest.approx(9.60)      # not 9.35

    def test_prefilter_is_a_superset_with_anchors(self, monkeypatch):
        _compact(monkeypatch, anchors=["pdl"])
        assert sweep_reclaim_present(_pdl_frame()) is True
        _compact(monkeypatch, anchors=[])
        assert sweep_reclaim_present(_pdl_frame()) is False


class TestEntryWindow:
    def test_outside_window_holds_with_reason(self, monkeypatch):
        # Standard fixture's last bar is 15:25 UTC = 11:25 ET — outside 09:30-10:30.
        _compact(monkeypatch, win=("09:30", "10:30"))
        d = evaluate_sweep_reclaim(_sweep_reclaim_frame())
        assert d["signal"] == "HOLD"
        assert any("outside entry window" in r for r in d["reasons"])

    def test_inside_window_is_unchanged(self, monkeypatch):
        # Same fixture, window that contains 11:25 ET → decision as with no window.
        _compact(monkeypatch, win=("11:00", "12:00"))
        monkeypatch.setattr(config, "SWEEP_LOOKBACK_BARS", 15)
        monkeypatch.setattr(config, "SWEEP_RECLAIM_WINDOW_BARS", 2)
        monkeypatch.setattr(config, "SWEEP_MIN_SESSION_BARS", 20)
        d = evaluate_sweep_reclaim(_sweep_reclaim_frame())
        assert d["signal"] == "STRONG_BUY"

    def test_malformed_window_is_ignored_not_fatal(self, monkeypatch):
        _compact(monkeypatch, win=("9:3x", "10:30"))
        monkeypatch.setattr(config, "SWEEP_LOOKBACK_BARS", 15)
        monkeypatch.setattr(config, "SWEEP_RECLAIM_WINDOW_BARS", 2)
        monkeypatch.setattr(config, "SWEEP_MIN_SESSION_BARS", 20)
        d = evaluate_sweep_reclaim(_sweep_reclaim_frame())
        assert d["signal"] == "STRONG_BUY"  # window off, entry unaffected
