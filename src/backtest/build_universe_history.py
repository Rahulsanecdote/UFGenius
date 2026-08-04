"""
Build a point-in-time S&P 500 membership file for the backtest (audit M11).

The backtest's survivorship-bias fix (`universe_history.py`) only activates
when a membership file is supplied — this module builds one from Wikipedia's
"List of S&P 500 companies" page, which carries both the current constituents
and a table of historical index changes (date, added, removed).

    python -m src.backtest.build_universe_history --output data/universe_history.json

Reconstruction walks the changes NEWEST → OLDEST from today's membership:
an "added" row resolves the start of an open membership interval; a "removed"
row opens one ending on that date. Tickers still unresolved when the change
history runs out were members since before the data begins — their start is
floored at the earliest change date and means "member since at least here".

Honest limits (disclosed, not hidden):
- Wikipedia's changes table is titled "Selected changes" — early history is
  incomplete, so pre-floor membership is approximated by the floor date.
- Change dates are treated as inclusive on both sides (added on D → member
  from D; removed on D → member through D), a ≤1-day approximation.
- Price history for delisted names still depends on the data provider.
"""

from __future__ import annotations

import argparse
import io
import json
from collections import defaultdict
from pathlib import Path
from typing import Optional

import pandas as pd

from src.utils import config, http
from src.utils.logger import get_logger

log = get_logger(__name__)

WIKI_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
_UA_HEADERS = {"User-Agent": config.CONSTITUENT_FETCH_USER_AGENT}


def _normalize(ticker: str) -> str:
    """Match the scanner's convention: uppercase, dots → dashes (yfinance)."""
    return str(ticker).strip().upper().replace(".", "-")


def _flat_columns(df: pd.DataFrame) -> list[str]:
    """Flatten a table's columns to lowercase strings, joining MultiIndex
    header rows (e.g. ("Added", "Ticker") → "added ticker") so tables can be
    matched by header content rather than position."""
    if isinstance(df.columns, pd.MultiIndex):
        return [
            " ".join(str(part) for part in tup if str(part) != "nan").strip().lower()
            for tup in df.columns
        ]
    return [str(c).strip().lower() for c in df.columns]


def _find_table(tables: list[pd.DataFrame], *needles: str) -> pd.DataFrame:
    """Return the first table whose (flattened) headers each contain one of the
    given needles; raise ValueError if none match. Locates tables by content so
    the parser survives Wikipedia reordering its tables."""
    for table in tables:
        cols = _flat_columns(table)
        if all(any(needle in c for c in cols) for needle in needles):
            return table
    raise ValueError(f"no table with columns matching {needles!r} found")


def _column(df: pd.DataFrame, *needles: str) -> pd.Series:
    """Return the first column whose flattened header contains all needles;
    raise ValueError if none match."""
    cols = _flat_columns(df)
    for idx, name in enumerate(cols):
        if all(needle in name for needle in needles):
            return df.iloc[:, idx]
    raise ValueError(f"no column matching {needles!r} in {cols}")


def parse_wikipedia(html: str) -> tuple[list[str], list[tuple[pd.Timestamp, Optional[str], Optional[str]]]]:
    """Extract (current_members, changes) from the constituents page HTML.

    Changes are (date, added_ticker, removed_ticker) tuples sorted newest
    first; either ticker may be None when only one side changed that day.
    """
    tables = pd.read_html(io.StringIO(html))

    constituents = _find_table(tables, "symbol")
    current = [
        _normalize(t)
        for t in _column(constituents, "symbol").dropna().astype(str)
        if str(t).strip()
    ]

    changes_table = _find_table(tables, "date", "added", "removed")
    dates = _column(changes_table, "date")
    added = _column(changes_table, "added", "ticker")
    removed = _column(changes_table, "removed", "ticker")

    changes: list[tuple[pd.Timestamp, Optional[str], Optional[str]]] = []
    for raw_date, raw_added, raw_removed in zip(dates, added, removed):
        try:
            date = pd.Timestamp(str(raw_date))
        except (TypeError, ValueError):
            continue
        if pd.isna(date):
            continue
        add = _normalize(raw_added) if not pd.isna(raw_added) and str(raw_added).strip() else None
        rem = _normalize(raw_removed) if not pd.isna(raw_removed) and str(raw_removed).strip() else None
        if add or rem:
            changes.append((date, add, rem))

    changes.sort(key=lambda c: c[0], reverse=True)
    return current, changes


def build_membership(
    current: list[str],
    changes: list[tuple[pd.Timestamp, Optional[str], Optional[str]]],
) -> dict[str, list[dict]]:
    """Reconstruct membership intervals by replaying changes newest → oldest."""
    if not current:
        raise ValueError("no current constituents supplied")
    if not changes:
        # No usable change rows means the "selected changes" table wasn't found
        # or parsed. Flooring every current ticker to a default date would emit
        # a file that gates backtests against *current* constituents — silently
        # reintroducing the survivorship bias this builder exists to remove.
        # Fail loudly instead of writing a misleading dataset.
        raise ValueError(
            "no usable historical constituent changes parsed; refusing to emit "
            "a membership file that would reintroduce survivorship bias"
        )

    # ticker -> end of the membership interval whose start is not yet known
    # (None end = still a member today).
    open_end: dict[str, Optional[pd.Timestamp]] = {t: None for t in current}
    intervals: dict[str, list[dict]] = defaultdict(list)

    floor = min(c[0] for c in changes)

    def _emit(ticker: str, start: pd.Timestamp, end: Optional[pd.Timestamp]) -> None:
        """Record one [start, end] membership interval (end=None ⇒ still open)."""
        intervals[ticker].append(
            {
                "start": start.strftime("%Y-%m-%d"),
                "end": end.strftime("%Y-%m-%d") if end is not None else None,
            }
        )

    for date, added, removed in changes:  # newest first
        if added is not None:
            if added in open_end:
                _emit(added, date, open_end.pop(added))
            else:
                # Added but neither a current member nor later-removed — the
                # ticker was likely renamed. Zero-information; skip with a log.
                log.debug(f"{added}: addition on {date.date()} with no open interval; skipped")
        if removed is not None:
            if removed not in open_end:
                open_end[removed] = date
            elif open_end[removed] is None:
                # `removed` is a *current* member (interval still open to today)
                # but the change data carries no matching re-addition —
                # Wikipedia's "selected changes" table is incomplete. The
                # current constituents table is authoritative for present
                # membership, so keep the open interval intact and drop this
                # unplaceable older removal rather than emit history that
                # contradicts current membership (is_member(today) must stay
                # true). The lost older stint is acceptable: the floor already
                # means "member since at least", and the data is admittedly
                # partial.
                log.debug(
                    f"{removed}: removal on {date.date()} with no later re-add; "
                    "keeping current open interval (incomplete change data)"
                )
            else:
                # Two removals with no addition between them — inconsistent
                # data. Keep the newer interval already recorded; skip the
                # older removal rather than clobber it.
                log.debug(
                    f"{removed}: duplicate removal on {date.date()}; "
                    "keeping the newer interval"
                )

    # Whatever is still open predates the change history: floor the start and
    # document the meaning ("member since at least the floor date").
    for ticker, end in open_end.items():
        _emit(ticker, floor, end)

    return {t: sorted(ivs, key=lambda iv: iv["start"]) for t, ivs in intervals.items()}


def build_universe_history_file(output_path: str, html: Optional[str] = None) -> dict:
    """Fetch (or accept) the Wikipedia page, build the file, return a summary."""
    if html is None:
        html = http.get_text(WIKI_URL, headers=_UA_HEADERS)
    current, changes = parse_wikipedia(html)
    membership = build_membership(current, changes)

    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(membership, indent=1, sort_keys=True))

    former = [t for t, ivs in membership.items() if all(iv["end"] is not None for iv in ivs)]
    summary = {
        "output": str(out),
        "tickers": len(membership),
        "current_members": len(current),
        "former_members": len(former),
        "changes_used": len(changes),
        "floor_date": min(iv["start"] for ivs in membership.values() for iv in ivs),
    }
    log.info(
        f"Wrote {summary['tickers']} tickers ({summary['former_members']} former members) "
        f"to {out} from {summary['changes_used']} index changes"
    )
    return summary


def main() -> None:
    """CLI entry point: build the membership file and print a summary."""
    parser = argparse.ArgumentParser(
        description="Build a point-in-time S&P 500 membership file (audit M11)."
    )
    parser.add_argument(
        "--output",
        default="data/universe_history.json",
        help="Where to write the membership JSON (default: data/universe_history.json)",
    )
    args = parser.parse_args()
    summary = build_universe_history_file(args.output)
    print(json.dumps(summary, indent=2))
    print(
        "\nPoint the backtest at it via config.yaml universe_history_path or "
        "BACKTEST_UNIVERSE_HISTORY_PATH.\nNote: Wikipedia's change table is "
        "'selected changes' — early history is approximated by the floor date, "
        "and delisted-name price coverage still depends on your data provider."
    )


if __name__ == "__main__":
    main()
