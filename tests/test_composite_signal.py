"""Tests for the point-in-time composite replay (audit B1).

The two properties that matter most:

* **No look-ahead** — a bar's score must depend only on data up to that bar.
  `test_score_is_unchanged_by_future_bars` proves it by scoring the same bar in a
  truncated frame and in a frame with wildly different future appended.
* **The live path is untouched** — `point_in_time` defaults to False everywhere,
  so production scoring is byte-for-byte what it was.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

import src.utils.config as cfg
from src.backtest.composite_signal import (
    ENTRY_LABELS,
    LOOKBACK_BARS,
    MIN_WARMUP_BARS,
    dropped_dimensions,
    evaluate_at,
    evaluate_series,
)
from src.signals.filters import run_disqualification_filters


def _frame(n: int = 400, *, drift: float = 0.0009, seed: int = 3) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2022-01-03", periods=n)
    px = 100 * np.exp(np.cumsum(rng.normal(drift, 0.013, n)))
    return pd.DataFrame(
        {"Open": px * 0.999, "High": px * 1.008, "Low": px * 0.992, "Close": px,
         "Volume": rng.integers(2e6, 6e6, n).astype(float)},
        index=idx,
    )


# ── no look-ahead (the property the whole module exists for) ──────────────────

def test_score_is_unchanged_by_future_bars():
    """Scoring bar T must give the same answer whatever happens after T."""
    df = _frame(400)
    bar = df.index[300]

    truncated = df.loc[:bar]
    crash = df.copy()
    crash.loc[crash.index > bar, ["Open", "High", "Low", "Close"]] *= 0.4  # -60% after T
    spike = df.copy()
    spike.loc[spike.index > bar, ["Open", "High", "Low", "Close"]] *= 3.0  # +200% after T

    base = evaluate_at("T", truncated, bar)
    assert base is not None
    for variant, name in ((crash, "crash"), (spike, "spike")):
        got = evaluate_at("T", variant, bar)
        assert got is not None
        assert got["score"] == base["score"], f"future {name} leaked into bar T"
        assert got["signal"] == base["signal"], f"future {name} leaked into bar T"


def test_series_entry_flags_ignore_the_future():
    df = _frame(400)
    cut = df.index[350]
    full = evaluate_series("T", df).loc[:cut, "entry_signal"]
    partial = evaluate_series("T", df.loc[:cut])["entry_signal"]
    pd.testing.assert_series_equal(full, partial, check_names=False)


# ── the live path must be unchanged ───────────────────────────────────────────

def test_filters_default_still_enforce_fundamentals():
    """Without point_in_time, an unknown market cap still disqualifies (live)."""
    reasons = run_disqualification_filters("T", _frame(60), fundamental_score={})
    assert any("UNKNOWN_MARKET_CAP" in r for r in reasons)


def test_point_in_time_skips_only_the_fundamentals_checks():
    df = _frame(60)
    reasons = run_disqualification_filters("T", df, fundamental_score={}, point_in_time=True)
    assert not any("MARKET_CAP" in r for r in reasons)
    assert not any("BANKRUPTCY" in r for r in reasons)


def test_point_in_time_keeps_the_price_derived_checks():
    """Chaser-trap and liquidity gates are computable from price — they stay on."""
    df = _frame(60)
    df.loc[df.index[-1], "Close"] = float(df["Close"].iloc[-6]) * 5  # +400% in 5 days
    reasons = run_disqualification_filters("T", df, fundamental_score={}, point_in_time=True)
    assert any("CHASER_TRAP" in r for r in reasons)

    thin = _frame(60)
    thin["Volume"] = 10.0
    assert any(
        "ILLIQUID" in r
        for r in run_disqualification_filters("T", thin, fundamental_score={}, point_in_time=True)
    )


def test_generate_signal_defaults_to_live_mode():
    """point_in_time must default False so production scoring is unchanged."""
    import inspect

    from src.signals.generator import generate_signal

    assert inspect.signature(generate_signal).parameters["point_in_time"].default is False


# ── weight handling ───────────────────────────────────────────────────────────

def test_non_reconstructible_dimensions_are_dropped():
    assert sorted(dropped_dimensions("T", _frame(300))) == ["fundamental", "macro", "sentiment"]


def test_surviving_weights_are_renormalised_to_one():
    sig = evaluate_at("T", _frame(300), _frame(300).index[-1])
    assert sig is not None
    w = sig["_weights"]
    assert w["sentiment"] == 0.0 and w["fundamental"] == 0.0 and w["macro"] == 0.0
    assert sum(w.values()) == pytest.approx(1.0)
    # technical + volume carry the whole decision, in their original proportion.
    assert w["technical"] == pytest.approx(0.35 / 0.55, rel=1e-6)
    assert w["volume"] == pytest.approx(0.20 / 0.55, rel=1e-6)


def test_scores_are_not_compressed_toward_neutral():
    """Dropping the placeholders must leave a real spread for the label bands."""
    rep = evaluate_series("T", _frame(500))
    scores = rep["signal_score"].dropna()
    assert scores.max() - scores.min() > 20, "composite collapsed toward a constant"
    assert rep["signal_label"].dropna().nunique() > 1


def test_sentiment_sources_are_never_called_in_point_in_time_mode(monkeypatch):
    import src.signals.generator as G

    def _boom(*a, **k):  # pragma: no cover - must never run
        raise AssertionError("sentiment source called during historical replay")

    monkeypatch.setattr(G, "analyze_news_sentiment", _boom)
    monkeypatch.setattr(G, "analyze_social_sentiment", _boom)
    monkeypatch.setattr(G, "analyze_insider_activity", _boom)
    monkeypatch.setattr(G, "detect_market_regime", _boom)  # current regime = leak
    assert evaluate_at("T", _frame(300), _frame(300).index[-1]) is not None


# ── windowing / warmup ────────────────────────────────────────────────────────

def test_bars_before_warmup_are_unscored():
    df = _frame(400)
    assert evaluate_at("T", df, df.index[MIN_WARMUP_BARS - 2]) is None
    rep = evaluate_series("T", df)
    assert rep["signal_label"].iloc[: MIN_WARMUP_BARS - 1].isna().all()
    assert not rep["entry_signal"].iloc[: MIN_WARMUP_BARS - 1].any()


def test_window_is_capped_to_the_live_lookback():
    """The scorer must see at most LOOKBACK_BARS, like the live period='1y'."""
    seen = {}

    def _spy(ticker, macro_regime=None, context=None, point_in_time=False, **kw):
        seen["rows"] = len(context.price_df)
        return {"signal": "HOLD", "score": 50.0}

    df = _frame(600)
    evaluate_at("T", df, df.index[-1], generator=_spy)
    assert seen["rows"] == LOOKBACK_BARS


# ── entry rule ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("label,score,expected", [
    ("STRONG_BUY", 85.0, True),
    ("BUY", 70.0, True),
    ("BUY", 60.0, False),      # below min_score
    ("WEAK_BUY", 90.0, False),  # not an entry label
    ("HOLD", 90.0, False),
    ("SELL", 90.0, False),
])
def test_entry_flag_mirrors_the_live_entry_rule(label, score, expected):
    df = _frame(300)
    gen = lambda *a, **k: {"signal": label, "score": score}  # noqa: E731
    rep = evaluate_series("T", df, min_score=65.0, generator=gen)
    assert bool(rep["entry_signal"].iloc[-1]) is expected


def test_entry_labels_match_what_the_executor_acts_on():
    assert set(ENTRY_LABELS) == {"STRONG_BUY", "BUY"}


def test_scorer_failure_degrades_to_no_signal():
    def _boom(*a, **k):
        raise RuntimeError("scorer exploded")

    df = _frame(300)
    assert evaluate_at("T", df, df.index[-1], generator=_boom) is None
    rep = evaluate_series("T", df, generator=_boom)
    assert not rep["entry_signal"].any()


# ── stride ────────────────────────────────────────────────────────────────────

def test_stride_reindexes_to_every_bar_without_forward_filling():
    df = _frame(400)
    gen = lambda *a, **k: {"signal": "BUY", "score": 90.0}  # noqa: E731
    rep = evaluate_series("T", df, stride=5, generator=gen)
    assert len(rep) == len(df), "strided result must cover every bar"
    entries = int(rep["entry_signal"].sum())
    every_bar = int(evaluate_series("T", df, stride=1, generator=gen)["entry_signal"].sum())
    assert 0 < entries < every_bar, "stride should score fewer bars, not none or all"
    # Skipped bars must be False, never a carried-over stale signal.
    assert rep["entry_signal"].iloc[0] == False  # noqa: E712


def test_stride_always_scores_the_most_recent_bar():
    df = _frame(403)  # length deliberately not a multiple of the stride
    gen = lambda *a, **k: {"signal": "BUY", "score": 90.0}  # noqa: E731
    rep = evaluate_series("T", df, stride=5, generator=gen)
    assert bool(rep["entry_signal"].iloc[-1]) is True


def test_stride_is_floored_at_one(monkeypatch):
    df = _frame(300)
    gen = lambda *a, **k: {"signal": "BUY", "score": 90.0}  # noqa: E731
    assert len(evaluate_series("T", df, stride=0, generator=gen)) == len(df)
    assert len(evaluate_series("T", df, stride=-5, generator=gen)) == len(df)


def test_stride_defaults_from_config(monkeypatch):
    monkeypatch.setattr(cfg, "BACKTEST_COMPOSITE_STRIDE", 10)
    calls = []

    def _gen(*a, **k):
        calls.append(1)
        return {"signal": "HOLD", "score": 50.0}

    df = _frame(400)
    evaluate_series("T", df, generator=_gen)
    assert len(calls) < len(df) / 5
