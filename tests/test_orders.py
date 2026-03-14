"""Unit tests for src/alpaca/orders.py — mocks out alpaca-py SDK entirely."""

import sys
from unittest.mock import MagicMock, patch

import pytest

import src.alpaca.orders as orders_module
from src.alpaca.orders import (
    OrderError,
    _reset_client,
)


# ─── Fake alpaca module hierarchy ────────────────────────────────────────── #
# alpaca-py may not be installed in CI.  We inject a complete fake into
# sys.modules so the lazy imports inside orders.py resolve correctly.


class _OrderSide:
    BUY = "buy"
    SELL = "sell"


class _TimeInForce:
    DAY = "day"
    GTC = "gtc"


class _QueryOrderStatus:
    OPEN = "open"


class _LimitOrderRequest:
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


class _StopOrderRequest:
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


class _GetOrdersRequest:
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


def _alpaca_sys_modules():
    requests_mod = MagicMock()
    requests_mod.LimitOrderRequest = _LimitOrderRequest
    requests_mod.StopOrderRequest = _StopOrderRequest
    requests_mod.GetOrdersRequest = _GetOrdersRequest

    enums_mod = MagicMock()
    enums_mod.OrderSide = _OrderSide
    enums_mod.TimeInForce = _TimeInForce
    enums_mod.QueryOrderStatus = _QueryOrderStatus

    client_mod = MagicMock()

    return {
        "alpaca": MagicMock(),
        "alpaca.trading": MagicMock(),
        "alpaca.trading.client": client_mod,
        "alpaca.trading.requests": requests_mod,
        "alpaca.trading.enums": enums_mod,
    }


# ─── Helpers ─────────────────────────────────────────────────────────────── #


def _mock_order(order_id: str = "abc-123", status: str = "accepted"):
    order = MagicMock()
    order.id = order_id
    order.status = status
    return order


def _alpaca_ctx(mock_client=None):
    """Context manager that patches sys.modules with fake alpaca + overrides _get_client."""
    _reset_client()
    if mock_client is None:
        mock_client = MagicMock()
    modules = _alpaca_sys_modules()
    return (
        patch.dict(sys.modules, modules),
        patch.object(orders_module, "_get_client", return_value=mock_client),
    )


# ─── place_entry_order ───────────────────────────────────────────────────── #


def test_place_entry_order_submits_limit_buy():
    mock_order = _mock_order()
    mock_client = MagicMock()
    mock_client.submit_order.return_value = mock_order

    sys_ctx, client_ctx = _alpaca_ctx(mock_client)
    with sys_ctx, client_ctx:
        from src.alpaca.orders import place_entry_order
        result = place_entry_order("AAPL", 10, 189.40)

    mock_client.submit_order.assert_called_once()
    req = mock_client.submit_order.call_args[0][0]
    assert isinstance(req, _LimitOrderRequest)
    assert req.symbol == "AAPL"
    assert req.qty == 10
    assert req.side is _OrderSide.BUY
    assert req.time_in_force is _TimeInForce.DAY
    assert req.limit_price == 189.40
    assert result is mock_order


def test_place_entry_order_rejects_zero_shares():
    with pytest.raises(OrderError, match="shares must be positive"):
        from src.alpaca.orders import place_entry_order
        place_entry_order("AAPL", 0, 189.40)


def test_place_entry_order_rejects_negative_price():
    with pytest.raises(OrderError, match="limit_price must be positive"):
        from src.alpaca.orders import place_entry_order
        place_entry_order("AAPL", 10, -1.0)


def test_place_entry_order_raises_on_api_exception():
    mock_client = MagicMock()
    mock_client.submit_order.side_effect = Exception("API timeout")

    sys_ctx, client_ctx = _alpaca_ctx(mock_client)
    with sys_ctx, client_ctx:
        from src.alpaca.orders import place_entry_order
        with pytest.raises(OrderError, match="Failed to submit entry order"):
            place_entry_order("AAPL", 10, 189.40)


# ─── place_stop_order ────────────────────────────────────────────────────── #


def test_place_stop_order_submits_gtc_stop_sell():
    mock_order = _mock_order()
    mock_client = MagicMock()
    mock_client.submit_order.return_value = mock_order

    sys_ctx, client_ctx = _alpaca_ctx(mock_client)
    with sys_ctx, client_ctx:
        from src.alpaca.orders import place_stop_order
        result = place_stop_order("AAPL", 10, 186.35)

    req = mock_client.submit_order.call_args[0][0]
    assert isinstance(req, _StopOrderRequest)
    assert req.side is _OrderSide.SELL
    assert req.time_in_force is _TimeInForce.GTC
    assert req.stop_price == 186.35
    assert result is mock_order


def test_place_stop_order_rejects_zero_shares():
    with pytest.raises(OrderError, match="shares must be positive"):
        from src.alpaca.orders import place_stop_order
        place_stop_order("AAPL", 0, 186.35)


def test_place_stop_order_raises_on_api_exception():
    mock_client = MagicMock()
    mock_client.submit_order.side_effect = RuntimeError("network error")

    sys_ctx, client_ctx = _alpaca_ctx(mock_client)
    with sys_ctx, client_ctx:
        from src.alpaca.orders import place_stop_order
        with pytest.raises(OrderError, match="Failed to submit stop order"):
            place_stop_order("AAPL", 10, 186.35)


# ─── place_limit_sell ────────────────────────────────────────────────────── #


def test_place_limit_sell_submits_gtc_limit_sell():
    mock_order = _mock_order()
    mock_client = MagicMock()
    mock_client.submit_order.return_value = mock_order

    sys_ctx, client_ctx = _alpaca_ctx(mock_client)
    with sys_ctx, client_ctx:
        from src.alpaca.orders import place_limit_sell
        result = place_limit_sell("AAPL", 5, 191.69)

    req = mock_client.submit_order.call_args[0][0]
    assert isinstance(req, _LimitOrderRequest)
    assert req.side is _OrderSide.SELL
    assert req.time_in_force is _TimeInForce.GTC
    assert req.limit_price == 191.69
    assert req.qty == 5
    assert result is mock_order


def test_place_limit_sell_rejects_zero_shares():
    with pytest.raises(OrderError, match="shares must be positive"):
        from src.alpaca.orders import place_limit_sell
        place_limit_sell("AAPL", 0, 191.69)


# ─── cancel_order ────────────────────────────────────────────────────────── #


def test_cancel_order_returns_true_on_success():
    mock_client = MagicMock()
    mock_client.cancel_order_by_id.return_value = None

    sys_ctx, client_ctx = _alpaca_ctx(mock_client)
    with sys_ctx, client_ctx:
        from src.alpaca.orders import cancel_order
        result = cancel_order("order-111")

    mock_client.cancel_order_by_id.assert_called_once_with("order-111")
    assert result is True


def test_cancel_order_returns_false_on_422():
    """Already filled/cancelled order returns False, not an error."""
    mock_client = MagicMock()
    mock_client.cancel_order_by_id.side_effect = Exception("422 Unprocessable Entity")

    sys_ctx, client_ctx = _alpaca_ctx(mock_client)
    with sys_ctx, client_ctx:
        from src.alpaca.orders import cancel_order
        result = cancel_order("order-222")

    assert result is False


def test_cancel_order_raises_on_unexpected_error():
    mock_client = MagicMock()
    mock_client.cancel_order_by_id.side_effect = Exception("500 Internal Server Error")

    sys_ctx, client_ctx = _alpaca_ctx(mock_client)
    with sys_ctx, client_ctx:
        from src.alpaca.orders import cancel_order
        with pytest.raises(OrderError, match="Failed to cancel order"):
            cancel_order("order-333")


# ─── get_order ───────────────────────────────────────────────────────────── #


def test_get_order_returns_order_object():
    mock_order = _mock_order(order_id="xyz-789", status="filled")
    mock_client = MagicMock()
    mock_client.get_order_by_id.return_value = mock_order

    sys_ctx, client_ctx = _alpaca_ctx(mock_client)
    with sys_ctx, client_ctx:
        from src.alpaca.orders import get_order
        result = get_order("xyz-789")

    mock_client.get_order_by_id.assert_called_once_with("xyz-789")
    assert result is mock_order


def test_get_order_raises_on_api_error():
    mock_client = MagicMock()
    mock_client.get_order_by_id.side_effect = Exception("connection refused")

    sys_ctx, client_ctx = _alpaca_ctx(mock_client)
    with sys_ctx, client_ctx:
        from src.alpaca.orders import get_order
        with pytest.raises(OrderError, match="Failed to get order"):
            get_order("bad-id")


# ─── get_open_orders_for ─────────────────────────────────────────────────── #


def test_get_open_orders_for_returns_list():
    o1, o2 = _mock_order("a"), _mock_order("b")
    mock_client = MagicMock()
    mock_client.get_orders.return_value = [o1, o2]

    sys_ctx, client_ctx = _alpaca_ctx(mock_client)
    with sys_ctx, client_ctx:
        from src.alpaca.orders import get_open_orders_for
        result = get_open_orders_for("AAPL")

    assert result == [o1, o2]


def test_get_open_orders_for_raises_on_api_error():
    mock_client = MagicMock()
    mock_client.get_orders.side_effect = Exception("timeout")

    sys_ctx, client_ctx = _alpaca_ctx(mock_client)
    with sys_ctx, client_ctx:
        from src.alpaca.orders import get_open_orders_for
        with pytest.raises(OrderError, match="Failed to get open orders"):
            get_open_orders_for("AAPL")


# ─── close_position_full ─────────────────────────────────────────────────── #


def test_close_position_full_calls_close_position():
    mock_order = _mock_order()
    mock_client = MagicMock()
    mock_client.close_position.return_value = mock_order

    sys_ctx, client_ctx = _alpaca_ctx(mock_client)
    with sys_ctx, client_ctx:
        from src.alpaca.orders import close_position_full
        result = close_position_full("AAPL")

    mock_client.close_position.assert_called_once_with("AAPL")
    assert result is mock_order


def test_close_position_full_raises_on_api_error():
    mock_client = MagicMock()
    mock_client.close_position.side_effect = Exception("forbidden")

    sys_ctx, client_ctx = _alpaca_ctx(mock_client)
    with sys_ctx, client_ctx:
        from src.alpaca.orders import close_position_full
        with pytest.raises(OrderError, match="Failed to close position"):
            close_position_full("AAPL")


# ─── No credentials ──────────────────────────────────────────────────────── #


def test_raises_order_error_when_credentials_missing():
    _reset_client()
    with patch.dict(sys.modules, _alpaca_sys_modules()):
        with patch("src.alpaca.orders.config.ALPACA_API_KEY", ""):
            with patch("src.alpaca.orders.config.ALPACA_SECRET_KEY", ""):
                from src.alpaca.orders import place_entry_order
                with pytest.raises(OrderError, match="credentials not configured"):
                    place_entry_order("AAPL", 10, 189.40)
