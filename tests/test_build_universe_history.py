"""Tests for the point-in-time membership file builder (audit M11 dataset)."""

from __future__ import annotations

import json

import pandas as pd
import pytest

from src.backtest.build_universe_history import (
    build_membership,
    build_universe_history_file,
    parse_wikipedia,
)
from src.backtest.universe_history import load_universe_history

# Shaped like the real page: a constituents table plus a two-row-header
# changes table (pd.read_html turns the latter into MultiIndex columns).
_FIXTURE_HTML = """
<html><body>
<table>
  <tr><th>Symbol</th><th>Security</th></tr>
  <tr><td>AAA</td><td>Alpha Corp</td></tr>
  <tr><td>BBB</td><td>Beta Corp</td></tr>
  <tr><td>BRK.B</td><td>Berkshire</td></tr>
</table>
<table>
  <tr><th rowspan="2">Date</th><th colspan="2">Added</th><th colspan="2">Removed</th><th rowspan="2">Reason</th></tr>
  <tr><th>Ticker</th><th>Security</th><th>Ticker</th><th>Security</th></tr>
  <tr><td>June 1, 2023</td><td>BBB</td><td>Beta Corp</td><td>OLD</td><td>Old Co</td><td>Acquisition</td></tr>
  <tr><td>March 2, 2020</td><td>OLD</td><td>Old Co</td><td></td><td></td><td>Growth</td></tr>
</table>
</body></html>
"""


def test_parse_wikipedia_extracts_members_and_changes():
    current, changes = parse_wikipedia(_FIXTURE_HTML)
    assert current == ["AAA", "BBB", "BRK-B"]  # dot normalized to dash
    assert changes[0] == (pd.Timestamp("2023-06-01"), "BBB", "OLD")  # newest first
    assert changes[1] == (pd.Timestamp("2020-03-02"), "OLD", None)


def test_build_membership_reconstructs_intervals():
    current, changes = parse_wikipedia(_FIXTURE_HTML)
    membership = build_membership(current, changes)

    # BBB joined on the 2023 change date.
    assert membership["BBB"] == [{"start": "2023-06-01", "end": None}]
    # OLD was a member from its 2020 addition until BBB replaced it.
    assert membership["OLD"] == [{"start": "2020-03-02", "end": "2023-06-01"}]
    # AAA predates the change history — floored at the earliest change date.
    assert membership["AAA"] == [{"start": "2020-03-02", "end": None}]


def test_output_round_trips_through_loader(tmp_path):
    path = tmp_path / "hist.json"
    summary = build_universe_history_file(str(path), html=_FIXTURE_HTML)

    hist = load_universe_history(str(path))
    assert hist is not None and len(hist) == summary["tickers"] == 4
    assert summary["former_members"] == 1  # OLD

    # The whole point: a delisted name is a member inside its window only.
    assert hist.is_member("OLD", "2021-06-15")
    assert not hist.is_member("OLD", "2024-01-01")
    assert hist.is_member("BBB", "2024-01-01")
    assert not hist.is_member("BBB", "2022-01-01")


def test_rejoining_ticker_gets_two_intervals():
    current = ["T"]
    changes = [  # newest first: re-added 2022, removed 2018, added 2010
        (pd.Timestamp("2022-01-10"), "T", None),
        (pd.Timestamp("2018-05-05"), None, "T"),
        (pd.Timestamp("2010-02-02"), "T", None),
    ]
    membership = build_membership(current, changes)
    assert membership["T"] == [
        {"start": "2010-02-02", "end": "2018-05-05"},
        {"start": "2022-01-10", "end": None},
    ]


def test_addition_without_open_interval_is_skipped():
    # Renamed tickers appear as additions with no later removal and no current
    # membership — zero information, must not crash or invent intervals.
    current = ["A"]
    changes = [(pd.Timestamp("2020-01-01"), "GHOST", None)]
    membership = build_membership(current, changes)
    assert "GHOST" not in membership
    assert membership["A"][0]["end"] is None


def test_empty_constituents_fail_loudly():
    with pytest.raises(ValueError):
        build_membership([], [])


def test_empty_changes_fail_loudly():
    # No parsed changes ⇒ flooring every current ticker to a default date would
    # gate backtests against current constituents and silently reintroduce
    # survivorship bias. The builder must refuse rather than emit that file.
    with pytest.raises(ValueError):
        build_membership(["AAA", "BBB"], [])


def test_current_member_missing_readd_keeps_open_interval(tmp_path):
    # Wikipedia's "selected changes" table is incomplete: it can list an old
    # removal for a ticker that is still a current member while omitting the
    # later re-addition. The current constituents table is authoritative, so the
    # generated history must NEVER contradict it — every current ticker keeps an
    # open interval and gates as a present-day member.
    current = ["T", "U"]
    changes = [
        (pd.Timestamp("2018-05-05"), None, "T"),  # old removal of a current member, no re-add
        (pd.Timestamp("2015-01-01"), "U", None),  # normal: U added, floors below it
    ]
    membership = build_membership(current, changes)

    # T retains an open interval despite the orphan removal.
    assert membership["T"] == [{"start": "2015-01-01", "end": None}]
    # Invariant: every current ticker has an interval still open today.
    for ticker in current:
        assert any(iv["end"] is None for iv in membership[ticker])

    # And it gates as a current member through the real loader.
    path = tmp_path / "hist.json"
    path.write_text(json.dumps(membership, indent=1, sort_keys=True))
    hist = load_universe_history(str(path))
    assert hist is not None and hist.is_member("T", "2026-08-04")


def test_written_file_is_valid_sorted_json(tmp_path):
    path = tmp_path / "hist.json"
    build_universe_history_file(str(path), html=_FIXTURE_HTML)
    raw = json.loads(path.read_text())
    assert list(raw.keys()) == sorted(raw.keys())


@pytest.mark.integration
def test_live_wikipedia_build(tmp_path):
    summary = build_universe_history_file(str(tmp_path / "live.json"))
    assert summary["current_members"] > 400
    assert summary["former_members"] > 50
