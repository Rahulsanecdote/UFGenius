"""Tests for the P2.3 per-signal outcome attribution."""

from __future__ import annotations

from src.observability.attribution import attribution_from_tracker, signal_attribution


def _trade(signal, pnl, return_pct):
    return {"signal": signal, "pnl": pnl, "return_pct": return_pct}


def test_groups_by_signal_label():
    trades = [
        _trade("STRONG_BUY", 100.0, 5.0),
        _trade("STRONG_BUY", -40.0, -2.0),
        _trade("BUY", 20.0, 1.0),
    ]
    out = signal_attribution(trades)
    assert set(out["by_signal"].keys()) == {"STRONG_BUY", "BUY"}
    sb = out["by_signal"]["STRONG_BUY"]
    assert sb["trades"] == 2
    assert sb["wins"] == 1
    assert sb["losses"] == 1
    assert sb["win_rate"] == 0.5
    assert sb["total_pnl"] == 60.0
    assert sb["avg_pnl"] == 30.0
    assert sb["avg_return_pct"] == 1.5


def test_overall_aggregates_all():
    trades = [
        _trade("STRONG_BUY", 100.0, 5.0),
        _trade("BUY", -20.0, -1.0),
        _trade("WEAK_BUY", 10.0, 0.5),
    ]
    out = signal_attribution(trades)
    o = out["overall"]
    assert o["trades"] == 3
    assert o["wins"] == 2
    assert o["total_pnl"] == 90.0


def test_empty_trades():
    out = signal_attribution([])
    assert out["overall"]["trades"] == 0
    assert out["overall"]["win_rate"] is None
    assert out["by_signal"] == {}


def test_skips_non_finite_pnl_and_missing():
    trades = [
        _trade("BUY", float("nan"), 1.0),
        _trade("BUY", None, 1.0),
        {"signal": "BUY"},  # no pnl
        _trade("BUY", 5.0, 1.0),
    ]
    out = signal_attribution(trades)
    assert out["by_signal"]["BUY"]["trades"] == 1


def test_missing_label_becomes_unknown():
    out = signal_attribution([{"pnl": 1.0, "return_pct": 0.5}])
    assert "UNKNOWN" in out["by_signal"]


def test_attribution_from_tracker_uses_get_trades():
    class _Tracker:
        def get_trades(self, paper_only=False):
            return [_trade("BUY", 10.0, 1.0)]

    out = attribution_from_tracker(_Tracker())
    assert out["overall"]["trades"] == 1


def test_attribution_from_tracker_tolerates_failure():
    class _BadTracker:
        def get_trades(self, paper_only=False):
            raise RuntimeError("nope")

    out = attribution_from_tracker(_BadTracker())
    assert out["overall"]["trades"] == 0
