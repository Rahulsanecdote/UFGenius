"""
Low-level Alpaca order execution primitives.

All functions raise OrderError on failure. Callers are responsible for
catching and deciding whether to retry or abort.
"""

from __future__ import annotations

import threading

from src.utils import config
from src.utils.logger import get_logger

log = get_logger(__name__)

_client = None  # Module-level singleton
_client_lock = threading.Lock()


class OrderError(Exception):
    """Raised when an Alpaca order operation fails."""


def _get_client():
    """Return a cached TradingClient. Raises OrderError if credentials missing."""
    global _client
    if _client is not None:
        return _client

    # Double-checked locking: safe when main thread and monitor thread both
    # call order functions for the first time concurrently.
    with _client_lock:
        if _client is not None:
            return _client

        api_key = config.ALPACA_API_KEY
        api_secret = config.ALPACA_SECRET_KEY
        if not (api_key and api_secret):
            raise OrderError(
                "Alpaca credentials not configured (ALPACA_API_KEY / ALPACA_SECRET_KEY)"
            )

        try:
            from alpaca.trading.client import TradingClient
        except ImportError as exc:
            raise OrderError("alpaca-py not installed — run: pip install alpaca-py") from exc

        _client = TradingClient(api_key, api_secret, paper=config.ALPACA_PAPER)
    return _client


def _reset_client() -> None:
    """Clear the cached client (used in tests to inject a fresh mock)."""
    global _client
    with _client_lock:
        _client = None


def place_entry_order(symbol: str, shares: int, limit_price: float):
    """
    Submit a DAY LIMIT buy order.

    Args:
        symbol:      Ticker symbol (e.g. "AAPL").
        shares:      Number of shares to buy.
        limit_price: Limit price (rounded to 2 dp).

    Returns:
        alpaca Order object.

    Raises:
        OrderError: On validation failure or Alpaca API error.
    """
    if shares <= 0:
        raise OrderError(f"shares must be positive, got {shares}")
    if limit_price <= 0:
        raise OrderError(f"limit_price must be positive, got {limit_price}")

    try:
        from alpaca.trading.requests import LimitOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce
    except ImportError as exc:
        raise OrderError("alpaca-py not installed") from exc

    req = LimitOrderRequest(
        symbol=symbol,
        qty=shares,
        side=OrderSide.BUY,
        time_in_force=TimeInForce.DAY,
        limit_price=round(limit_price, 2),
    )
    try:
        order = _get_client().submit_order(req)
        log.info(
            f"Entry order submitted: {symbol} x{shares} LIMIT @ ${limit_price:.2f}"
            f" — id={order.id}"
        )
        return order
    except OrderError:
        raise
    except Exception as exc:
        raise OrderError(f"Failed to submit entry order for {symbol}: {exc}") from exc


def place_stop_order(symbol: str, shares: int, stop_price: float):
    """
    Submit a GTC STOP sell order.

    Args:
        symbol:     Ticker symbol.
        shares:     Number of shares to sell on stop trigger.
        stop_price: Trigger price.

    Returns:
        alpaca Order object.

    Raises:
        OrderError: On validation failure or Alpaca API error.
    """
    if shares <= 0:
        raise OrderError(f"shares must be positive, got {shares}")
    if stop_price <= 0:
        raise OrderError(f"stop_price must be positive, got {stop_price}")

    try:
        from alpaca.trading.requests import StopOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce
    except ImportError as exc:
        raise OrderError("alpaca-py not installed") from exc

    req = StopOrderRequest(
        symbol=symbol,
        qty=shares,
        side=OrderSide.SELL,
        time_in_force=TimeInForce.GTC,
        stop_price=round(stop_price, 2),
    )
    try:
        order = _get_client().submit_order(req)
        log.info(
            f"Stop order submitted: {symbol} x{shares} STOP @ ${stop_price:.2f}"
            f" — id={order.id}"
        )
        return order
    except OrderError:
        raise
    except Exception as exc:
        raise OrderError(f"Failed to submit stop order for {symbol}: {exc}") from exc


def place_limit_sell(symbol: str, shares: int, limit_price: float):
    """
    Submit a GTC LIMIT sell order for a partial exit tranche.

    Args:
        symbol:      Ticker symbol.
        shares:      Number of shares to sell at this target.
        limit_price: Target exit price.

    Returns:
        alpaca Order object.

    Raises:
        OrderError: On validation failure or Alpaca API error.
    """
    if shares <= 0:
        raise OrderError(f"shares must be positive, got {shares}")
    if limit_price <= 0:
        raise OrderError(f"limit_price must be positive, got {limit_price}")

    try:
        from alpaca.trading.requests import LimitOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce
    except ImportError as exc:
        raise OrderError("alpaca-py not installed") from exc

    req = LimitOrderRequest(
        symbol=symbol,
        qty=shares,
        side=OrderSide.SELL,
        time_in_force=TimeInForce.GTC,
        limit_price=round(limit_price, 2),
    )
    try:
        order = _get_client().submit_order(req)
        log.info(
            f"Limit sell submitted: {symbol} x{shares} @ ${limit_price:.2f}"
            f" — id={order.id}"
        )
        return order
    except OrderError:
        raise
    except Exception as exc:
        raise OrderError(f"Failed to submit limit sell for {symbol}: {exc}") from exc


def cancel_order(order_id: str) -> bool:
    """
    Cancel an order by ID.

    Returns:
        True  — order successfully cancelled.
        False — order was already filled/cancelled (422 response); treated as a no-op.

    Raises:
        OrderError: For unexpected API failures.
    """
    try:
        _get_client().cancel_order_by_id(order_id)
        log.debug(f"Order {order_id} cancelled")
        return True
    except OrderError:
        raise
    except Exception as exc:
        msg = str(exc)
        # HTTP 422 = already filled or cancelled — not a real error
        if "422" in msg or "unprocessable" in msg.lower():
            log.debug(f"Order {order_id} already closed (422) — no-op")
            return False
        raise OrderError(f"Failed to cancel order {order_id}: {exc}") from exc


def get_order(order_id: str):
    """
    Fetch current state of an order.

    Returns:
        alpaca Order object.

    Raises:
        OrderError: On API failure.
    """
    try:
        return _get_client().get_order_by_id(order_id)
    except OrderError:
        raise
    except Exception as exc:
        raise OrderError(f"Failed to get order {order_id}: {exc}") from exc


def get_open_orders_for(symbol: str) -> list:
    """
    Return list of open orders for a symbol.

    Raises:
        OrderError: On API failure.
    """
    try:
        from alpaca.trading.requests import GetOrdersRequest
        from alpaca.trading.enums import QueryOrderStatus
    except ImportError as exc:
        raise OrderError("alpaca-py not installed") from exc

    try:
        req = GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[symbol])
        return list(_get_client().get_orders(req))
    except OrderError:
        raise
    except Exception as exc:
        raise OrderError(f"Failed to get open orders for {symbol}: {exc}") from exc


def close_position_full(symbol: str):
    """
    Market-close the entire open position (emergency exit).

    Returns:
        alpaca Order object.

    Raises:
        OrderError: On API failure.
    """
    try:
        order = _get_client().close_position(symbol)
        log.warning(f"Emergency close submitted: {symbol} — full position liquidated")
        return order
    except OrderError:
        raise
    except Exception as exc:
        raise OrderError(f"Failed to close position {symbol}: {exc}") from exc
