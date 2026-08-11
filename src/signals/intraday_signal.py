"""
Intraday entry signal + trade plan (upgrade plan P1.3).

A **deterministic** intraday entry decision — not a once-daily composite. An
entry fires only on breakout *confirmation with participation*:

1. **Above VWAP** — price is trading above the session VWAP (buyers in control).
2. **Opening-range breakout** — price has broken above the opening-range high.
3. **Volume confirmation** — relative volume clears the floor (a breakout on thin
   volume is a fakeout).

All three ⇒ ``STRONG_BUY``; VWAP + volume without the ORB ⇒ ``BUY`` (momentum,
not yet a breakout); otherwise ``HOLD`` (no entry). The stop is sized off the
**intraday** ATR, so it reflects intraday volatility rather than a daily range.

Deterministic and reproducible: same bars in, same decision out. Thresholds are
config-driven (``config.INTRADAY_*``). Nothing here assumes profitability — it is
a rule set to be validated, and every entry still passes RiskGuard downstream.
The plan is built by reusing ``generate_trade_plan`` with the intraday frame, so
the entry/stop/target/sizing and the money-path gating are shared with the daily
path (only the inputs are intraday).
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

import pandas as pd

from src.signals.trade_plan import generate_trade_plan
from src.technical.intraday_features import (
    current_session_bars,
    intraday_atr,
    opening_range,
    relative_volume,
    vwap,
)
from src.utils import config
from src.utils.logger import get_logger

log = get_logger(__name__)

_ENTRY_SIGNALS = frozenset({"STRONG_BUY", "BUY"})


def evaluate_intraday_entry(df: pd.DataFrame, now: Optional[datetime] = None) -> dict:
    """Evaluate a deterministic intraday entry on an intraday OHLCV frame.

    Returns a decision dict (also shaped so ``generate_trade_plan`` can consume
    it directly): ``signal``, ``enter``, ``current_price``, ``score``,
    ``confidence``, ``reasons``, and an ``intraday`` feature/trigger block.
    """
    session = current_session_bars(df)
    min_bars = int(config.INTRADAY_MIN_SESSION_BARS)
    if df is None or df.empty or session is None or len(session) < max(2, min_bars):
        return _hold("insufficient session data")

    last_price = float(session["Close"].astype(float).iloc[-1])
    v = vwap(df)
    orange = opening_range(df, minutes=config.INTRADAY_OPENING_RANGE_MINUTES)
    rel_vol = relative_volume(df)
    atr = intraday_atr(df, period=config.INTRADAY_ATR_PERIOD)

    if v is None or orange is None or rel_vol is None:
        return _hold("intraday features unavailable")

    require_vwap = bool(config.INTRADAY_REQUIRE_ABOVE_VWAP)
    min_rel = float(config.INTRADAY_MIN_REL_VOLUME)

    above_vwap = last_price > v
    or_breakout = last_price > orange["high"]
    volume_ok = rel_vol >= min_rel

    reasons: list[str] = []
    if above_vwap:
        reasons.append(f"Above VWAP ({last_price:.2f} > {v:.2f})")
    if or_breakout:
        reasons.append(f"Opening-range breakout (> {orange['high']:.2f})")
    if volume_ok:
        reasons.append(f"Volume {rel_vol:.1f}x session avg")

    vwap_ok = above_vwap or not require_vwap
    if vwap_ok and or_breakout and volume_ok:
        signal = "STRONG_BUY"
    elif vwap_ok and volume_ok and above_vwap:
        signal = "BUY"
    else:
        signal = "HOLD"
        reasons.append("No confirmed breakout with participation")

    # Deterministic 0-100 score from the confirmed triggers (for plan display /
    # ranking — not a probability).
    score = 50.0 + (15 if above_vwap else 0) + (20 if or_breakout else 0) + (15 if volume_ok else 0)
    confidence = "HIGH" if signal == "STRONG_BUY" else "MODERATE" if signal == "BUY" else "LOW"

    intraday = {
        "vwap": round(v, 4),
        "opening_range_high": round(orange["high"], 4),
        "opening_range_low": round(orange["low"], 4),
        "rel_volume": round(rel_vol, 2),
        "atr": round(atr, 4) if atr is not None else None,
        "above_vwap": above_vwap,
        "or_breakout": or_breakout,
        "volume_ok": volume_ok,
        "session_bars": int(len(session)),
    }
    return {
        "signal": signal,
        "enter": signal in _ENTRY_SIGNALS,
        "current_price": round(last_price, 4),
        "score": round(score, 1),
        "confidence": confidence,
        "reasons": reasons,
        "intraday": intraday,
    }


def _hold(reason: str) -> dict:
    return {
        "signal": "HOLD",
        "enter": False,
        "current_price": None,
        "score": 0.0,
        "confidence": "LOW",
        "reasons": [reason],
        "intraday": {},
    }


def build_intraday_plan(
    ticker: str,
    df: pd.DataFrame,
    decision: dict,
    account_size: Optional[float] = None,
) -> dict:
    """Build a trade plan from an intraday entry decision.

    Reuses ``generate_trade_plan`` with the **intraday** frame, so the stop is
    sized off intraday ATR and sizing/targets/gating match the daily path. The
    intraday feature block is attached for transparency. Returns a ``skip``/
    ``error`` dict for non-entry decisions, mirroring the daily planner.
    """
    if not decision.get("enter"):
        return {"ticker": ticker, "signal": decision.get("signal", "HOLD"),
                "skip": True, "reason": "intraday decision is not an entry"}
    plan = generate_trade_plan(ticker, decision, account_size=account_size, df=df)
    if isinstance(plan, dict) and "error" not in plan and not plan.get("skip"):
        plan["intraday"] = decision.get("intraday", {})
        plan["source"] = "intraday"
    return plan
