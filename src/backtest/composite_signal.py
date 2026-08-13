"""Point-in-time replay of the live composite signal, for the backtest (audit B1).

Before this, `src/backtest/engine.py` had **no reference to `src.signals`**: it
entered on a hardcoded `Close > SMA_50 > SMA_200` + RSI-band rule while the live
path traded `generate_signal`'s weighted composite. So `--mode validate` could
never validate the strategy that actually places orders — a VALIDATED verdict
would have cleared a system the harness never tested.

This module closes that gap by replaying the **real scorer** bar by bar:
`evaluate_series()` walks a ticker's history and, at each bar, hands
`generate_signal` a frame sliced to that bar only, collecting the label and score
the live system would have produced from the evidence available at the time.

### What is and is not reconstructible

Only technical and volume are recoverable from history. Sentiment is inherently
*current* (no provider serves the news/social/insider mood of a past date),
and fundamentals and the macro regime are served as of today. Scoring those from
present-day values would inject look-ahead into every bar — the exact defect the
P1.1 guards and the M4 same-bar fix exist to prevent — so `point_in_time=True`
drops them and renormalises the remaining weights (see `generate_signal`).

The honest description of what this replays is therefore the **technical+volume
composite, renormalised** — 55% of live weight carrying 100% of the decision —
using the real scorers, the real `SIGNAL_THRESHOLDS` bands and the real labels.
That is a categorically better proxy than an unrelated SMA/RSI rule, but it is
still a *subset* of the live composite, and every result must say so. Callers get
`dropped_dimensions` for exactly that disclosure.

### No look-ahead

Each evaluation sees `history.loc[:bar]` — never a row beyond the bar being
scored. The caller then shifts the resulting flag by one bar so the fill lands at
the *next* open, preserving the M4 discipline. `tests/test_composite_signal.py`
asserts both properties directly.
"""

from __future__ import annotations

from typing import Any, Callable, Iterable, Optional

import pandas as pd

from src.signals.context import SignalContext
from src.signals.generator import generate_signal
from src.utils import config
from src.utils.logger import get_logger

log = get_logger(__name__)

# generate_signal refuses a frame shorter than this, and the technical scorers
# need a long-MA warmup to be meaningful; bars before it are simply not scored.
MIN_WARMUP_BARS = 200

# Trailing window handed to the scorer at each bar. This matches the LIVE path,
# which builds its context from `period="1y"` (~252 trading days) — so the replay
# sees the same amount of history the real system would have, not an
# ever-growing one. It also keeps cost per bar constant instead of O(bars),
# turning an O(bars²) replay into a linear one.
LOOKBACK_BARS = 252

# Buy-side labels the live executor acts on (`bot.py` submits strong_buys + buys).
ENTRY_LABELS = ("STRONG_BUY", "BUY")


def evaluate_at(
    ticker: str,
    history: pd.DataFrame,
    bar: Any,
    *,
    generator: Callable[..., dict] = generate_signal,
) -> Optional[dict]:
    """Score one bar from the evidence available at that bar. None if unscorable.

    The frame is sliced to ``history.loc[:bar]`` so nothing after ``bar`` is
    visible. Never raises — a scorer failure on one bar degrades to "no signal"
    rather than aborting a multi-thousand-bar replay.
    """
    try:
        window = history.loc[:bar]
    except (KeyError, TypeError) as exc:
        log.debug(f"{ticker}: cannot slice history at {bar}: {exc}")
        return None
    if len(window) < MIN_WARMUP_BARS:
        return None
    # Trailing window only — same history depth the live path scores on.
    if len(window) > LOOKBACK_BARS:
        window = window.tail(LOOKBACK_BARS)

    ctx = SignalContext(
        ticker=ticker,
        price_df=window,
        ticker_info={},
        # Deliberately empty: present-day fundamentals are look-ahead for a past
        # bar, so point_in_time mode skips the fundamentals-derived filters and
        # drops the fundamental weight rather than scoring today's balance sheet.
        fundamentals_raw={},
        instrument=None,
        provider="backtest",
    )
    try:
        return generator(ticker, macro_regime=None, context=ctx, point_in_time=True)
    except Exception as exc:  # one bad bar must not kill the replay
        log.debug(f"{ticker}: point-in-time scoring failed at {bar}: {exc}")
        return None


def evaluate_series(
    ticker: str,
    history: pd.DataFrame,
    bars: Optional[Iterable[Any]] = None,
    *,
    min_score: Optional[float] = None,
    entry_labels: Iterable[str] = ENTRY_LABELS,
    stride: Optional[int] = None,
    generator: Callable[..., dict] = generate_signal,
) -> pd.DataFrame:
    """Replay the composite across ``bars``; return per-bar label/score/entry flag.

    Returns a frame indexed like ``bars`` with columns ``signal_label``,
    ``signal_score`` and ``entry_signal``. ``entry_signal`` is True when the label
    is one the live executor would act on **and** the score clears ``min_score``
    (default `backtest.composite_min_score`), mirroring the live entry rule
    rather than inventing a new one.

    ``stride`` scores only every Nth bar (default `backtest.composite_stride`),
    trading fidelity for runtime: replaying the real scorer costs ~28 ms/bar, so
    a full 503-ticker run is hours at stride 1. Skipped bars simply cannot open a
    position — the honest reading of stride N is "the strategy re-evaluates every
    N bars". With median holds of 26–118 days a weekly stride is a modest
    approximation, but it IS an approximation and the result discloses it.

    The caller is responsible for shifting ``entry_signal`` by one bar to fill at
    the next open — this function never looks past the bar it is scoring.
    """
    if min_score is None:
        min_score = float(config.BACKTEST_COMPOSITE_MIN_SCORE)
    if stride is None:
        stride = int(config.BACKTEST_COMPOSITE_STRIDE)
    stride = max(1, int(stride))
    labels = {str(s).upper() for s in entry_labels}

    full_index = list(history.index if bars is None else bars)
    # Anchor the stride to the END of the series so the most recent bars are
    # always scored — striding from the start would leave the newest bar
    # unevaluated whenever the length is not a multiple of the stride.
    index = full_index[::-1][::stride][::-1] if stride > 1 else full_index
    out_label: list[Optional[str]] = []
    out_score: list[float] = []
    out_entry: list[bool] = []

    for bar in index:
        sig = evaluate_at(ticker, history, bar, generator=generator)
        if not sig:
            out_label.append(None)
            out_score.append(float("nan"))
            out_entry.append(False)
            continue
        label = str(sig.get("signal") or "")
        try:
            score = float(sig.get("score"))
        except (TypeError, ValueError):
            score = float("nan")
        out_label.append(label or None)
        out_score.append(score)
        out_entry.append(bool(label in labels and score == score and score >= min_score))

    frame = pd.DataFrame(
        {"signal_label": out_label, "signal_score": out_score, "entry_signal": out_entry},
        index=pd.Index(index, name=getattr(history.index, "name", None)),
    )
    if len(index) != len(full_index):
        # Reindex back onto every bar. Unscored bars get entry_signal False —
        # never forward-filled, which would let a stale signal open a position on
        # a bar the strategy did not actually evaluate.
        frame = frame.reindex(pd.Index(full_index, name=frame.index.name))
        # Fill before the dtype settles: reindex introduces NaN into the bool
        # column, and .fillna on the resulting object column is deprecated.
        frame["entry_signal"] = frame["entry_signal"].eq(True).astype(bool)
    return frame


def dropped_dimensions(
    ticker: str, history: pd.DataFrame, *, generator: Callable[..., dict] = generate_signal
) -> list[str]:
    """Which composite dimensions this replay could not reconstruct.

    Probed from a real scoring call (rather than hardcoded) so the disclosure
    tracks the scorer's actual behaviour if the weights or modes change.
    """
    if history is None or history.empty:
        return []
    sig = evaluate_at(ticker, history, history.index[-1], generator=generator)
    return list((sig or {}).get("_point_in_time_dropped") or [])
