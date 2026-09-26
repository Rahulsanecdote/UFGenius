"""Pre-expansion (spike precursor) detection on intraday bars.

The movers path is structurally *late*: a name reaches it only after it has
already moved enough to appear on a provider's gainers list. The catalyst wire
(`src/catalysts/catalyst_alerts.py`) attacks that from the news side. This
module attacks it from the **price-structure** side — the measurable state a
tape is in *before* the vertical part of a move, not after.

**This is not prediction, and the wording matters.** What it detects is a
volatility *contraction*: range compressing on drying volume, price held in the
upper part of the day's range, and then the first bar whose range and volume
both expand out of that coil. Contraction is a reliable precursor of expansion
in the weak sense that expansion has to come out of *something* — but it says
nothing about DIRECTION, and most coils resolve into noise. Treated as a
trigger it would be actively harmful; treated as "look at this one now" it buys
attention a few bars earlier than a magnitude-ranked scanner can.

Four features, deliberately, with one or two parameters each:

1. **Range compression** — recent true range vs the prior baseline. The core.
2. **Volume dry-up** — participation falling *during* the compression, which is
   what separates a coil from a dead tape.
3. **Expansion trigger** — the newest bar breaking the coil's high on a volume
   multiple. Compression alone is a watch state; this is the "now" part.
4. **Upper-range position + VWAP** — holding the top of the day's range rather
   than grinding down it, so the resolution being *looked for* is upward.

Why not a candlestick-pattern library: individual candle shapes on 1m/5m
microcap bars are mostly microstructure (a wick on 40k shares of a $2 stock
describes one order, not intent), the published out-of-sample evidence for them
is weak, and "every pattern × every parameter" is a search space that reliably
produces something beautiful in-sample and worthless out of it. This repo
already fights that — `--mode optimize` carries an overfitting haircut and
`candidate_ranking: rotate` exists because alphabetical order was letting ticker
*names* pick trades.

**Warm-up matters more than any threshold here.** `warmup_bars()` is 28 at the
defaults, which is 28 minutes on 1m bars and 140 minutes on 5m. Replaying MSGY
(2026-09-25, $2.13 -> $5.95 between 09:30 and 11:07) at 5m produced nothing at
all, because the detector had not accumulated enough history to have an opinion
until after the move was over. **Run it on 1m.**

**Default off.** Discovery/alerting only — no executor or broker import, and
the money path is reached only through the same gated `generate_trade_plan`
every other entry uses. Judge it with `--mode intraday-backtest --entry
precursor` BEFORE enabling it anywhere, then on measured alert outcomes.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

import numpy as np
import pandas as pd

from src.technical.intraday_features import (
    current_session_bars,
    relative_volume,
    vwap,
)
from src.utils import config
from src.utils.logger import get_logger

log = get_logger(__name__)

_ENTRY_SIGNALS = {"BUY", "STRONG_BUY"}


def _true_range(df: pd.DataFrame) -> pd.Series:
    """Wilder true range, which counts the gap between bars.

    Plain high-low understates a coil that is stepping between bars rather than
    inside them, and that understatement biases the compression ratio toward
    looking tighter than the tape really is.
    """
    high = df["High"].astype(float)
    low = df["Low"].astype(float)
    prev_close = df["Close"].astype(float).shift(1)
    return pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)


def _finite(value) -> Optional[float]:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if np.isfinite(out) else None


def warmup_bars() -> int:
    """Bars needed before anything can be measured at all.

    Worth stating plainly because it decides whether this module can see the
    moves it was built for. At the defaults (coil 6, baseline 20) it is 28 bars,
    and 28 bars is a very different amount of *time* per interval:

        1m  ->  28 min  -> first measurement ~09:58 ET
        5m  -> 140 min  -> first measurement ~11:50 ET

    MSGY on 2026-09-25 ran its entire $2.13 -> $5.95 leg between 09:30 and
    11:07. On 5m bars this detector was structurally blind to it — not because
    the structure was absent but because it had not yet accumulated enough
    history to have an opinion. **This is a 1-minute tool.** Run it on 5m and
    it will be quiet through exactly the window morning momentum happens in.
    """
    coil_n = max(2, int(config.PRECURSOR_COIL_BARS))
    base_n = max(coil_n + 1, int(config.PRECURSOR_BASELINE_BARS))
    return base_n + coil_n + 2


def detect_precursor(df: pd.DataFrame) -> Optional[dict]:
    """Measure the coil/expansion state of a frame, or None if unmeasurable.

    Pure: thresholds come from config, the frame is the only input, and nothing
    here decides whether to alert — that is ``evaluate_precursor``'s job.
    """
    session = current_session_bars(df)
    coil_n = max(2, int(config.PRECURSOR_COIL_BARS))
    base_n = max(coil_n + 1, int(config.PRECURSOR_BASELINE_BARS))
    # Three non-overlapping windows, newest first:
    #   [trigger]  the single newest bar — the expansion being tested
    #   [coil]     the `coil_n` bars before it — the compression
    #   [baseline] the `base_n` bars before THAT — the reference range
    # The trigger must sit outside the coil. Including it would put its large
    # range into the coil's own average, so the coil would look widest exactly
    # when it fires and the compression ratio would fight the trigger. The
    # baseline must sit outside the coil for the same reason in reverse: a
    # baseline containing the compression is a reference dragged toward it.
    # One extra bar so the oldest baseline bar's true range has a prior close.
    if session is None or len(session) < warmup_bars():
        return None

    tr = _true_range(session)
    trigger_tr = _finite(tr.iloc[-1])
    coil_tr = tr.iloc[-(coil_n + 1):-1]
    base_tr = tr.iloc[-(coil_n + 1 + base_n):-(coil_n + 1)]
    coil_atr = _finite(coil_tr.mean())
    base_atr = _finite(base_tr.mean())
    if coil_atr is None or base_atr is None or base_atr <= 0 or coil_atr <= 0:
        return None

    volumes = session["Volume"].astype(float)
    coil_vol = _finite(volumes.iloc[-(coil_n + 1):-1].mean())
    base_vol = _finite(volumes.iloc[-(coil_n + 1 + base_n):-(coil_n + 1)].mean())

    closes = session["Close"].astype(float)
    highs = session["High"].astype(float)
    lows = session["Low"].astype(float)
    last_close = _finite(closes.iloc[-1])
    last_vol = _finite(volumes.iloc[-1])
    if last_close is None:
        return None

    coil_high = _finite(highs.iloc[-(coil_n + 1):-1].max())
    coil_low = _finite(lows.iloc[-(coil_n + 1):-1].min())

    day_high = _finite(highs.max())
    day_low = _finite(lows.min())
    day_range = (day_high - day_low) if (day_high is not None and day_low is not None) else None
    range_pos = None
    if day_range is not None and day_range > 0:
        range_pos = (last_close - day_low) / day_range

    session_vwap = vwap(df)
    # Risk unit vs round-trip friction. A coil low sits very close to price by
    # construction, so on a high-priced name the entire risk unit can be
    # SMALLER than the cost of getting in and out — at which point the trade is
    # losing arithmetic before the signal has any say. Measured here, enforced
    # in evaluate_precursor.
    risk = (last_close - coil_low) if coil_low is not None else None
    cost = last_close * (float(config.BACKTEST_COMMISSION_PCT)
                         + float(config.BACKTEST_SLIPPAGE_PCT)) * 2.0
    return {
        "compression": round(coil_atr / base_atr, 4),
        "coil_atr": round(coil_atr, 4),
        "baseline_atr": round(base_atr, 4),
        "volume_dryup": (round(coil_vol / base_vol, 4)
                         if coil_vol is not None and base_vol not in (None, 0) else None),
        "expansion_volume": (round(last_vol / coil_vol, 4)
                             if last_vol is not None and coil_vol not in (None, 0) else None),
        "expansion_range": (round(trigger_tr / coil_atr, 4)
                            if trigger_tr is not None else None),
        "coil_high": round(coil_high, 4) if coil_high is not None else None,
        "coil_low": round(coil_low, 4) if coil_low is not None else None,
        "range_position": round(range_pos, 4) if range_pos is not None else None,
        "above_vwap": (last_close > session_vwap) if session_vwap is not None else None,
        "vwap": round(session_vwap, 4) if session_vwap is not None else None,
        "rel_volume": relative_volume(df),
        "last_price": round(last_close, 4),
        "coil_bars": coil_n,
        "baseline_bars": base_n,
        "risk_pct": (round(risk / last_close * 100.0, 4)
                     if risk is not None and last_close else None),
        "round_trip_cost_pct": round(cost / last_close * 100.0, 4) if last_close else None,
        "risk_cost_multiple": (round(risk / cost, 3)
                               if risk is not None and cost > 0 else None),
    }


def coil_present(df: pd.DataFrame) -> bool:
    """True when the frame is merely COMPRESSED — the cheap structural test.

    A superset of the graded entry, so a producer can enqueue on this and let
    the consumer run the full grading, the way `sweep_reclaim_present` does.
    """
    try:
        m = detect_precursor(df)
    except Exception:  # detection must never break a scan
        return False
    if not m:
        return False
    return m["compression"] <= float(config.PRECURSOR_MAX_COMPRESSION)


def evaluate_precursor(df: pd.DataFrame, now: Optional[datetime] = None) -> dict:
    """Grade a frame's pre-expansion state into a decision dict.

    Same shape as the other intraday evaluators (``signal`` / ``enter`` /
    ``reasons`` / a feature block), so ``generate_trade_plan`` and the intraday
    backtest can consume it unchanged. ``precursor.stop_hint`` is an ABSOLUTE
    level — the coil's low — because that is where the premise fails: back
    inside the range means the expansion did not hold.
    """
    m = detect_precursor(df)
    if not m:
        return _hold("insufficient bars to measure compression")

    compressed = m["compression"] <= float(config.PRECURSOR_MAX_COMPRESSION)
    dry = (m["volume_dryup"] is not None
           and m["volume_dryup"] <= float(config.PRECURSOR_MAX_VOLUME_DRYUP))
    vol_pop = (m["expansion_volume"] is not None
               and m["expansion_volume"] >= float(config.PRECURSOR_MIN_EXPANSION_VOLUME))
    range_pop = (m["expansion_range"] is not None
                 and m["expansion_range"] >= float(config.PRECURSOR_MIN_EXPANSION_RANGE))
    broke_coil = (m["coil_high"] is not None and m["last_price"] > m["coil_high"])
    high_in_range = (m["range_position"] is not None
                     and m["range_position"] >= float(config.PRECURSOR_MIN_RANGE_POSITION))
    vwap_ok = (m["above_vwap"] is True) or not bool(config.PRECURSOR_REQUIRE_ABOVE_VWAP)
    # Cost floor. The stop must be far enough away that round-trip friction is
    # a fraction of the risk unit rather than a multiple of it. Backtesting the
    # detector on 50 S&P names (1m, 2026-09-21..25) returned profit factor 0.06
    # and an average loss of -2.83R -- not a verdict on the signal but on the
    # geometry: a measured trade risked $0.625/share on a $339 stock (0.184% of
    # price) against $1.356 of modelled round-trip cost (0.400%), so friction
    # was 2.17x the whole risk unit. A perfect entry stopping out exactly at
    # its stop still loses ~2R, and a 2R target nets nothing.
    #
    # At multiple N a loss costs about (1 + 1/N)R and a 2R target nets about
    # (2 - 1/N)R, so N=2 is roughly 1.5R against 1.5R and N=3 is 1.67R against
    # 1.33R. Higher is better economics and fewer setups. This is DERIVED from
    # the cost model, not fitted to returns -- the distinction that keeps it
    # from being the curve-fitting this module exists to avoid.
    min_mult = float(config.PRECURSOR_MIN_RISK_COST_MULTIPLE)
    risk_ok = (min_mult <= 0
               or (m["risk_cost_multiple"] is not None
                   and m["risk_cost_multiple"] >= min_mult))

    reasons: list[str] = []
    if compressed:
        reasons.append(f"Range compressed to {m['compression']:.2f}x its baseline")
    if dry:
        reasons.append(f"Volume dried up to {m['volume_dryup']:.2f}x during the coil")
    if broke_coil:
        reasons.append(f"Broke the coil high ({m['coil_high']:.2f})")
    if vol_pop:
        reasons.append(f"Expansion bar on {m['expansion_volume']:.1f}x coil volume")
    if range_pop:
        reasons.append(f"Expansion bar range {m['expansion_range']:.1f}x the coil")
    if high_in_range:
        reasons.append(f"Holding the top of the day's range ({m['range_position']:.0%})")

    # The coil alone is a WATCH state, never an entry: compression resolves in
    # both directions, so firing on it would be a coin flip dressed as a signal.
    # An entry needs the coil AND a break of it AND participation behind the
    # break — the part that is at least directional.
    expanding = broke_coil and vol_pop and range_pop
    # Every one of these is REQUIRED. `high_in_range` used to be a tiebreak
    # between BUY and STRONG_BUY, which let a coil at the BOTTOM of the day's
    # range enter — a bounce attempt, the opposite animal from a continuation
    # out of strength, and flatly against what this detector claims to look
    # for. Volume dry-up is the only genuine grade here: it is what separates a
    # coil from a tape that merely went quiet.
    if compressed and expanding and high_in_range and vwap_ok and risk_ok:
        signal = "STRONG_BUY" if dry else "BUY"
    else:
        signal = "HOLD"
        if not compressed:
            reasons.append("No volatility contraction to expand out of")
        elif not expanding:
            reasons.append("Coiled but not yet expanding — watch, not an entry")
        elif not high_in_range:
            reasons.append("Coiled low in the day's range — not a continuation setup")
        elif not vwap_ok:
            reasons.append("Below VWAP")
        elif not risk_ok:
            mult = m["risk_cost_multiple"]
            reasons.append(
                f"Stop too tight to pay for itself — risk {m['risk_pct']:.3f}% of price "
                f"vs {m['round_trip_cost_pct']:.3f}% round-trip cost"
                + (f" ({mult:.2f}x, need {min_mult:.1f}x)" if mult is not None else ""))

    score = (
        50.0
        + (12 if compressed else 0)
        + (10 if dry else 0)
        + (12 if broke_coil else 0)
        + (8 if vol_pop else 0)
        + (5 if range_pop else 0)
        + (3 if high_in_range else 0)
    )
    return {
        "signal": signal,
        "enter": signal in _ENTRY_SIGNALS,
        "current_price": m["last_price"],
        "score": round(min(100.0, score), 1),
        "confidence": ("HIGH" if signal == "STRONG_BUY"
                       else "MODERATE" if signal == "BUY" else "LOW"),
        "reasons": reasons,
        "precursor": {**m, "stop_hint": m["coil_low"], "compressed": compressed,
                      "expanding": expanding},
    }


def _hold(reason: str) -> dict:
    return {
        "signal": "HOLD",
        "enter": False,
        "current_price": None,
        "score": 0.0,
        "confidence": "LOW",
        "reasons": [reason],
        "precursor": {},
    }
