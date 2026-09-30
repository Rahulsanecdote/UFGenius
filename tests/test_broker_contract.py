"""The exit path against a broker that behaves like Alpaca on the two points
the mocks never modelled — and that both broke it.

1. **Statuses are enums.** alpaca-py returns ``OrderStatus.FILLED``, and
   ``str()`` of that is ``"OrderStatus.FILLED"``. The monitor compared
   ``str(order.status).lower() == "filled"``, so on a real account a filled
   entry was never recognised: it stayed ``pending_fill`` and its stop was
   never placed. Every mock in the suite used the plain string ``"filled"``,
   which is why nothing caught it.
2. **Open sell orders reserve shares.** Alpaca's own error guide: "When you
   submit a sell order (e.g., a Limit Sell or Stop Loss), the shares tied to
   that order are reserved until the order is filled or canceled ... The second
   request is rejected." The executor placed a full-size stop and then three
   target limit sells for the same shares, so every target was refused and a
   trade could only ever leave at its stop.

`FakeAlpaca` returns real ``alpaca.trading.models.Order`` objects and enforces
the reservation rule, with OCO semantics as documented (take-profit is the
parent, the stop its child leg; one filling cancels the other; a partial
take-profit shrinks the stop). The paper account had never placed an order,
so neither bug had been seen against a real broker. These tests are the
nearest thing to that which a unit test can be — a paper run is still the
real check.
"""

from __future__ import annotations

import itertools
import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from alpaca.trading.enums import OrderClass, OrderSide
from alpaca.trading.models import Order

import src.alpaca.executor as ex
from src.alpaca import orders
from src.alpaca.position_tracker import PositionTracker

_OPEN = {"new", "accepted", "held", "partially_filled", "pending_new"}
_ASSET = "904837e3-3b76-47ec-b432-046db621571b"


class FakeAlpaca:
    """Just enough of TradingClient to run the executor's order lifecycle."""

    def __init__(self, legs_on_submit: bool = True):
        self.orders: dict[str, dict] = {}
        self.position: dict[str, int] = {}
        self.legs_on_submit = legs_on_submit
        self.rejections: list[str] = []

    # ── broker state ────────────────────────────────────────────────────
    def _reserved(self, symbol: str) -> int:
        """Shares held by open sell orders. An OCO reserves ONCE: its stop leg
        guards the same shares as its take-profit parent."""
        return sum(
            o["qty"] - o["filled_qty"]
            for o in self.orders.values()
            if o["symbol"] == symbol and o["side"] == "sell"
            and o["status"] in _OPEN and o["parent"] is None
        )

    def _new(self, order_class: str = "simple", **kw) -> dict:
        o = dict(id=str(uuid.uuid4()), filled_qty=0, filled_avg=None, parent=None,
                 legs=[], limit=None, stop=None, order_class=order_class, **kw)
        self.orders[o["id"]] = o
        return o

    def _model(self, o: dict, nested: bool) -> Order:
        now = datetime(2026, 9, 30, 14, 0, tzinfo=timezone.utc).isoformat()
        legs = None
        if nested and o["legs"]:
            legs = [self._model(self.orders[i], False) for i in o["legs"]]
        return Order(
            id=o["id"], client_order_id=o["id"][:8], created_at=now, updated_at=now,
            submitted_at=now, asset_id=_ASSET, symbol=o["symbol"], asset_class="us_equity",
            qty=str(o["qty"]), filled_qty=str(o["filled_qty"]),
            filled_avg_price=None if o["filled_avg"] is None else str(o["filled_avg"]),
            order_class=o["order_class"], order_type=o["type"], type=o["type"],
            side=o["side"], time_in_force="gtc", limit_price=o["limit"],
            stop_price=o["stop"], status=o["status"], extended_hours=False, legs=legs,
        )

    # ── TradingClient surface ───────────────────────────────────────────
    def submit_order(self, req) -> Order:
        side = req.side.value if hasattr(req.side, "value") else str(req.side)
        qty = int(req.qty)
        if side == "sell":
            available = self.position.get(req.symbol, 0) - self._reserved(req.symbol)
            if qty > available:
                msg = (f'{{"code":40310000,"message":"insufficient qty available for '
                       f'order (requested: {qty}, available: {max(0, available)})"}}')
                self.rejections.append(msg)
                raise Exception(msg)
        if getattr(req, "order_class", None) == OrderClass.OCO:
            parent = self._new(symbol=req.symbol, side=side, type="limit", qty=qty,
                               status="new", order_class="oco")
            parent["limit"] = req.take_profit.limit_price
            leg = self._new(symbol=req.symbol, side=side, type="stop", qty=qty,
                            status="held", order_class="oco")
            leg["stop"], leg["parent"] = req.stop_loss.stop_price, parent["id"]
            parent["legs"] = [leg["id"]]
            return self._model(parent, nested=self.legs_on_submit)
        typ = req.type.value if hasattr(req.type, "value") else str(req.type)
        o = self._new(symbol=req.symbol, side=side, type=typ, qty=qty, status="new")
        o["limit"] = getattr(req, "limit_price", None)
        o["stop"] = getattr(req, "stop_price", None)
        return self._model(o, nested=False)

    def get_order_by_id(self, order_id, filter=None) -> Order:
        return self._model(self.orders[str(order_id)], nested=bool(filter and filter.nested))

    def cancel_order_by_id(self, order_id) -> None:
        o = self.orders[str(order_id)]
        if o["status"] not in _OPEN:
            raise Exception("422 Unprocessable Entity: order is not cancelable")
        self._cancel_group(o)

    # ── the market ──────────────────────────────────────────────────────
    def _cancel_group(self, o: dict) -> None:
        """Alpaca: if any one order of the group is cancelled, the rest are."""
        root = self.orders[o["parent"]] if o["parent"] else o
        for x in [root] + [self.orders[i] for i in root["legs"]]:
            if x["status"] in _OPEN:
                x["status"] = "canceled"

    def _fill(self, o: dict, qty: int, price: float) -> None:
        o["filled_qty"] += qty
        o["filled_avg"] = price
        o["status"] = "filled" if o["filled_qty"] >= o["qty"] else "partially_filled"
        sign = 1 if o["side"] == "buy" else -1
        self.position[o["symbol"]] = self.position.get(o["symbol"], 0) + sign * qty

    def fill_entry(self, order_id: str, price: float, qty: int | None = None) -> None:
        o = self.orders[order_id]
        self._fill(o, qty or o["qty"], price)

    def trade_at(self, symbol: str, price: float) -> None:
        """Price prints at `price`: trigger every sell order it crosses."""
        for o in list(self.orders.values()):
            if o["symbol"] != symbol or o["side"] != "sell" or o["status"] not in _OPEN:
                continue
            if o["type"] == "limit" and price >= o["limit"]:
                self._fill(o, o["qty"] - o["filled_qty"], o["limit"])
                for i in o["legs"]:               # take-profit filled: cancel its stop
                    if self.orders[i]["status"] in _OPEN:
                        self.orders[i]["status"] = "canceled"
            elif o["type"] == "stop" and price <= o["stop"]:
                self._fill(o, o["qty"] - o["filled_qty"], price)
                if o["parent"]:                   # stop filled: cancel its take-profit
                    p = self.orders[o["parent"]]
                    if p["status"] in _OPEN:
                        p["status"] = "canceled"

    def part_fill_take_profit(self, parent_id: str, qty: int) -> None:
        """Documented: a partial take-profit shrinks the stop to the remainder."""
        p = self.orders[parent_id]
        self._fill(p, qty, p["limit"])
        for i in p["legs"]:
            self.orders[i]["qty"] = p["qty"] - p["filled_qty"]


def _plan(shares=10, entry=189.40, stop=186.35):
    return {
        "ticker": "AAPL", "signal": "STRONG_BUY",
        "entry": {"type": "LIMIT", "price": entry},
        "stop_loss": {"price": stop},
        "targets": {"T1": {"price": 191.69}, "T2": {"price": 196.07}, "T3": {"price": 203.84}},
        "position": {"shares": shares, "position_value": shares * entry,
                     "risk_dollars": shares * (entry - stop)},
    }


@pytest.fixture
def broker(monkeypatch):
    fake = FakeAlpaca()
    monkeypatch.setattr(orders, "_client", fake)
    monkeypatch.setattr(ex, "_record_execution_quality", lambda *a, **k: None)
    return fake


@pytest.fixture
def tracker(tmp_path):
    t = PositionTracker(store_path=str(tmp_path / "pos.json"))
    t.load()
    return t


def _open_and_fill(broker, tracker, shares=10, fill=189.50):
    entry = orders.place_entry_order("AAPL", shares, 189.40)
    tracker.add_position(_plan(shares=shares), str(entry.id))
    broker.fill_entry(str(entry.id), fill)
    ex.monitor_positions(tracker)
    return tracker.get("AAPL")


# ── 1. statuses are enums ────────────────────────────────────────────────────

class TestEnumStatuses:
    def test_the_broker_really_returns_an_enum_that_stringifies_wrongly(self, broker):
        o = orders.place_entry_order("AAPL", 1, 189.40)
        broker.fill_entry(str(o.id), 189.4)
        got = orders.get_order(str(o.id))
        assert str(got.status) != "filled"          # the trap
        assert orders.order_status(got) == "filled"  # the fix

    def test_a_filled_entry_is_recognised_and_protected(self, broker, tracker):
        pos = _open_and_fill(broker, tracker)
        assert pos.status == "active"
        assert pos.fill_price == 189.50
        assert pos.t1_stop_id and pos.t2_stop_id and pos.t3_stop_id

    def test_an_expired_entry_is_cleaned_up(self, broker, tracker):
        """Before the fix this never matched either: an expired DAY entry sat in
        the tracker as pending forever and counted against max_positions."""
        entry = orders.place_entry_order("AAPL", 10, 189.40)
        tracker.add_position(_plan(), str(entry.id))
        broker.orders[str(entry.id)]["status"] = "expired"
        ex.monitor_positions(tracker)
        assert tracker.get("AAPL").status == "closed"


# ── 2. open sell orders reserve shares ───────────────────────────────────────

class TestReservation:
    def test_the_old_sequence_is_refused_by_the_broker(self, broker):
        """Validates the fake as much as it records the bug: a full-size stop,
        then a target sell for the same shares, is a 403."""
        o = orders.place_entry_order("AAPL", 10, 189.40)
        broker.fill_entry(str(o.id), 189.4)
        orders.place_stop_order("AAPL", 10, 186.35)
        with pytest.raises(orders.OrderError, match="insufficient qty"):
            orders.place_limit_sell("AAPL", 3, 191.69)

    def test_one_oco_per_tranche_fits_inside_the_position(self, broker, tracker):
        _open_and_fill(broker, tracker)
        assert broker.rejections == []
        assert broker._reserved("AAPL") == 10 == broker.position["AAPL"]


# ── the lifecycle ────────────────────────────────────────────────────────────

class TestLifecycle:
    def test_target_then_stop(self, broker, tracker):
        _open_and_fill(broker, tracker)
        broker.trade_at("AAPL", 192.00)                  # T1 (3 sh @ 191.69)
        ex.monitor_positions(tracker)
        pos = tracker.get("AAPL")
        assert pos.t1_hit and pos.shares_open == 7 and pos.status == "active"
        # The other 7 shares are still guarded by their own stop legs — no
        # resize, no unprotected window.
        assert broker._reserved("AAPL") == 7 == broker.position["AAPL"]

        broker.trade_at("AAPL", 186.00)                  # stop: T2 + T3 legs fire
        ex.monitor_positions(tracker)
        pos = tracker.get("AAPL")
        assert pos.t2_stopped and pos.t3_stopped
        assert pos.status == "closed"
        assert broker.position["AAPL"] == 0              # flat, not short
        expected = 3 * (191.69 - 189.50) + 7 * (186.00 - 189.50)
        assert pos.realized_pnl == pytest.approx(expected)
        assert tracker.get_trades()[-1]["reason"] == "STOP"

    def test_all_three_targets(self, broker, tracker):
        _open_and_fill(broker, tracker)
        for px in (192.0, 197.0, 204.0):
            broker.trade_at("AAPL", px)
            ex.monitor_positions(tracker)
        pos = tracker.get("AAPL")
        assert pos.status == "closed" and broker.position["AAPL"] == 0
        assert (pos.t1_hit, pos.t2_hit, pos.t3_hit) == (True, True, True)
        expected = 3 * (191.69 - 189.5) + 4 * (196.07 - 189.5) + 3 * (203.84 - 189.5)
        assert pos.realized_pnl == pytest.approx(expected)
        assert tracker.get_trades()[-1]["reason"] == "ALL_TARGETS"

    def test_straight_to_the_stop(self, broker, tracker):
        _open_and_fill(broker, tracker)
        broker.trade_at("AAPL", 186.20)
        ex.monitor_positions(tracker)
        pos = tracker.get("AAPL")
        assert pos.status == "closed" and broker.position["AAPL"] == 0
        assert pos.realized_pnl == pytest.approx(10 * (186.20 - 189.50))

    def test_a_one_share_position_uses_one_tranche(self, broker, tracker):
        pos = _open_and_fill(broker, tracker, shares=1)
        assert (pos.t1_shares, pos.t2_shares, pos.t3_shares) == (1, 0, 0)
        assert broker._reserved("AAPL") == 1
        broker.trade_at("AAPL", 192.0)
        ex.monitor_positions(tracker)
        assert tracker.get("AAPL").status == "closed"

    def test_legs_missing_from_the_submit_response_are_found_by_nested_fetch(
            self, broker, tracker):
        broker.legs_on_submit = False
        pos = _open_and_fill(broker, tracker)
        assert pos.t1_stop_id is None                    # not in the response
        broker.trade_at("AAPL", 186.0)
        ex.monitor_positions(tracker)
        assert tracker.get("AAPL").status == "closed"


# ── the edges ────────────────────────────────────────────────────────────────

class TestEdges:
    def test_a_failed_placement_is_retried_next_cycle(self, broker, tracker, monkeypatch):
        real = orders.place_oco_exit
        calls = itertools.count()

        def flaky(*a, **k):
            if next(calls) == 1:                         # T2 fails the first time
                raise orders.OrderError("HTTP 500")
            return real(*a, **k)

        monkeypatch.setattr(ex, "place_oco_exit", flaky)
        pos = _open_and_fill(broker, tracker)
        assert pos.t2_order_id is None and broker._reserved("AAPL") == 6
        ex.monitor_positions(tracker)
        assert tracker.get("AAPL").t2_order_id is not None
        assert broker._reserved("AAPL") == 10

    def test_a_cancelled_exit_is_re_placed(self, broker, tracker):
        pos = _open_and_fill(broker, tracker)
        broker.cancel_order_by_id(pos.t3_order_id)      # e.g. cancelled by hand
        assert broker._reserved("AAPL") == 7
        ex.monitor_positions(tracker)
        assert broker._reserved("AAPL") == 10
        assert tracker.get("AAPL").t3_order_id != pos.t3_order_id

    def test_a_part_filled_then_cancelled_exit_re_places_only_the_remainder(
            self, broker, tracker):
        pos = _open_and_fill(broker, tracker)            # T2 = 4 shares
        broker.part_fill_take_profit(pos.t2_order_id, 1)
        broker.cancel_order_by_id(pos.t2_order_id)
        ex.monitor_positions(tracker)
        pos = tracker.get("AAPL")
        assert pos.t2_shares == 3 and pos.shares_open == 9
        assert broker.rejections == []                   # never tried to sell 4
        assert broker._reserved("AAPL") == 9 == broker.position["AAPL"]
        assert pos.realized_pnl == pytest.approx(1 * (196.07 - 189.50))

    def test_a_partial_take_profit_then_stop_books_both_legs(self, broker, tracker):
        pos = _open_and_fill(broker, tracker)
        broker.part_fill_take_profit(pos.t2_order_id, 1)
        broker.trade_at("AAPL", 186.0)
        ex.monitor_positions(tracker)
        pos = tracker.get("AAPL")
        assert pos.status == "closed" and broker.position["AAPL"] == 0
        expected = 1 * (196.07 - 189.5) + 9 * (186.0 - 189.5)
        assert pos.realized_pnl == pytest.approx(expected)

    def test_both_legs_filling_is_reported_loudly(self, broker, tracker, caplog):
        pos = _open_and_fill(broker, tracker)
        leg = broker.orders[pos.t1_stop_id]
        broker.trade_at("AAPL", 192.0)                  # T1 take-profit fills...
        leg["status"] = "held"                           # ...and the race: its stop too
        broker._fill(leg, leg["qty"], 186.0)
        ex.monitor_positions(tracker)
        assert tracker.get("AAPL").t1_hit
        assert "SHORT 3 share(s)" in caplog.text


# ── the request itself ───────────────────────────────────────────────────────

class TestOcoRequest:
    def test_matches_the_documented_body(self, monkeypatch):
        sent = []
        client = MagicMock()
        client.submit_order.side_effect = lambda req: sent.append(req) or MagicMock(id="x")
        monkeypatch.setattr(orders, "_client", client)
        orders.place_oco_exit("SPY", 100, 301.0, 299.0)
        body = sent[0].to_request_fields()
        assert body["order_class"] == OrderClass.OCO and body["type"].value == "limit"
        assert body["side"] == OrderSide.SELL and body["time_in_force"].value == "gtc"
        assert body["take_profit"] == {"limit_price": 301.0}
        assert body["stop_loss"] == {"stop_price": 299.0}
        assert "limit_price" not in body

    @pytest.mark.parametrize("shares,tp,stop", [(0, 2, 1), (1, 1, 1), (1, 0.5, 1), (1, 2, 0)])
    def test_rejects_nonsense_before_sending(self, shares, tp, stop, monkeypatch):
        monkeypatch.setattr(orders, "_client", MagicMock())
        with pytest.raises(orders.OrderError):
            orders.place_oco_exit("SPY", shares, tp, stop)


@pytest.mark.parametrize("status,want", [
    ("filled", "filled"), ("Filled", "filled"), (None, ""),
])
def test_order_status_normalises_strings_too(status, want):
    assert orders.order_status(MagicMock(status=status)) == want
