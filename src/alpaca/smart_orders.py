"""
Smart order handling (upgrade plan P2.2).

A plain limit at the plan's (slightly discounted) price is a *passive* order — it
often does not fill when price moves away, so a validated signal is missed. Smart
order handling prices the entry as a **marketable limit**: a limit that crosses
the market by a small, bounded offset, so it fills promptly *without* chasing
into unbounded slippage.

The crossing offset is **tuned to the P2.1 measured slippage** — we cross by
about as much as fills have actually been costing, clamped to a floor (so the
limit is genuinely marketable) and a hard cap (so a bad measurement or a fast
market can never make us chase further than configured). The plan's price stays
the accounting benchmark, so the execution-quality ledger still measures shortfall
against what we *intended*, not against the marketable limit we submitted.

Pure functions (no I/O beyond reading config + the measured slippage); default
off, so with `smart_orders.enabled = false` the entry price is unchanged.
"""

from __future__ import annotations

from typing import Optional

from src.utils import config
from src.utils.logger import get_logger

log = get_logger(__name__)


def _clamp(value: float, lo: float, hi: float) -> float:
    lo, hi = (lo, hi) if lo <= hi else (hi, lo)
    return max(lo, min(hi, value))


def entry_offset_pct(measured: Optional[float] = None) -> float:
    """The marketable crossing offset for an entry, as a fraction.

    Starts from the P2.1 measured slippage (or 0 when unavailable) and clamps it
    to ``[floor, cap]`` — the floor keeps the limit marketable, the cap bounds how
    far we will ever chase. ``measured`` is injectable for testing.
    """
    floor = float(config.SMART_ORDERS_ENTRY_OFFSET_FLOOR_PCT)
    cap = float(config.SMART_ORDERS_ENTRY_OFFSET_CAP_PCT)
    if measured is None:
        try:
            from src.alpaca.execution_quality import measured_slippage_pct

            measured = measured_slippage_pct()
        except Exception as exc:
            log.debug(f"measured slippage unavailable for smart order: {exc}")
            measured = None
    base = float(measured) if measured is not None else 0.0
    return _clamp(base, floor, cap)


def marketable_limit_price(side: str, reference: float, offset_pct: float) -> float:
    """A limit that crosses the market by ``offset_pct`` (buy up / sell down).

    Rounded to 2dp (US equity tick above $1). Returns the reference unchanged for
    a non-positive reference (nothing sensible to price against).
    """
    ref = float(reference)
    if ref <= 0:
        return round(ref, 2)
    off = max(0.0, float(offset_pct))
    priced = ref * (1 + off) if str(side).lower() == "buy" else ref * (1 - off)
    return round(priced, 2)


def smart_entry_price(
    market_price: float,
    accounting_price: Optional[float] = None,
    measured: Optional[float] = None,
) -> float:
    """The marketable-limit BUY price for an entry.

    Crosses the **current market** (``market_price``, e.g. the plan's
    ``reference_price``) by the measured-slippage offset, so the limit is
    genuinely marketable — a plain limit set below the market never fills. The
    result is then **capped relative to the accounting price** (the plan's
    intended entry): we never chase more than ``entry_offset_cap_pct`` above what
    we planned to pay. When smart orders are disabled, returns the accounting
    price unchanged (plain limit at the plan price).

    ``accounting_price`` defaults to ``market_price`` when not supplied.
    """
    accounting = float(accounting_price if accounting_price is not None else market_price)
    if not bool(config.SMART_ORDERS_ENABLED):
        return round(accounting, 2)
    market = float(market_price)
    if market <= 0:
        return round(accounting, 2)
    off = entry_offset_pct(measured)
    crossed = marketable_limit_price("buy", market, off)      # cross the market
    cap = max(0.0, float(config.SMART_ORDERS_ENTRY_OFFSET_CAP_PCT))
    ceiling = round(accounting * (1 + cap), 2)                 # cap chase vs plan price
    price = min(crossed, ceiling)
    log.debug(
        f"smart entry: market={market} +{off:.4%} → {crossed}, "
        f"cap {ceiling} (acct {accounting}) → {price}"
    )
    return price
