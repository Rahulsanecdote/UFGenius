"""Unit tests for src/alpaca/executor.py — RiskGuard, execute_trade_plan, monitor."""

from unittest.mock import MagicMock, patch

import pytest

from src.alpaca.executor import (
    RiskGuard,
    _check_entry_fill,
    _check_exits,
    _is_market_hours,
    execute_trade_plan,
    monitor_positions,
)
from src.alpaca.position_tracker import PositionTracker


# ─── Fixtures ────────────────────────────────────────────────────────────── #


def _portfolio(
    equity=50_000.0,
    buying_power=40_000.0,
    position_count=0,
):
    return {
        "total_equity": equity,
        "buying_power": buying_power,
        "position_count": position_count,
    }


def _plan(
    ticker="AAPL",
    signal="STRONG_BUY",
    shares=10,
    entry=189.40,
    stop=186.35,
    position_value=1894.0,
    risk_dollars=30.5,
):
    return {
        "ticker": ticker,
        "signal": signal,
        "entry": {"type": "LIMIT", "price": entry},
        "stop_loss": {"price": stop},
        "targets": {
            "T1": {"price": 191.69, "exit_pct": 30},
            "T2": {"price": 196.07, "exit_pct": 40},
            "T3": {"price": 203.84, "exit_pct": 30},
        },
        "position": {
            "shares": shares,
            "position_value": position_value,
            "risk_dollars": risk_dollars,
        },
        "reasoning": ["Golden Cross: SMA50 > SMA200"],
    }


@pytest.fixture
def tracker(tmp_path):
    t = PositionTracker(store_path=str(tmp_path / "pos.json"))
    t.load()
    return t


# ─── RiskGuard ───────────────────────────────────────────────────────────── #


class TestRiskGuard:
    def _check(self, plan, portfolio, tracker):
        return RiskGuard().check(plan, portfolio, tracker)

    def test_approves_valid_plan(self, tracker):
        ok, reason = self._check(_plan(), _portfolio(), tracker)
        assert ok is True
        assert reason == ""

    def test_rejects_non_buy_signal(self, tracker):
        ok, reason = self._check(_plan(signal="HOLD"), _portfolio(), tracker)
        assert ok is False
        assert "not executable" in reason

    def test_rejects_sell_signal(self, tracker):
        ok, reason = self._check(_plan(signal="SELL"), _portfolio(), tracker)
        assert ok is False

    def test_rejects_duplicate_ticker(self, tracker):
        tracker.add_position(_plan(), "ord-001")
        ok, reason = self._check(_plan(), _portfolio(), tracker)
        assert ok is False
        assert "already tracked" in reason

    def test_rejects_when_portfolio_has_error(self, tracker):
        ok, reason = self._check(_plan(), {"error": "API down"}, tracker)
        assert ok is False
        assert "Portfolio unavailable" in reason

    def test_rejects_zero_equity(self, tracker):
        ok, reason = self._check(_plan(), _portfolio(equity=0), tracker)
        assert ok is False
        assert "equity is zero" in reason

    def test_rejects_when_max_positions_reached(self, tracker):
        portfolio = _portfolio(position_count=5)  # max_positions default = 5
        ok, reason = self._check(_plan(), portfolio, tracker)
        assert ok is False
        assert "Max open positions" in reason

    def test_rejects_when_position_too_large(self, tracker):
        # Position value = $6,000 > 10% of $10,000 equity
        ok, reason = self._check(
            _plan(position_value=6_000),
            _portfolio(equity=10_000, buying_power=9_000),
            tracker,
        )
        assert ok is False
        assert "exceeds" in reason

    def test_rejects_when_insufficient_cash(self, tracker):
        # equity=100_000 → max_single=$10,000; position_value=$4,000 passes check 5
        # cash_reserve=20% of 100_000=20_000; required=4_000+20_000=24_000
        # buying_power=15_000 < 24_000 → rejected at cash reserve check
        ok, reason = self._check(
            _plan(position_value=4_000),
            _portfolio(equity=100_000, buying_power=15_000),
            tracker,
        )
        assert ok is False
        assert "buying power" in reason.lower()

    def test_rejects_when_trade_risk_too_high(self, tracker):
        # equity=100_000 → position_value=$1,894 ≤ $10,000 → check 5 passes
        # buying_power=$90,000 → cash reserve check passes
        # max_portfolio_risk=5% of 100,000=5,000; per_trade_limit=5000/5=1000
        # risk_dollars=$3,000 > $1,000 → rejected
        ok, reason = self._check(
            _plan(risk_dollars=3_000),
            _portfolio(equity=100_000, buying_power=90_000),
            tracker,
        )
        assert ok is False
        assert "risk" in reason.lower()

    def test_rejects_when_daily_trade_limit_reached(self, tracker):
        # Add 3 positions opened today (max_trades_per_day = 3)
        for i, ticker in enumerate(["AAPL", "MSFT", "GOOG"]):
            tracker.add_position(_plan(ticker=ticker), f"ord-{i}")
        ok, reason = self._check(_plan(ticker="TSLA"), _portfolio(), tracker)
        assert ok is False
        assert "Daily trade limit" in reason

    def test_rejects_bear_market_signal(self, tracker):
        bear_plan = _plan()
        bear_plan["reasoning"] = ["Macro: BEAR_RISK_OFF regime"]
        with patch.dict(
            "src.alpaca.executor.config.SAFETY",
            {"trade_in_bear_market": False, **{k: v for k, v in {
                "max_positions": 5, "max_portfolio_risk_pct": 5.0,
                "max_daily_loss_pct": 2.0, "cash_reserve_pct": 20.0,
                "max_single_position_pct": 10.0, "max_trades_per_day": 3,
            }.items()}},
        ):
            ok, reason = self._check(bear_plan, _portfolio(), tracker)
        assert ok is False
        assert "Bear market" in reason


# ─── execute_trade_plan ──────────────────────────────────────────────────── #


def _mock_order(order_id="test-order-id"):
    o = MagicMock()
    o.id = order_id
    return o


class TestExecuteTradePlan:
    def test_places_order_on_approval(self, tracker):
        with patch("src.alpaca.executor.get_portfolio_data", return_value=_portfolio()):
            with patch(
                "src.alpaca.executor.place_entry_order",
                return_value=_mock_order(),
            ) as mock_place:
                result = execute_trade_plan(_plan(), tracker)

        assert result["ok"] is True
        assert result["order_id"] == "test-order-id"
        assert result["ticker"] == "AAPL"
        mock_place.assert_called_once()

    def test_saves_to_tracker_on_success(self, tracker):
        with patch("src.alpaca.executor.get_portfolio_data", return_value=_portfolio()):
            with patch(
                "src.alpaca.executor.place_entry_order",
                return_value=_mock_order("saved-order-id"),
            ):
                execute_trade_plan(_plan(), tracker)

        pos = tracker.get("AAPL")
        assert pos is not None
        assert pos.entry_order_id == "saved-order-id"
        assert pos.status == "pending_fill"

    def test_dry_run_does_not_place_order(self, tracker):
        with patch("src.alpaca.executor.get_portfolio_data", return_value=_portfolio()):
            with patch(
                "src.alpaca.executor.place_entry_order"
            ) as mock_place:
                result = execute_trade_plan(_plan(), tracker, dry_run=True)

        mock_place.assert_not_called()
        assert result["ok"] is True
        assert result["dry_run"] is True
        assert result["order_id"] is None
        # Tracker should NOT have a position after dry run
        assert tracker.get("AAPL") is None

    def test_returns_error_on_risk_rejection(self, tracker):
        with patch(
            "src.alpaca.executor.get_portfolio_data",
            return_value=_portfolio(position_count=5),
        ):
            result = execute_trade_plan(_plan(), tracker)

        assert result["ok"] is False
        assert "Max open positions" in result["reason"]

    def test_returns_error_on_order_api_failure(self, tracker):
        from src.alpaca.orders import OrderError

        with patch("src.alpaca.executor.get_portfolio_data", return_value=_portfolio()):
            with patch(
                "src.alpaca.executor.place_entry_order",
                side_effect=OrderError("timeout"),
            ):
                result = execute_trade_plan(_plan(), tracker)

        assert result["ok"] is False
        assert "timeout" in result["reason"]

    def test_returns_error_for_incomplete_plan(self, tracker):
        bad_plan = {"ticker": "", "entry": {}, "position": {}}
        with patch("src.alpaca.executor.get_portfolio_data", return_value=_portfolio()):
            result = execute_trade_plan(bad_plan, tracker)
        assert result["ok"] is False
        assert "Incomplete" in result["reason"]


# ─── Monitor: _check_entry_fill ──────────────────────────────────────────── #


class TestCheckEntryFill:
    def _add_pending(self, tracker):
        plan = _plan()
        tracker.add_position(plan, "entry-order-id")
        return tracker.get("AAPL")

    def test_filled_entry_transitions_to_active(self, tracker):
        self._add_pending(tracker)
        mock_order = MagicMock()
        mock_order.status = "filled"
        mock_order.filled_avg_price = "189.50"
        mock_order.filled_qty = "10"

        with patch("src.alpaca.executor.get_order", return_value=mock_order):
            with patch("src.alpaca.executor.place_stop_order", return_value=MagicMock(id="stop-id")):
                with patch("src.alpaca.executor.place_limit_sell", return_value=MagicMock(id="tgt-id")):
                    _check_entry_fill("AAPL", tracker.get("AAPL"), tracker)

        pos = tracker.get("AAPL")
        assert pos.status == "active"
        assert pos.fill_price == 189.50

    def test_filled_entry_places_stop_order(self, tracker):
        self._add_pending(tracker)
        mock_order = MagicMock()
        mock_order.status = "filled"
        mock_order.filled_avg_price = "189.50"
        mock_order.filled_qty = "10"

        with patch("src.alpaca.executor.get_order", return_value=mock_order):
            with patch(
                "src.alpaca.executor.place_stop_order",
                return_value=MagicMock(id="stop-id"),
            ) as mock_stop:
                with patch(
                    "src.alpaca.executor.place_limit_sell",
                    return_value=MagicMock(id="tgt"),
                ):
                    _check_entry_fill("AAPL", tracker.get("AAPL"), tracker)

        mock_stop.assert_called_once()

    def test_filled_entry_places_three_target_orders(self, tracker):
        self._add_pending(tracker)
        mock_order = MagicMock()
        mock_order.status = "filled"
        mock_order.filled_avg_price = "189.50"
        mock_order.filled_qty = "10"

        placed_sells = []

        def capture_sell(symbol, shares, price):
            placed_sells.append((symbol, shares, price))
            return MagicMock(id=f"t-{len(placed_sells)}")

        with patch("src.alpaca.executor.get_order", return_value=mock_order):
            with patch("src.alpaca.executor.place_stop_order", return_value=MagicMock(id="s")):
                with patch("src.alpaca.executor.place_limit_sell", side_effect=capture_sell):
                    _check_entry_fill("AAPL", tracker.get("AAPL"), tracker)

        assert len(placed_sells) == 3

    def test_expired_entry_marks_closed(self, tracker):
        self._add_pending(tracker)
        mock_order = MagicMock()
        mock_order.status = "expired"

        with patch("src.alpaca.executor.get_order", return_value=mock_order):
            _check_entry_fill("AAPL", tracker.get("AAPL"), tracker)

        assert tracker.get("AAPL").status == "closed"


# ─── Monitor: _check_exits ───────────────────────────────────────────────── #


class TestCheckExits:
    def _add_active(self, tracker):
        plan = _plan()
        tracker.add_position(plan, "entry-order-id")
        tracker.mark_entry_filled("AAPL", 189.50, 10)
        tracker.mark_stop_placed("AAPL", "stop-order-id")
        tracker.mark_target_placed("AAPL", "t1", "t1-order-id")
        tracker.mark_target_placed("AAPL", "t2", "t2-order-id")
        tracker.mark_target_placed("AAPL", "t3", "t3-order-id")

    def _order_with_status(self, status):
        o = MagicMock()
        o.status = status
        return o

    def test_stop_filled_closes_position_and_cancels_targets(self, tracker):
        self._add_active(tracker)

        def _get_order_side_effect(oid):
            if oid == "stop-order-id":
                return self._order_with_status("filled")
            return self._order_with_status("open")

        cancelled = []

        with patch("src.alpaca.executor.get_order", side_effect=_get_order_side_effect):
            with patch("src.alpaca.executor.cancel_order", side_effect=lambda oid: cancelled.append(oid) or True):
                _check_exits("AAPL", tracker.get("AAPL"), tracker)

        assert tracker.get("AAPL").status == "closed"
        # Should attempt to cancel t1, t2, t3
        assert len(cancelled) == 3

    def test_t1_filled_marks_target_hit(self, tracker):
        self._add_active(tracker)

        def _get_order_side_effect(oid):
            if oid == "t1-order-id":
                return self._order_with_status("filled")
            return self._order_with_status("open")

        with patch("src.alpaca.executor.get_order", side_effect=_get_order_side_effect):
            _check_exits("AAPL", tracker.get("AAPL"), tracker)

        pos = tracker.get("AAPL")
        assert pos.t1_hit is True
        assert pos.status != "closed"

    def test_all_targets_filled_closes_position(self, tracker):
        self._add_active(tracker)

        def _get_order_side_effect(oid):
            if oid in ("t1-order-id", "t2-order-id", "t3-order-id"):
                return self._order_with_status("filled")
            return self._order_with_status("open")

        with patch("src.alpaca.executor.get_order", side_effect=_get_order_side_effect):
            _check_exits("AAPL", tracker.get("AAPL"), tracker)

        assert tracker.get("AAPL").status == "closed"

    def test_monitor_handles_api_error_gracefully(self, tracker):
        """monitor_positions must not raise even if get_order fails."""
        from src.alpaca.orders import OrderError

        plan = _plan()
        tracker.add_position(plan, "entry-id")

        with patch("src.alpaca.executor.get_order", side_effect=OrderError("timeout")):
            # Must not propagate exception
            monitor_positions(tracker)


# ─── _is_market_hours ────────────────────────────────────────────────────── #


def test_is_market_hours_returns_bool():
    """Simply verify the function returns a bool without raising."""
    result = _is_market_hours()
    assert isinstance(result, bool)
