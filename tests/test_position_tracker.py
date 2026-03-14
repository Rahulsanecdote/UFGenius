"""Unit tests for src/alpaca/position_tracker.py."""

import json
import os
from datetime import date

import pytest

from src.alpaca.position_tracker import LivePosition, PositionTracker


# ─── Fixtures ────────────────────────────────────────────────────────────── #


def _sample_plan(ticker="AAPL", shares=10, entry=189.40, stop=186.35):
    return {
        "ticker": ticker,
        "signal": "STRONG_BUY",
        "entry": {"type": "LIMIT", "price": entry},
        "stop_loss": {"price": stop},
        "targets": {
            "T1": {"price": 191.69, "exit_pct": 30},
            "T2": {"price": 196.07, "exit_pct": 40},
            "T3": {"price": 203.84, "exit_pct": 30},
        },
        "position": {
            "shares": shares,
            "position_value": entry * shares,
            "risk_dollars": (entry - stop) * shares,
        },
    }


@pytest.fixture
def store_path(tmp_path):
    return str(tmp_path / "positions.json")


@pytest.fixture
def tracker(store_path):
    t = PositionTracker(store_path=store_path)
    t.load()
    return t


# ─── add_position ────────────────────────────────────────────────────────── #


def test_add_position_creates_pending_fill_entry(tracker):
    pos = tracker.add_position(_sample_plan(), "order-001")
    assert pos.status == "pending_fill"
    assert pos.ticker == "AAPL"
    assert pos.entry_order_id == "order-001"
    assert pos.fill_price is None


def test_add_position_splits_shares_30_40_remainder(tracker):
    pos = tracker.add_position(_sample_plan(shares=10), "order-002")
    # 30% of 10 = 3, 40% of 10 = 4, remainder = 3
    assert pos.t1_shares == 3
    assert pos.t2_shares == 4
    assert pos.t3_shares == 3
    assert pos.t1_shares + pos.t2_shares + pos.t3_shares == 10


def test_add_position_remainder_goes_to_t3(tracker):
    pos = tracker.add_position(_sample_plan(shares=7), "order-003")
    # 30% of 7 = round(2.1) = 2, 40% of 7 = round(2.8) = 3, remainder = 2
    assert pos.t1_shares + pos.t2_shares + pos.t3_shares == 7


def test_add_position_minimum_one_share_per_tranche_for_small_position(tracker):
    pos = tracker.add_position(_sample_plan(shares=1), "order-004")
    # Even for 1 share, t1_shares clamped to min(1,...)
    assert pos.t1_shares >= 1
    assert pos.shares_initial == 1


def test_add_position_stores_stop_and_targets(tracker):
    pos = tracker.add_position(_sample_plan(), "order-005")
    assert pos.stop_price == 186.35
    assert pos.t1_price == 191.69
    assert pos.t2_price == 196.07
    assert pos.t3_price == 203.84


def test_add_position_raises_on_duplicate_ticker(tracker):
    tracker.add_position(_sample_plan(), "order-006")
    with pytest.raises(ValueError, match="already tracked"):
        tracker.add_position(_sample_plan(), "order-007")


def test_add_position_sets_trades_today_date(tracker):
    pos = tracker.add_position(_sample_plan(), "order-008")
    assert pos.trades_today_date == date.today().isoformat()


# ─── mark_entry_filled ───────────────────────────────────────────────────── #


def test_mark_entry_filled_transitions_to_active(tracker):
    tracker.add_position(_sample_plan(), "order-010")
    tracker.mark_entry_filled("AAPL", 189.50, 10)
    pos = tracker.get("AAPL")
    assert pos.status == "active"
    assert pos.fill_price == 189.50
    assert pos.shares_open == 10


def test_mark_entry_filled_recomputes_tranche_sizes(tracker):
    # Plan had 10 shares, but partial fill of 8
    tracker.add_position(_sample_plan(shares=10), "order-011")
    tracker.mark_entry_filled("AAPL", 189.50, 8)
    pos = tracker.get("AAPL")
    assert pos.shares_initial == 8
    assert pos.t1_shares + pos.t2_shares + pos.t3_shares == 8


# ─── mark_stop_placed / mark_target_placed ───────────────────────────────── #


def test_mark_stop_placed_saves_order_id(tracker):
    tracker.add_position(_sample_plan(), "order-020")
    tracker.mark_entry_filled("AAPL", 189.50, 10)
    tracker.mark_stop_placed("AAPL", "stop-order-id")
    assert tracker.get("AAPL").stop_order_id == "stop-order-id"


def test_mark_target_placed_saves_order_ids(tracker):
    tracker.add_position(_sample_plan(), "order-021")
    tracker.mark_entry_filled("AAPL", 189.50, 10)
    tracker.mark_target_placed("AAPL", "t1", "t1-order-id")
    tracker.mark_target_placed("AAPL", "t2", "t2-order-id")
    tracker.mark_target_placed("AAPL", "t3", "t3-order-id")
    pos = tracker.get("AAPL")
    assert pos.t1_order_id == "t1-order-id"
    assert pos.t2_order_id == "t2-order-id"
    assert pos.t3_order_id == "t3-order-id"


# ─── mark_target_hit ─────────────────────────────────────────────────────── #


def test_mark_t1_hit_reduces_shares_open_and_sets_flag(tracker):
    tracker.add_position(_sample_plan(shares=10), "order-030")
    tracker.mark_entry_filled("AAPL", 189.50, 10)
    pos_before = tracker.get("AAPL")
    t1_shares = pos_before.t1_shares
    tracker.mark_target_hit("AAPL", "t1")
    pos = tracker.get("AAPL")
    assert pos.t1_hit is True
    assert pos.shares_open == 10 - t1_shares


def test_mark_t2_hit_further_reduces_shares(tracker):
    tracker.add_position(_sample_plan(shares=10), "order-031")
    tracker.mark_entry_filled("AAPL", 189.50, 10)
    tracker.mark_target_hit("AAPL", "t1")
    t2_shares = tracker.get("AAPL").t2_shares
    shares_before = tracker.get("AAPL").shares_open
    tracker.mark_target_hit("AAPL", "t2")
    assert tracker.get("AAPL").t2_hit is True
    assert tracker.get("AAPL").shares_open == shares_before - t2_shares


def test_all_three_targets_hit_leaves_zero_shares(tracker):
    tracker.add_position(_sample_plan(shares=10), "order-032")
    tracker.mark_entry_filled("AAPL", 189.50, 10)
    tracker.mark_target_hit("AAPL", "t1")
    tracker.mark_target_hit("AAPL", "t2")
    tracker.mark_target_hit("AAPL", "t3")
    assert tracker.get("AAPL").shares_open == 0


# ─── mark_closed ─────────────────────────────────────────────────────────── #


def test_mark_closed_sets_status_and_removes_from_get_open(tracker):
    tracker.add_position(_sample_plan(), "order-040")
    tracker.mark_closed("AAPL", "STOP")
    pos = tracker.get("AAPL")
    assert pos.status == "closed"
    assert pos.shares_open == 0
    assert "AAPL" not in tracker.get_open()


# ─── get_open ────────────────────────────────────────────────────────────── #


def test_get_open_excludes_closed_positions(tracker):
    tracker.add_position(_sample_plan(), "order-050")
    tracker.add_position(_sample_plan("MSFT"), "order-051")
    tracker.mark_closed("AAPL", "TEST")
    open_pos = tracker.get_open()
    assert "AAPL" not in open_pos
    assert "MSFT" in open_pos


# ─── trades_today ────────────────────────────────────────────────────────── #


def test_trades_today_counts_only_todays_entries(tracker, monkeypatch):
    tracker.add_position(_sample_plan(), "order-060")
    # Backdate the second position to yesterday
    tracker.add_position(_sample_plan("MSFT"), "order-061")
    tracker._positions["MSFT"].trades_today_date = "2020-01-01"
    assert tracker.trades_today() == 1


# ─── Persistence ─────────────────────────────────────────────────────────── #


def test_save_and_load_round_trips_state(store_path):
    t1 = PositionTracker(store_path=store_path)
    t1.load()
    t1.add_position(_sample_plan(), "order-070")
    t1.mark_entry_filled("AAPL", 189.50, 10)

    # Create a fresh tracker and load from disk
    t2 = PositionTracker(store_path=store_path)
    t2.load()
    pos = t2.get("AAPL")
    assert pos is not None
    assert pos.fill_price == 189.50
    assert pos.status == "active"
    assert pos.entry_order_id == "order-070"


def test_atomic_save_leaves_no_tmp_file(store_path, tracker):
    tracker.add_position(_sample_plan(), "order-080")
    tmp = store_path + ".tmp"
    assert not os.path.exists(tmp), ".tmp file should be cleaned up after save"
    assert os.path.exists(store_path), "store file should exist after save"


def test_missing_store_file_does_not_raise(tmp_path):
    t = PositionTracker(store_path=str(tmp_path / "nonexistent.json"))
    t.load()  # Must not raise
    assert t.get_open() == {}


# ─── remove ──────────────────────────────────────────────────────────────── #


def test_remove_deletes_position(tracker):
    tracker.add_position(_sample_plan(), "order-090")
    tracker.remove("AAPL")
    assert tracker.get("AAPL") is None


def test_remove_unknown_ticker_is_noop(tracker):
    tracker.remove("ZZZZ")  # Must not raise


# ─── _require error path ─────────────────────────────────────────────────── #


def test_require_raises_key_error_for_unknown_ticker(tracker):
    with pytest.raises(KeyError, match="No tracked position"):
        tracker.mark_entry_filled("ZZZZ", 100.0, 5)
