"""Named screener presets — pre-trade filters that turn a large universe into a
short, criteria-matched watchlist (the disciplined version of "scroll through
100s of charts every morning").

A screener finds *candidates*; it does not confer an edge. Presets are filters,
not trade signals — the output feeds the scan / intraday-entry logic, which
(and only after out-of-sample validation) decides anything about money.
"""

from src.screener.features import compute_screen_features
from src.screener.screener import (
    ScreenResult,
    evaluate_preset,
    screen_ticker,
    screen_universe,
)

__all__ = [
    "ScreenResult",
    "compute_screen_features",
    "evaluate_preset",
    "screen_ticker",
    "screen_universe",
]
