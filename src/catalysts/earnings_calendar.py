"""
Calendar-backed earnings dates (upgrade plan P1.4).

Upgrades the best-effort, single-field earnings lookup to a real **calendar**: a
``{ticker: "YYYY-MM-DD"}`` JSON file (authoritative, offline, testable) with a
per-ticker provider fallback (the existing yfinance earnings timestamp) when a
ticker is absent from the file. RiskGuard's earnings-week block reads
``days_to_earnings`` from this, so an entry the day before earnings is refused
even for plans that arrived without an earnings timestamp.

The file is the source of truth so the calendar can be pre-built for the whole
universe (see ``refresh_from_provider``) and audited, rather than depending on a
live per-ticker lookup at decision time. Everything degrades gracefully: a
missing/malformed file or a failed provider lookup yields ``None`` (unknown),
which RiskGuard treats as fail-open-but-logged, exactly as before.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from datetime import date, datetime
from pathlib import Path
from typing import Callable, Optional

from src.utils import config
from src.utils.logger import get_logger

log = get_logger(__name__)


def _yfinance_days_to_earnings(ticker: str) -> Optional[int]:
    """Provider fallback: the existing yfinance earnings-timestamp lookup."""
    try:
        from src.data.fetcher import fetch_ticker_info_yfinance
        from src.signals.trade_plan import _days_to_earnings

        return _days_to_earnings(fetch_ticker_info_yfinance(ticker))
    except Exception as exc:
        log.debug(f"[{ticker}] yfinance earnings lookup failed: {exc}")
        return None


def _parse_date(value) -> Optional[date]:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value).date()
    except ValueError:
        try:
            return datetime.strptime(value, "%Y-%m-%d").date()
        except (ValueError, TypeError):
            return None


class EarningsCalendar:
    """A ticker → next-earnings-date map, file-backed with a provider fallback."""

    def __init__(
        self,
        path: Optional[str] = None,
        provider_lookup: Callable[[str], Optional[int]] = _yfinance_days_to_earnings,
    ) -> None:
        self._path = str(path or config.CATALYST_EARNINGS_CALENDAR_PATH)
        self._provider = provider_lookup
        self._dates: dict[str, date] = {}
        self._loaded = False
        self._mtime: Optional[float] = None
        self._lock = threading.RLock()

    @staticmethod
    def _file_mtime(path: str) -> Optional[float]:
        try:
            p = Path(path)
            return p.stat().st_mtime if p.exists() else None
        except OSError:
            return None

    def load(self) -> "EarningsCalendar":
        """Load the calendar file. Missing/malformed → empty (provider fallback only)."""
        with self._lock:
            self._loaded = True
            self._dates = {}
            self._mtime = self._file_mtime(self._path)
            p = Path(self._path)
            if not p.exists():
                return self
            try:
                raw = json.loads(p.read_text(encoding="utf-8"))
            except Exception as exc:
                log.error(f"Earnings calendar unreadable ({self._path}): {exc}")
                return self
            if not isinstance(raw, dict):
                log.error(f"Earnings calendar {self._path} is not a JSON object; ignoring")
                return self
            for ticker, val in raw.items():
                d = _parse_date(val)
                if d is not None:
                    self._dates[str(ticker).upper()] = d
            return self

    def _ensure_loaded(self) -> None:
        """(Re)load if never loaded or the file changed on disk since last load.

        The mtime check makes ``default_calendar()`` pick up an external
        ``--mode earnings-calendar`` refresh without a process restart, so a
        long-running executor never risks blocking/allowing on stale dates.
        """
        with self._lock:
            if not self._loaded or self._file_mtime(self._path) != self._mtime:
                self.load()

    def next_earnings(self, ticker: str) -> Optional[date]:
        """The next earnings date from the file, or None if absent."""
        self._ensure_loaded()
        with self._lock:
            return self._dates.get(str(ticker).upper())

    def days_to_earnings(self, ticker: str, as_of: Optional[date] = None) -> Optional[int]:
        """Days until the ticker's next earnings, or None if unknown.

        File first (calendar-backed); if the ticker is absent — OR its stored
        date has already passed (a stale record) — fall back to the provider
        lookup. Returning a negative value would suppress the fallback and let an
        obsolete record hide the ticker's *next* earnings week. ``as_of`` defaults
        to today (injectable for tests).
        """
        as_of = as_of or date.today()
        d = self.next_earnings(ticker)
        if d is not None:
            days = (d - as_of).days
            if days >= 0:
                return days
            # Stored date is in the past → stale; fall through to the provider.
        if self._provider is not None:
            try:
                return self._provider(ticker)
            except Exception as exc:
                log.debug(f"[{ticker}] earnings provider fallback failed: {exc}")
        return None

    def refresh_from_provider(self, tickers: list[str], as_of: Optional[date] = None) -> int:
        """Populate the calendar from the provider for ``tickers`` and save.

        Best-effort bulk build: for each ticker with a known days-to-earnings,
        store as_of + days as the date. Saves periodically so an interrupted run
        (hundreds of sequential provider calls) keeps what it already fetched.
        Returns the number of dates written.
        """
        import math
        from datetime import timedelta

        as_of = as_of or date.today()
        with self._lock:
            self._ensure_loaded()
            written = 0
            if self._provider is None:
                self._save_locked()
                return written
            total = len(tickers)
            for i, ticker in enumerate(tickers, 1):
                try:
                    days = self._provider(ticker)
                except Exception:
                    days = None
                # Guard non-finite: int(nan)/int(inf) raise and would abort the
                # whole refresh mid-way.
                if isinstance(days, (int, float)) and math.isfinite(days):
                    self._dates[str(ticker).upper()] = as_of + timedelta(days=int(days))
                    written += 1
                if i % 50 == 0:
                    log.info(f"Earnings calendar refresh: {i}/{total} ({written} dated) …")
                    self._save_locked()  # periodic checkpoint
            self._save_locked()
            return written

    def _save_locked(self) -> None:
        p = Path(self._path)
        os.makedirs(p.parent, exist_ok=True)
        payload = {t: d.isoformat() for t, d in sorted(self._dates.items())}
        fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".ec-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
            os.replace(tmp, str(p))
            # This instance just wrote the file; adopt its mtime so the next
            # _ensure_loaded doesn't reload the data we already hold.
            self._mtime = self._file_mtime(self._path)
        except Exception as exc:
            log.error(f"Failed to save earnings calendar: {exc}", exc_info=True)
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass


_default: Optional[EarningsCalendar] = None
_default_lock = threading.Lock()


def default_calendar() -> EarningsCalendar:
    """Process-wide calendar loaded from the configured path (lazy singleton)."""
    global _default
    with _default_lock:
        if _default is None:
            _default = EarningsCalendar().load()
        return _default
