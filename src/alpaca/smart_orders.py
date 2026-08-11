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


def smart_entry_price(reference: float, measured: Optional[float] = None) -> float:
    """The marketable-limit BUY price for an entry at ``reference``.

    ``reference`` is the plan's intended entry price. When smart orders are
    disabled this returns the reference unchanged (plain limit).
    """
    if not bool(config.SMART_ORDERS_ENABLED):
        return round(float(reference), 2)
    off = entry_offset_pct(measured)
    price = marketable_limit_price("buy", reference, off)
    log.debug(f"smart entry: ref={reference} +{off:.4%} → marketable limit {price}")
    return price
