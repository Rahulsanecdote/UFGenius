"""Tests for backtest entry-candidate ranking (audit B2).

The bug: `_entry_candidates_for_date` returned plain alphabetical order, and the
caller fills scarce position slots from the head of that list. Ticker *name*
therefore decided which trades the strategy took — on a 503-name S&P universe all
57 out-of-sample trades landed on A-names, so widening the universe could not
change the result.

The regression test that matters is `test_selection_is_not_alphabetically_biased`:
with far more qualifying candidates than slots, the selected set must not be
monopolised by the alphabetically-first names.
"""

from __future__ import annotations

import pandas as pd
import pytest

import src.utils.config as cfg
from src.backtest.engine import _entry_candidates_for_date, _rank_candidates


def _frame(date: pd.Timestamp, *, close: float = 100.0, sma200: float = 90.0) -> pd.DataFrame:
    return pd.DataFrame(
        {"enter_flag": [True], "Close": [close], "SMA_200": [sma200]}, index=[date]
    )


def _histories(tickers: list[str], date: pd.Timestamp) -> dict[str, pd.DataFrame]:
    return {t: _frame(date) for t in tickers}


DATE = pd.Timestamp("2024-03-15")
# 60 tickers spread across the alphabet, deliberately built so the alphabetical
# head is a large, easily-detected block (A* names).
ALPHA_HEAD = [f"A{i:02d}" for i in range(30)]
REST = [f"{c}ZZ" for c in "BCDEFGHIJKLMNOPQRSTUVWXYZ"]
ALL = ALPHA_HEAD + REST


@pytest.fixture(autouse=True)
def _default_ranking(monkeypatch):
    monkeypatch.setattr(cfg, "BACKTEST_CANDIDATE_RANKING", "rotate")


# ── the regression ────────────────────────────────────────────────────────────

def test_selection_is_not_alphabetically_biased():
    """Slots are scarce; the winners must not all be A-names.

    Simulates the real fill loop: take the first 5 candidates each day. Under the
    old `sorted()` ordering this returned only A* names on every date.
    """
    picked: set[str] = set()
    for day in pd.date_range("2024-01-01", periods=60, freq="D"):
        ranked = _entry_candidates_for_date(_histories(ALL, day), {}, day)
        picked.update(ranked[:5])  # the caller fills 5 slots from the head

    from_head = {t for t in picked if t in ALPHA_HEAD}
    from_rest = {t for t in picked if t in REST}
    assert from_rest, "no non-A ticker was ever selected — alphabetical bias is back"
    # The head is 30/55 of the pool, so a fair sampler lands well under 90% here.
    share = len(from_head) / len(picked)
    assert share < 0.9, f"alphabetical head took {share:.0%} of selections"


def test_every_candidate_is_reachable_over_time():
    """No qualifying ticker should be structurally excluded from ever trading."""
    picked: set[str] = set()
    for day in pd.date_range("2024-01-01", periods=400, freq="D"):
        picked.update(_entry_candidates_for_date(_histories(ALL, day), {}, day)[:5])
    missing = set(ALL) - picked
    assert not missing, f"never selectable: {sorted(missing)[:10]}"


# ── determinism (the harnesses depend on it) ──────────────────────────────────

def test_ranking_is_reproducible_across_calls():
    a = _entry_candidates_for_date(_histories(ALL, DATE), {}, DATE)
    b = _entry_candidates_for_date(_histories(ALL, DATE), {}, DATE)
    assert a == b


def test_ranking_differs_across_dates():
    other = pd.Timestamp("2024-03-16")
    a = _entry_candidates_for_date(_histories(ALL, DATE), {}, DATE)
    b = _entry_candidates_for_date(_histories(ALL, other), {}, other)
    assert a != b, "ordering is date-independent — rotation is not happening"


def test_ranking_is_a_permutation_not_a_filter():
    ranked = _entry_candidates_for_date(_histories(ALL, DATE), {}, DATE)
    assert sorted(ranked) == sorted(ALL)


# ── modes ─────────────────────────────────────────────────────────────────────

def test_alphabetical_mode_reproduces_legacy_order(monkeypatch):
    monkeypatch.setattr(cfg, "BACKTEST_CANDIDATE_RANKING", "alphabetical")
    ranked = _entry_candidates_for_date(_histories(ALL, DATE), {}, DATE)
    assert ranked == sorted(ALL)


def test_momentum_mode_ranks_strongest_trend_first(monkeypatch):
    monkeypatch.setattr(cfg, "BACKTEST_CANDIDATE_RANKING", "momentum")
    hist = {
        "ZWEAK": _frame(DATE, close=100.0, sma200=99.0),    # +1%
        "ASTRONG": _frame(DATE, close=150.0, sma200=100.0),  # +50%
        "MMID": _frame(DATE, close=120.0, sma200=100.0),     # +20%
    }
    assert _entry_candidates_for_date(hist, {}, DATE) == ["ASTRONG", "MMID", "ZWEAK"]


def test_momentum_mode_survives_bad_rows(monkeypatch):
    """A missing/zero SMA must not raise — it sorts last."""
    monkeypatch.setattr(cfg, "BACKTEST_CANDIDATE_RANKING", "momentum")
    hist = {
        "GOOD": _frame(DATE, close=150.0, sma200=100.0),
        "ZERO": _frame(DATE, close=100.0, sma200=0.0),
        "NAN": _frame(DATE, close=float("nan"), sma200=100.0),
    }
    ranked = _entry_candidates_for_date(hist, {}, DATE)
    assert ranked[0] == "GOOD"
    assert sorted(ranked) == ["GOOD", "NAN", "ZERO"]


def test_unknown_mode_falls_back_to_neutral_rotation(monkeypatch):
    monkeypatch.setattr(cfg, "BACKTEST_CANDIDATE_RANKING", "nonsense-mode")
    ranked = _entry_candidates_for_date(_histories(ALL, DATE), {}, DATE)
    assert sorted(ranked) == sorted(ALL)
    assert ranked != sorted(ALL), "unknown mode silently kept the biased order"


# ── the existing gates still apply ────────────────────────────────────────────

def test_open_positions_and_non_signal_bars_are_excluded():
    hist = _histories(["AAA", "BBB", "CCC"], DATE)
    hist["CCC"].loc[DATE, "enter_flag"] = False
    ranked = _entry_candidates_for_date(hist, {"BBB": object()}, DATE)
    assert sorted(ranked) == ["AAA"]


def test_missing_date_is_skipped():
    hist = {"AAA": _frame(DATE), "BBB": _frame(pd.Timestamp("2020-01-01"))}
    assert _entry_candidates_for_date(hist, {}, DATE) == ["AAA"]


def test_short_lists_pass_through_untouched():
    assert _rank_candidates([], {}, DATE) == []
    assert _rank_candidates(["ONE"], {}, DATE) == ["ONE"]


# ── momentum must not peek at the fill bar (PR #57 review) ───────────────────

def _two_bar_frame(date, *, prior_close, prior_sma, fill_close) -> pd.DataFrame:
    """Signal bar then fill bar. Ranking must read the SIGNAL bar."""
    prior = date - pd.Timedelta(days=1)
    return pd.DataFrame(
        {
            "enter_flag": [True, True],
            "Close": [prior_close, fill_close],
            "SMA_200": [prior_sma, prior_sma],
        },
        index=[prior, date],
    )


def test_momentum_ranks_on_the_signal_bar_not_the_fill_bar(monkeypatch):
    """The fill bar's close is unknown at the open the entry fills at.

    WEAK is weaker on the signal bar but explodes on the fill bar. Ranking on the
    fill bar would promote it — that is same-bar look-ahead deciding which trades
    get taken, since the caller fills scarce slots from the head of this list.
    """
    monkeypatch.setattr(cfg, "BACKTEST_CANDIDATE_RANKING", "momentum")
    hist = {
        "STRONG": _two_bar_frame(DATE, prior_close=150.0, prior_sma=100.0, fill_close=151.0),
        "WEAK": _two_bar_frame(DATE, prior_close=101.0, prior_sma=100.0, fill_close=500.0),
    }
    assert _entry_candidates_for_date(hist, {}, DATE) == ["STRONG", "WEAK"]


def test_momentum_prefers_the_precomputed_shifted_column(monkeypatch):
    """When the engine supplies mom_entry, ranking uses it verbatim."""
    monkeypatch.setattr(cfg, "BACKTEST_CANDIDATE_RANKING", "momentum")
    hist = {}
    for name, mom, close in (("A", 0.02, 999.0), ("B", 0.40, 1.0)):
        f = _frame(DATE, close=close, sma200=100.0)
        f["mom_entry"] = [mom]
        hist[name] = f
    # B wins on the shifted column despite a far lower current close.
    assert _entry_candidates_for_date(hist, {}, DATE) == ["B", "A"]


def test_momentum_without_a_prior_bar_sorts_last(monkeypatch):
    monkeypatch.setattr(cfg, "BACKTEST_CANDIDATE_RANKING", "momentum")
    hist = {
        "NOPRIOR": _frame(DATE, close=500.0, sma200=100.0),  # single bar only
        "OK": _two_bar_frame(DATE, prior_close=120.0, prior_sma=100.0, fill_close=121.0),
    }
    assert _entry_candidates_for_date(hist, {}, DATE)[0] == "OK"
