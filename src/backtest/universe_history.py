"""
Point-in-time universe membership for the backtest (audit M11).

The backtest's ticker list is whatever the caller supplies at run time, which
silently excludes delisted/renamed names — survivorship bias that inflates
reported returns. This module lets a backtest consume a membership-interval
file so entries are only taken in tickers that were actually members of the
universe on the entry date.

File format (JSON), one list of membership intervals per ticker:

    {
      "AAPL": [{"start": "2015-01-01", "end": null}],
      "TWTR": [{"start": "2015-01-01", "end": "2022-10-27"}]
    }

Interval endpoints are inclusive; "end": null means still a member. A ticker
absent from the file is treated as NEVER a member — the file is the authority
on the universe, otherwise the bias this exists to remove sneaks back in
through omissions.

Membership gating only fixes the eligibility half of the problem: price
history for delisted names must still come from the data provider, and many
free sources (yfinance included) drop delisted tickers. The residual gap is
disclosed in the backtest's bias_disclosures.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from src.utils.logger import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class _Interval:
    start: pd.Timestamp
    end: pd.Timestamp | None  # None = open-ended (still a member)


def _naive(ts: pd.Timestamp) -> pd.Timestamp:
    """Normalize to timezone-naive (UTC) — aware/naive comparisons raise in pandas."""
    if ts.tzinfo is not None:
        return ts.tz_convert("UTC").tz_localize(None)
    return ts


class UniverseHistory:
    """Answers "was this ticker in the universe on this date?" queries."""

    def __init__(self, intervals: dict[str, list[_Interval]]):
        self._intervals = intervals

    def __len__(self) -> int:
        return len(self._intervals)

    def tickers(self) -> list[str]:
        """Every ticker the file covers — the universe across the whole period."""
        return sorted(self._intervals)

    def is_member(self, ticker: str, date) -> bool:
        ts = _naive(pd.Timestamp(date))
        for interval in self._intervals.get(ticker.upper(), ()):
            if interval.start <= ts and (interval.end is None or ts <= interval.end):
                return True
        return False


def _parse_date(ticker: str, value, field: str) -> pd.Timestamp:
    try:
        ts = pd.Timestamp(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{ticker}: bad {field} date {value!r}") from exc
    if pd.isna(ts):
        raise ValueError(f"{ticker}: bad {field} date {value!r}")
    return _naive(ts)


def _parse_interval(ticker: str, raw: dict) -> _Interval:
    if not isinstance(raw, dict) or "start" not in raw:
        raise ValueError(f"{ticker}: interval must be an object with a 'start' date")
    start = _parse_date(ticker, raw["start"], "start")
    end_raw = raw.get("end")
    end = None if end_raw is None else _parse_date(ticker, end_raw, "end")
    if end is not None and end < start:
        raise ValueError(f"{ticker}: interval end {end.date()} precedes start {start.date()}")
    return _Interval(start=start, end=end)


def load_universe_history(path: str | None) -> UniverseHistory | None:
    """
    Load a membership file, or None when no path is configured.

    A configured-but-unusable file (missing, malformed JSON, bad dates) also
    returns None with a WARNING: the backtest then falls back to the run-time
    universe and keeps its survivorship disclosure, rather than silently
    running a "point-in-time" test against a broken file.
    """
    if not path:
        return None
    try:
        raw = json.loads(Path(path).read_text())
        if not isinstance(raw, dict):
            raise ValueError("top level must be an object of ticker -> interval list")
        intervals: dict[str, list[_Interval]] = {}
        for ticker, entries in raw.items():
            if not isinstance(entries, list):
                raise ValueError(f"{ticker}: expected a list of intervals")
            intervals[str(ticker).upper()] = [_parse_interval(ticker, e) for e in entries]
        log.info(f"Loaded point-in-time universe history: {len(intervals)} tickers from {path}")
        return UniverseHistory(intervals)
    except (OSError, ValueError) as exc:
        log.warning(
            f"Universe history at {path!r} is unusable ({exc}) — backtest falls back "
            "to the run-time universe (survivorship bias applies)."
        )
        return None
