"""
Sweep-reclaim intraday entry signal (liquidity-grab reversal timing).

A **deterministic** intraday *reversal* entry — the counterpart to the momentum
breakout in ``src/signals/intraday_signal.py``. The setup is the classic
stop-hunt / liquidity-grab reversal:

1. **Swing low** — the lowest low over the ``lookback`` bars that precede the
   recent window is the level where resting stop orders cluster.
2. **Sweep** — within the last ``reclaim_window`` bars, price dips *below* that
   swing low (a wick that grabs the liquidity below it).
3. **Reclaim** — the current bar closes back *above* the swept level: the
   breakdown failed, buyers stepped in.
4. **Volume confirmation** — the reclaim carries above-average participation (a
   reclaim on thin volume is unconvincing).

Sweep + reclaim + volume ⇒ ``STRONG_BUY``; sweep + reclaim without the volume
confirmation ⇒ ``BUY`` (unless ``require_volume``); otherwise ``HOLD`` (no entry).
An over-extended reclaim (price already far above the swept level) is rejected —
entering there means chasing with a stop too far below to give a sane R:R.

The stop sits just **below the swept wick**: if price falls back through the low
that was swept, the reversal thesis is wrong. That stop is fed into the shared,
gated ``generate_trade_plan`` (targets/sizing/RiskGuard identical to every other
path) by converting the stop distance into the ATR input the planner consumes,
so nothing about the money path is special-cased here.

Deterministic and reproducible: same bars in, same decision out. Thresholds are
config-driven (``config.SWEEP_*``). Nothing here assumes profitability — it is a
timing hypothesis to be validated out-of-sample before real money, and every
entry still passes RiskGuard downstream. **Default-off** (``SWEEP_RECLAIM_ENABLED``,
consulted by the consumer, not here — the evaluator stays pure for testing).
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

import pandas as pd

from src.signals.trade_plan import generate_trade_plan
from src.technical.intraday_features import current_session_bars, relative_volume
from src.utils import config
from src.utils.logger import get_logger

log = get_logger(__name__)

_ENTRY_SIGNALS = frozenset({"STRONG_BUY", "BUY"})


def evaluate_sweep_reclaim(df: pd.DataFrame, now: Optional[datetime] = None) -> dict:
    """Evaluate a deterministic sweep-reclaim reversal entry on an intraday frame.

    Returns a decision dict shaped so ``build_sweep_reclaim_plan`` (and, through
    it, ``generate_trade_plan``) can consume it directly: ``signal``, ``enter``,
    ``current_price``, ``score``, ``confidence``, ``reasons``, and a ``sweep``
    feature/trigger block (including the ``stop_hint`` below the swept wick).
    Never raises. The ``now`` argument is accepted for signature parity with the
    other intraday evaluators; session slicing is anchored on the last bar's date.
    """
    lookback = max(1, int(config.SWEEP_LOOKBACK_BARS))
    window = max(1, int(config.SWEEP_RECLAIM_WINDOW_BARS))
    min_bars = int(config.SWEEP_MIN_SESSION_BARS)
    # Need a full reference window (lookback) that sits entirely BEFORE the recent
    # reclaim window, plus the recent window itself.
    need = lookback + window

    session = current_session_bars(df)
    if df is None or df.empty or session is None or len(session) < max(need, min_bars):
        return _hold("insufficient session data for sweep-reclaim")

    low = session["Low"].astype(float)
    close = session["Close"].astype(float)
    n = len(session)

    # Reference swing low: min Low over the `lookback` bars immediately preceding
    # the recent `window` bars (so the level being swept is established BEFORE the
    # sweep, not contaminated by it).
    ref = low.iloc[n - window - lookback: n - window]
    recent_low = low.iloc[n - window:]
    if ref.empty or recent_low.empty:
        return _hold("insufficient session data for sweep-reclaim")

    swing_low = float(ref.min())
    sweep_low = float(recent_low.min())        # the wick that grabbed liquidity
    reclaim_close = float(close.iloc[-1])
    current_price = reclaim_close

    swept = sweep_low < swing_low               # pierced below the level
    reclaimed = reclaim_close > swing_low       # closed back above it

    extension = (reclaim_close - swing_low) / swing_low if swing_low > 0 else None
    max_ext = float(config.SWEEP_MAX_RECLAIM_EXTENSION_PCT)
    too_extended = extension is not None and extension > max_ext

    rel_vol = relative_volume(df)
    min_rel = float(config.SWEEP_MIN_REL_VOLUME)
    volume_ok = rel_vol is not None and rel_vol >= min_rel
    require_vol = bool(config.SWEEP_REQUIRE_VOLUME)

    reasons: list[str] = []
    if swept:
        reasons.append(f"Swept swing low {swing_low:.2f} (wick to {sweep_low:.2f})")
    if reclaimed:
        reasons.append(f"Reclaimed {swing_low:.2f} (close {reclaim_close:.2f})")
    if volume_ok and rel_vol is not None:
        reasons.append(f"Volume {rel_vol:.1f}x session avg")

    setup = swept and reclaimed and not too_extended
    if setup and volume_ok:
        signal = "STRONG_BUY"
    elif setup and not require_vol:
        signal = "BUY"
    else:
        signal = "HOLD"
        if not swept:
            reasons.append("No sweep of the swing low")
        elif not reclaimed:
            reasons.append("Low swept but not reclaimed (breakdown, not reversal)")
        elif too_extended:
            reasons.append(f"Reclaim over-extended ({extension:.1%} above level) — chasing")
        elif require_vol and not volume_ok:
            reasons.append("Reclaim on thin volume — participation required")

    # Stop just BELOW the swept wick — a fall back through it invalidates the setup.
    buffer = max(0.0, float(config.SWEEP_STOP_BUFFER_PCT))
    stop_hint = round(sweep_low * (1.0 - buffer), 4)

    # Deterministic 0-100 score from the confirmed triggers (display/ranking only).
    score = 50.0 + (20 if swept else 0) + (20 if reclaimed else 0) \
        + (10 if volume_ok else 0) - (15 if too_extended else 0)
    score = max(0.0, min(100.0, score))
    confidence = "HIGH" if signal == "STRONG_BUY" else "MODERATE" if signal == "BUY" else "LOW"

    sweep = {
        "swing_low": round(swing_low, 4),
        "sweep_low": round(sweep_low, 4),
        "reclaim_close": round(reclaim_close, 4),
        "rel_volume": round(rel_vol, 2) if rel_vol is not None else None,
        "stop_hint": stop_hint,
        "extension_pct": round(extension * 100, 2) if extension is not None else None,
        "lookback_bars": lookback,
        "reclaim_window_bars": window,
        "swept": swept,
        "reclaimed": reclaimed,
        "volume_ok": volume_ok,
        "session_bars": n,
    }
    return {
        "signal": signal,
        "enter": signal in _ENTRY_SIGNALS,
        "current_price": round(current_price, 4),
        "score": round(score, 1),
        "confidence": confidence,
        "reasons": reasons,
        "sweep": sweep,
    }


def _hold(reason: str) -> dict:
    return {
        "signal": "HOLD",
        "enter": False,
        "current_price": None,
        "score": 0.0,
        "confidence": "LOW",
        "reasons": [reason],
        "sweep": {},
    }


def build_sweep_reclaim_plan(
    ticker: str,
    df: pd.DataFrame,
    decision: dict,
    account_size: Optional[float] = None,
) -> dict:
    """Build a trade plan from a sweep-reclaim entry decision.

    Reuses ``generate_trade_plan`` with the **intraday** frame, but pins the stop
    to the swept-wick level rather than a pure ATR distance: the stop distance is
    converted into the ATR input the planner consumes so that
    ``entry - ATR × multiplier`` lands just below the swept low. Targets, sizing,
    and RiskGuard gating are then identical to every other path. Returns a
    ``skip``/``error`` dict for non-entry decisions, mirroring the daily planner.
    """
    if not decision.get("enter"):
        return {"ticker": ticker, "signal": decision.get("signal", "HOLD"),
                "skip": True, "reason": "sweep-reclaim decision is not an entry"}

    sweep = decision.get("sweep") or {}
    stop_hint = sweep.get("stop_hint")
    current_price = decision.get("current_price")
    multiplier = float(config.ATR_STOP_MULTIPLIER)

    decision = dict(decision)
    if stop_hint is not None and current_price and multiplier > 0 and "volatility" not in decision:
        # generate_trade_plan sets entry = current_price * 0.998, then
        # stop = entry - ATR * multiplier. Back out the ATR that lands the stop at
        # the sweep-based stop_hint, so the swept-wick stop drives sizing/targets.
        entry_approx = round(float(current_price) * 0.998, 2)
        atr_equiv = (entry_approx - float(stop_hint)) / multiplier
        if atr_equiv > 0:
            decision["volatility"] = {"ATR_14": pd.Series([atr_equiv])}

    plan = generate_trade_plan(ticker, decision, account_size=account_size, df=df)
    if isinstance(plan, dict) and "error" not in plan and not plan.get("skip"):
        plan["sweep"] = sweep
        plan["source"] = "sweep_reclaim"
        if isinstance(plan.get("stop_loss"), dict) and sweep.get("sweep_low") is not None:
            plan["stop_loss"]["method"] = (
                f"Below swept low ${sweep['sweep_low']:.2f} (sweep-reclaim reversal)"
            )
    return plan
