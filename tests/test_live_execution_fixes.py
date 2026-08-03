"""Tests for the live-execution hardening (follow-up to the restore PR).

Covers: tranche allocation invariants, closed-position re-entry, in-flight
position counting, entry-tracking failure rollback, partial-fill handling,
stop resize / cancel on exits, resilient load, and monitor-interval clamping.
All offline — Alpaca order primitives are mocked.
"""

import json
from unittest.mock import MagicMock, patch

import pytest

import src.alpaca.executor as ex
from src.alpaca.executor import (
    RiskGuard,
    _check_entry_fill,
    _check_exits,
    execute_trade_plan,
    start_monitor_thread,
)
from src.alpaca.position_tracker import PositionTracker, _allocate_exit_tranches


def _plan(ticker="AAPL", signal="STRONG_BUY", shares=10, entry=189.40, stop=186.35,
          position_value=1894.0, risk_dollars=30.5):
    return {
        "ticker": ticker, "signal": signal,
        "entry": {"type": "LIMIT", "price": entry},
        "stop_loss": {"price": stop},
        "targets": {
            "T1": {"price": 191.69, "exit_pct": 30},
            "T2": {"price": 196.07, "exit_pct": 40},
            "T3": {"price": 203.84, "exit_pct": 30},
        },
        "position": {"shares": shares, "position_value": position_value,
                     "risk_dollars": risk_dollars},
        "reasoning": ["Golden Cross"],
    }


def _portfolio(equity=50_000.0, buying_power=40_000.0, position_count=0):
    return {"total_equity": equity, "buying_power": buying_power,
            "position_count": position_count}


@pytest.fixture
def tracker(tmp_path):
    t = PositionTracker(store_path=str(tmp_path / "pos.json"))
    t.load()
    return t


# ── Tranche allocation never over-allocates ─────────────────────────────────
def test_tranches_always_sum_to_shares():
    for s in range(0, 51):
        t1, t2, t3 = _allocate_exit_tranches(s)
        assert (t1, t2, t3) >= (0, 0, 0)
        assert t1 + t2 + t3 == s, f"tranches for {s} sum to {t1 + t2 + t3}"
    assert _allocate_exit_tranches(1) == (1, 0, 0)  # not (1, 1, 0)


# ── Closed positions can be re-entered ──────────────────────────────────────
def test_has_open_ignores_closed(tracker):
    tracker.add_position(_plan(), "o1")
    assert tracker.has_open("AAPL") is True
    tracker.mark_closed("AAPL", "STOP")
    assert tracker.has_open("AAPL") is False


def test_riskguard_allows_reentry_after_close(tracker):
    tracker.add_position(_plan(), "o1")
    tracker.mark_closed("AAPL", "STOP")
    ok, reason = RiskGuard().check(_plan(), _portfolio(), tracker)
    assert ok, reason


# ── In-flight tracker positions count against the cap ───────────────────────
def test_riskguard_counts_pending_tracker_positions(tracker):
    for i in range(5):  # 5 pending entries, broker still reports 0 positions
        tracker.add_position(_plan(ticker=f"T{i}"), f"o{i}")
    ok, reason = RiskGuard().check(
        _plan(ticker="NEW"), _portfolio(position_count=0), tracker
    )
    assert not ok and "Max open positions" in reason


# ── Entry placed but tracking fails → entry is cancelled ────────────────────
def test_tracking_failure_cancels_the_entry_order(tracker):
    with patch("src.alpaca.executor.get_portfolio_data", return_value=_portfolio()):
        with patch("src.alpaca.executor.place_entry_order",
                   return_value=MagicMock(id="E1")):
            with patch.object(tracker, "add_position",
                              side_effect=RuntimeError("disk full")):
                with patch("src.alpaca.executor.cancel_order") as mock_cancel:
                    result = execute_trade_plan(_plan(), tracker)
    assert result["ok"] is False
    assert "tracking failed" in result["reason"]
    mock_cancel.assert_called_once_with("E1")


# ── Partial fill: cancel remainder, protect only what filled ────────────────
def test_partial_fill_cancels_remainder_and_sizes_protection(tracker):
    tracker.add_position(_plan(shares=10), "E1")
    partial = MagicMock(status="partially_filled", filled_qty="4", filled_avg_price="189.5")
    after_cancel = MagicMock(status="canceled", filled_qty="6", filled_avg_price="189.5")
    seq = [partial, after_cancel]

    with patch("src.alpaca.executor.get_order", side_effect=lambda oid: seq.pop(0)):
        with patch("src.alpaca.executor.cancel_order", return_value=True) as mock_cancel:
            with patch("src.alpaca.executor.place_stop_order",
                       return_value=MagicMock(id="s")) as mock_stop:
                with patch("src.alpaca.executor.place_limit_sell",
                           return_value=MagicMock(id="t")):
                    _check_entry_fill("AAPL", tracker.get("AAPL"), tracker)

    pos = tracker.get("AAPL")
    assert pos.status == "active"
    assert pos.shares_initial == 6           # only the filled qty
    mock_cancel.assert_called_once()          # unfilled remainder cancelled
    assert mock_stop.call_args[0][1] == 6     # stop sized to filled shares


# ── Stop is resized after a partial exit ────────────────────────────────────
def _add_active(tracker):
    tracker.add_position(_plan(shares=10), "entry-id")
    tracker.mark_entry_filled("AAPL", 189.50, 10)
    tracker.mark_stop_placed("AAPL", "stop-order-id")
    tracker.mark_target_placed("AAPL", "t1", "t1-order-id")
    tracker.mark_target_placed("AAPL", "t2", "t2-order-id")
    tracker.mark_target_placed("AAPL", "t3", "t3-order-id")


def test_stop_resized_after_target_hit(tracker):
    _add_active(tracker)

    def gos(oid):
        return MagicMock(status="filled" if oid == "t1-order-id" else "open")

    with patch("src.alpaca.executor.get_order", side_effect=gos):
        with patch("src.alpaca.executor.cancel_order", return_value=True) as mock_cancel:
            with patch("src.alpaca.executor.place_stop_order",
                       return_value=MagicMock(id="new-stop")) as mock_stop:
                _check_exits("AAPL", tracker.get("AAPL"), tracker)

    pos = tracker.get("AAPL")
    assert pos.t1_hit is True
    mock_cancel.assert_called_once_with("stop-order-id")
    assert mock_stop.call_args[0][1] == pos.shares_open == 7
    assert pos.stop_order_id == "new-stop"


def test_stop_cancelled_when_all_targets_exit(tracker):
    _add_active(tracker)

    def gos(oid):
        return MagicMock(
            status="filled" if oid in ("t1-order-id", "t2-order-id", "t3-order-id") else "open"
        )

    with patch("src.alpaca.executor.get_order", side_effect=gos):
        with patch("src.alpaca.executor.cancel_order", return_value=True) as mock_cancel:
            with patch("src.alpaca.executor.place_stop_order", return_value=MagicMock(id="x")):
                _check_exits("AAPL", tracker.get("AAPL"), tracker)

    assert tracker.get("AAPL").status == "closed"
    mock_cancel.assert_called_once_with("stop-order-id")  # stale stop cancelled


# ── Resilient load ──────────────────────────────────────────────────────────
def test_load_skips_unreadable_records(tmp_path):
    p = str(tmp_path / "pos.json")
    t = PositionTracker(store_path=p)
    t.load()
    t.add_position(_plan(), "o1")
    raw = json.loads(open(p).read())
    raw["positions"]["BROKEN"] = {"ticker": "BROKEN", "unexpected": 1}  # invalid
    open(p, "w").write(json.dumps(raw))

    t2 = PositionTracker(store_path=p)
    t2.load()
    assert t2.get("AAPL") is not None      # good record survived
    assert t2.get("BROKEN") is None        # bad record skipped, not fatal


def test_load_prunes_prior_day_closed_positions(tmp_path):
    p = str(tmp_path / "pos.json")
    t = PositionTracker(store_path=p)
    t.load()
    t.add_position(_plan(), "o1")
    t.mark_closed("AAPL", "STOP")
    raw = json.loads(open(p).read())
    raw["positions"]["AAPL"]["trades_today_date"] = "2020-01-01"  # backdate
    open(p, "w").write(json.dumps(raw))

    t2 = PositionTracker(store_path=p)
    t2.load()
    assert t2.get("AAPL") is None  # stale closed record pruned


# ── Same-day re-entry still counts toward max_trades_per_day ────────────────
def test_reentry_does_not_bypass_daily_trade_limit(tracker):
    tracker.add_position(_plan(), "e1")
    tracker.mark_closed("AAPL", "STOP")
    tracker.add_position(_plan(), "e2")  # same ticker, same day, re-entered
    assert tracker.trades_today() == 2   # two entry events, not one record


# ── Position cap: disjoint broker + pending sets are summed, not max'd ───────
def test_riskguard_sums_disjoint_broker_and_pending(tracker):
    # 4 broker-only positions + 1 tracked pending on a different ticker = 5 → cap.
    tracker.add_position(_plan(ticker="PEND"), "p1")
    portfolio = {
        "total_equity": 50_000.0, "buying_power": 40_000.0,
        "position_count": 4,
        "holdings": [{"ticker": t} for t in ("AAA", "BBB", "CCC", "DDD")],
    }
    ok, reason = RiskGuard().check(_plan(ticker="NEW"), portfolio, tracker)
    assert not ok and "Max open positions" in reason


# ── Stop reconciliation: a failed replacement is retried next cycle ─────────
def test_failed_stop_replacement_is_reconciled_next_cycle(tracker):
    _add_active(tracker)

    def gos(oid):
        return MagicMock(status="filled" if oid == "t1-order-id" else "open")

    # First cycle: cancel succeeds, replacement stop placement FAILS.
    with patch("src.alpaca.executor.get_order", side_effect=gos):
        with patch("src.alpaca.executor.cancel_order", return_value=True):
            with patch("src.alpaca.executor.place_stop_order",
                       side_effect=ex.OrderError("api down")):
                _check_exits("AAPL", tracker.get("AAPL"), tracker)

    pos = tracker.get("AAPL")
    assert pos.t1_hit is True
    assert pos.stop_order_id is None      # protection recorded as missing
    assert pos.shares_open == 7

    # Next cycle: no new target fill, but _ensure_stop re-places the stop.
    with patch("src.alpaca.executor.get_order", return_value=MagicMock(status="open")):
        with patch("src.alpaca.executor.place_stop_order",
                   return_value=MagicMock(id="restored-stop")) as mock_stop:
            _check_exits("AAPL", tracker.get("AAPL"), tracker)

    pos = tracker.get("AAPL")
    assert pos.stop_order_id == "restored-stop"
    assert mock_stop.call_args[0][1] == 7


# ── Non-object store fails safe instead of aborting startup ─────────────────
def test_non_object_store_loads_empty(tmp_path):
    p = tmp_path / "pos.json"
    p.write_text("[1, 2, 3]")  # valid JSON, wrong shape
    t = PositionTracker(store_path=str(p))
    t.load()  # must not raise
    assert t.get_open() == {}


# ── Monitor interval is clamped to a positive floor ─────────────────────────
def test_monitor_interval_clamped_to_floor(monkeypatch):
    monkeypatch.setattr(ex.config, "MONITOR_INTERVAL_MIN", 0)
    captured = {}

    def fake_thread(*args, **kwargs):
        captured["interval_sec"] = kwargs["args"][1]
        return MagicMock()

    monkeypatch.setattr(ex.threading, "Thread", fake_thread)
    start_monitor_thread(MagicMock())
    assert captured["interval_sec"] >= 60
