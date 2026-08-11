"""Tests for the P1.4 calendar-backed earnings source."""

from __future__ import annotations

import json
from datetime import date

import pytest

from src.catalysts.earnings_calendar import EarningsCalendar, _parse_date

ASOF = date(2026, 2, 1)


def _cal(tmp_path, mapping, provider=None):
    p = tmp_path / "ec.json"
    p.write_text(json.dumps(mapping), encoding="utf-8")
    return EarningsCalendar(path=str(p), provider_lookup=provider).load()


def test_days_to_earnings_from_file(tmp_path):
    cal = _cal(tmp_path, {"AAPL": "2026-02-05", "MSFT": "2026-03-01"})
    assert cal.days_to_earnings("AAPL", as_of=ASOF) == 4
    assert cal.days_to_earnings("msft", as_of=ASOF) == 28  # case-insensitive


def test_provider_fallback_when_absent(tmp_path):
    cal = _cal(tmp_path, {"AAPL": "2026-02-05"}, provider=lambda t: 3 if t == "NVDA" else None)
    assert cal.days_to_earnings("NVDA", as_of=ASOF) == 3       # fell back to provider
    assert cal.days_to_earnings("ZZZ", as_of=ASOF) is None     # unknown everywhere


def test_next_earnings_lookup(tmp_path):
    cal = _cal(tmp_path, {"AAPL": "2026-02-05"})
    assert cal.next_earnings("AAPL") == date(2026, 2, 5)
    assert cal.next_earnings("ZZZ") is None


def test_missing_file_uses_provider_only(tmp_path):
    cal = EarningsCalendar(path=str(tmp_path / "nope.json"), provider_lookup=lambda t: 5).load()
    assert cal.days_to_earnings("AAPL", as_of=ASOF) == 5


def test_malformed_file_is_tolerated(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("not json", encoding="utf-8")
    cal = EarningsCalendar(path=str(p), provider_lookup=lambda t: None).load()
    assert cal.days_to_earnings("AAPL", as_of=ASOF) is None

    notobj = tmp_path / "list.json"
    notobj.write_text("[1,2,3]", encoding="utf-8")
    assert EarningsCalendar(path=str(notobj), provider_lookup=None).load().next_earnings("AAPL") is None


def test_bad_date_entries_are_skipped(tmp_path):
    cal = _cal(tmp_path, {"AAPL": "2026-02-05", "BAD": "not-a-date", "NUM": 42})
    assert cal.next_earnings("AAPL") == date(2026, 2, 5)
    assert cal.next_earnings("BAD") is None
    assert cal.next_earnings("NUM") is None


def test_refresh_from_provider_writes_file(tmp_path):
    path = tmp_path / "ec.json"
    cal = EarningsCalendar(path=str(path), provider_lookup=lambda t: {"AAA": 10, "BBB": 20}.get(t))
    written = cal.refresh_from_provider(["AAA", "BBB", "CCC"], as_of=ASOF)
    assert written == 2  # CCC had no date
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["AAA"] == "2026-02-11"  # ASOF + 10 days
    assert "CCC" not in saved


def test_expired_stored_date_falls_back_to_provider(tmp_path):
    # File date already passed → treated as stale → provider consulted so the
    # ticker's NEXT earnings week is not hidden by an obsolete record.
    cal = _cal(tmp_path, {"AAPL": "2026-01-10"}, provider=lambda t: 4)
    assert cal.days_to_earnings("AAPL", as_of=ASOF) == 4  # not -22 from the stale file
    # Future stored date is still used directly.
    cal2 = _cal(tmp_path, {"AAPL": "2026-02-10"}, provider=lambda t: 999)
    assert cal2.days_to_earnings("AAPL", as_of=ASOF) == 9


def test_reloads_when_file_changes(tmp_path):
    import os
    p = tmp_path / "ec.json"
    p.write_text(json.dumps({"AAPL": "2026-02-05"}), encoding="utf-8")
    cal = EarningsCalendar(path=str(p), provider_lookup=None).load()
    assert cal.days_to_earnings("AAPL", as_of=ASOF) == 4
    # Rewrite the file (as an external --mode earnings-calendar run would) and
    # bump its mtime; the next query must reflect the new data without a restart.
    p.write_text(json.dumps({"AAPL": "2026-02-20"}), encoding="utf-8")
    os.utime(p, (p.stat().st_atime + 10, p.stat().st_mtime + 10))
    assert cal.days_to_earnings("AAPL", as_of=ASOF) == 19


def test_refresh_skips_non_finite_provider_values(tmp_path):
    path = tmp_path / "ec.json"
    vals = {"AAA": 5, "NAN": float("nan"), "INF": float("inf"), "BBB": 8}
    cal = EarningsCalendar(path=str(path), provider_lookup=lambda t: vals.get(t))
    written = cal.refresh_from_provider(["AAA", "NAN", "INF", "BBB"], as_of=ASOF)
    assert written == 2  # NaN/inf skipped, not aborting the run
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert set(saved) == {"AAA", "BBB"}


def test_refresh_with_no_provider_writes_empty(tmp_path):
    path = tmp_path / "ec.json"
    cal = EarningsCalendar(path=str(path), provider_lookup=None)
    assert cal.refresh_from_provider(["AAA"], as_of=ASOF) == 0
    assert json.loads(path.read_text(encoding="utf-8")) == {}


def test_parse_date_forms():
    assert _parse_date("2026-02-05") == date(2026, 2, 5)
    assert _parse_date("2026-02-05T00:00:00") == date(2026, 2, 5)
    assert _parse_date("garbage") is None
    assert _parse_date(None) is None
