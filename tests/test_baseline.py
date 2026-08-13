"""Tests for the validated-backtest baseline and the paper-vs-backtest gate.

Covers the `docs/UPGRADE_PLAN.md` acceptance criterion "live paper performance
matches the validated backtest within tolerance": baseline persistence, the
one-sided tolerance comparison, its fail-closed refusals, and enforcement on the
RiskGuard live path. All offline — no network, no broker.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

import src.utils.config as cfg
from src.alpaca.executor import RiskGuard
from src.alpaca.position_tracker import PositionTracker
from src.alpaca.scorecard import meets_live_performance_gate
from src.backtest.baseline import (
    baseline_gate,
    build_baseline,
    compare_paper_to_baseline,
    load_baseline,
    save_baseline,
)

NOW = datetime(2026, 8, 12, tzinfo=timezone.utc)


def _validation_result(*, validated: bool = True, win_rate=50.0, profit_factor=2.0) -> dict:
    return {
        "split": {"in_sample": "2022-01-01 → 2023-06-01",
                  "out_of_sample": "2023-06-01 → 2023-12-31"},
        "out_of_sample": {
            "win_rate_pct": win_rate, "profit_factor": profit_factor,
            "sharpe_ratio": 1.35, "total_return_pct": 18.2,
            "max_drawdown_pct": -9.1, "total_trades": 44,
        },
        "bootstrap_out_of_sample": {"trade_level": {"prob_profitable": 0.82}},
        "verdict": {"validated": validated},
    }


def _baseline(**kwargs) -> dict:
    return build_baseline(
        _validation_result(**kwargs),
        tickers=["AAPL", "MSFT", "NVDA"], start="2022-01-01", end="2023-12-31",
        initial_capital=10_000, seed=42, now=NOW,
    )


def _card(n_trades=30, win_rate=45.0, profit_factor=1.6) -> dict:
    return {"n_trades": n_trades, "win_rate_pct": win_rate, "profit_factor": profit_factor}


@pytest.fixture(autouse=True)
def _baseline_config(monkeypatch):
    monkeypatch.setattr(cfg, "PAPER_SCORECARD_MIN_TRADES", 10)
    monkeypatch.setattr(cfg, "PAPER_SCORECARD_BASELINE_TOLERANCE_PCT", 30.0)
    monkeypatch.setattr(cfg, "PAPER_SCORECARD_BASELINE_MAX_AGE_DAYS", 180.0)
    monkeypatch.setattr(cfg, "PAPER_SCORECARD_BASELINE_GATE_ENABLED", True)
    # These tests are about the tolerance comparison, so they assume a baseline
    # built the only way the gate accepts — from a composite run. The
    # signal-source guard itself is exercised separately at the end of the file.
    monkeypatch.setattr(cfg, "BACKTEST_SIGNAL_SOURCE", "composite")


# ── build / persist ───────────────────────────────────────────────────────────

def test_build_baseline_captures_oos_metrics_and_provenance():
    b = _baseline()
    assert b["validated"] is True
    assert b["metrics"]["win_rate_pct"] == 50.0
    assert b["metrics"]["profit_factor"] == 2.0
    assert b["metrics"]["total_trades"] == 44
    assert b["provenance"]["n_tickers"] == 3
    assert b["provenance"]["out_of_sample"] == "2023-06-01 → 2023-12-31"
    assert b["provenance"]["seed"] == 42
    assert b["generated_at"] == NOW.isoformat()


def test_build_baseline_records_the_not_validated_verdict():
    assert _baseline(validated=False)["validated"] is False


def test_build_baseline_tolerates_a_junk_result():
    b = build_baseline({}, now=NOW)
    assert b["validated"] is False
    assert b["metrics"]["win_rate_pct"] is None
    assert b["metrics"]["total_trades"] == 0


def test_save_and_load_round_trip(tmp_path):
    path = str(tmp_path / "baseline.json")
    saved = save_baseline(
        _validation_result(), path=path, tickers=["AAPL"], start="2022-01-01",
        end="2023-12-31", initial_capital=10_000, seed=42, now=NOW,
    )
    on_disk = json.loads((tmp_path / "baseline.json").read_text())
    assert on_disk == saved
    assert load_baseline(path) == saved


def test_load_missing_baseline_is_none(tmp_path):
    assert load_baseline(str(tmp_path / "nope.json")) is None


def test_load_malformed_baseline_is_none_not_raise(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert load_baseline(str(bad)) is None

    no_metrics = tmp_path / "no_metrics.json"
    no_metrics.write_text(json.dumps({"validated": True}))
    assert load_baseline(str(no_metrics)) is None


def test_save_creates_missing_parent_directory(tmp_path):
    path = str(tmp_path / "nested" / "dir" / "baseline.json")
    save_baseline(_validation_result(), path=path, now=NOW)
    assert load_baseline(path) is not None


# ── tolerance comparison ──────────────────────────────────────────────────────

def test_paper_within_tolerance_passes():
    # 30% tolerance → floors are win rate 35.0 and profit factor 1.4.
    cmp_ = compare_paper_to_baseline(_card(win_rate=45.0, profit_factor=1.6), _baseline(), now=NOW)
    assert cmp_["all_pass"] is True
    assert cmp_["checks"]["win_rate_pct"]["floor"] == 35.0
    assert cmp_["checks"]["profit_factor"]["floor"] == 1.4
    assert cmp_["compared_metrics"] == ["win_rate_pct", "profit_factor"]


def test_paper_exactly_at_the_floor_passes():
    cmp_ = compare_paper_to_baseline(_card(win_rate=35.0, profit_factor=1.4), _baseline(), now=NOW)
    assert cmp_["all_pass"] is True


def test_paper_below_tolerance_fails_and_names_the_metric():
    cmp_ = compare_paper_to_baseline(_card(win_rate=30.0, profit_factor=1.9), _baseline(), now=NOW)
    assert cmp_["all_pass"] is False
    assert cmp_["checks"]["win_rate_pct"]["pass"] is False
    assert cmp_["checks"]["profit_factor"]["pass"] is True
    assert "win_rate_pct" in cmp_["reason"]


def test_comparison_is_one_sided_outperformance_never_blocks():
    cmp_ = compare_paper_to_baseline(_card(win_rate=95.0, profit_factor=9.0), _baseline(), now=NOW)
    assert cmp_["all_pass"] is True
    # ...but a large overshoot is surfaced as a possible data/accounting bug.
    assert cmp_["paper_exceeds_baseline"]
    assert "does not block promotion" in cmp_["divergence_note"]


def test_modest_outperformance_raises_no_divergence_note():
    cmp_ = compare_paper_to_baseline(_card(win_rate=55.0, profit_factor=2.2), _baseline(), now=NOW)
    assert cmp_["all_pass"] is True
    assert "divergence_note" not in cmp_


def test_zero_tolerance_demands_matching_the_baseline():
    b = _baseline()
    assert compare_paper_to_baseline(
        _card(win_rate=49.9, profit_factor=2.0), b, tolerance_pct=0, now=NOW
    )["all_pass"] is False
    assert compare_paper_to_baseline(
        _card(win_rate=50.0, profit_factor=2.0), b, tolerance_pct=0, now=NOW
    )["all_pass"] is True


def test_absurd_tolerance_is_clamped_not_trusted():
    b = _baseline()
    # A negative tolerance would otherwise raise the floor ABOVE the baseline;
    # >100 would drive the floor negative and pass anything. Both clamp.
    assert compare_paper_to_baseline(
        _card(win_rate=50.0, profit_factor=2.0), b, tolerance_pct=-50, now=NOW
    )["all_pass"] is True
    cmp_ = compare_paper_to_baseline(
        _card(win_rate=0.0, profit_factor=0.0), b, tolerance_pct=999, now=NOW
    )
    assert cmp_["tolerance_pct"] == 100.0
    assert cmp_["checks"]["win_rate_pct"]["floor"] == 0.0


# ── fail-closed refusals ──────────────────────────────────────────────────────

def test_missing_baseline_fails_closed_with_an_actionable_reason(tmp_path):
    cmp_ = compare_paper_to_baseline(_card(), path=str(tmp_path / "absent.json"), now=NOW)
    assert cmp_["all_pass"] is False
    assert cmp_["comparable"] is False
    assert "--save-baseline" in cmp_["reason"]


def test_unvalidated_baseline_is_refused_as_a_reference():
    cmp_ = compare_paper_to_baseline(_card(win_rate=99.0), _baseline(validated=False), now=NOW)
    assert cmp_["all_pass"] is False
    assert "NOT VALIDATED" in cmp_["reason"]


def test_stale_baseline_is_refused():
    cmp_ = compare_paper_to_baseline(
        _card(), _baseline(), now=NOW + timedelta(days=181)
    )
    assert cmp_["all_pass"] is False
    assert "days old" in cmp_["reason"]
    assert cmp_["age_days"] == 181.0


def test_baseline_just_inside_the_age_limit_is_accepted():
    cmp_ = compare_paper_to_baseline(_card(), _baseline(), now=NOW + timedelta(days=179))
    assert cmp_["all_pass"] is True


def test_zero_max_age_disables_the_staleness_check():
    cmp_ = compare_paper_to_baseline(
        _card(), _baseline(), max_age_days=0, now=NOW + timedelta(days=5_000)
    )
    assert cmp_["all_pass"] is True


def test_baseline_without_a_timestamp_fails_closed():
    b = _baseline()
    b["generated_at"] = "not-a-timestamp"
    cmp_ = compare_paper_to_baseline(_card(), b, now=NOW)
    assert cmp_["all_pass"] is False
    assert "age cannot be checked" in cmp_["reason"]


def test_naive_timestamp_is_read_as_utc():
    b = _baseline()
    b["generated_at"] = "2026-08-12T00:00:00"  # no tzinfo
    assert compare_paper_to_baseline(_card(), b, now=NOW)["all_pass"] is True


def test_too_few_paper_trades_fails_closed():
    cmp_ = compare_paper_to_baseline(_card(n_trades=9), _baseline(), now=NOW)
    assert cmp_["all_pass"] is False
    assert "9 closed paper trades" in cmp_["reason"]


def test_baseline_with_no_comparable_metric_fails_closed():
    b = _baseline()
    b["metrics"]["win_rate_pct"] = None
    b["metrics"]["profit_factor"] = None
    cmp_ = compare_paper_to_baseline(_card(), b, now=NOW)
    assert cmp_["all_pass"] is False
    assert "No metric could be compared" in cmp_["reason"]


def test_missing_paper_win_rate_fails_closed():
    card = _card()
    card["win_rate_pct"] = None
    cmp_ = compare_paper_to_baseline(card, _baseline(), now=NOW)
    assert cmp_["all_pass"] is False
    assert "missing from the paper scorecard" in cmp_["reason"]


# ── partial-metric handling ───────────────────────────────────────────────────

def test_baseline_without_profit_factor_still_compares_win_rate():
    b = _baseline(profit_factor=None)
    cmp_ = compare_paper_to_baseline(_card(win_rate=45.0), b, now=NOW)
    assert cmp_["all_pass"] is True
    assert cmp_["compared_metrics"] == ["win_rate_pct"]
    assert cmp_["checks"]["profit_factor"]["comparable"] is False


def test_lossless_paper_record_passes_the_profit_factor_check():
    # profit_factor is None on the paper side only when there are no losses yet —
    # strictly better than any finite baseline, mirroring the P0.4 floor check.
    cmp_ = compare_paper_to_baseline(_card(profit_factor=None), _baseline(), now=NOW)
    assert cmp_["all_pass"] is True
    assert cmp_["checks"]["profit_factor"]["pass"] is True
    assert cmp_["checks"]["profit_factor"]["note"] == "no losing paper trades yet"


# ── baseline_gate toggle ──────────────────────────────────────────────────────

def test_gate_disabled_is_a_no_op(monkeypatch, tmp_path):
    monkeypatch.setattr(cfg, "PAPER_SCORECARD_BASELINE_GATE_ENABLED", False)
    ok, cmp_ = baseline_gate(_card(win_rate=0.0), path=str(tmp_path / "absent.json"))
    assert ok is True and cmp_ is None


def test_gate_enabled_without_a_baseline_blocks(monkeypatch, tmp_path):
    monkeypatch.setattr(cfg, "PAPER_SCORECARD_BASELINE_PATH", str(tmp_path / "absent.json"))
    ok, cmp_ = baseline_gate(_card())
    assert ok is False
    assert cmp_["all_pass"] is False


def test_gate_enabled_with_a_good_baseline_passes(monkeypatch, tmp_path):
    path = str(tmp_path / "baseline.json")
    save_baseline(_validation_result(), path=path, now=datetime.now(timezone.utc))
    monkeypatch.setattr(cfg, "PAPER_SCORECARD_BASELINE_PATH", path)
    ok, cmp_ = baseline_gate(_card(win_rate=45.0, profit_factor=1.6))
    assert ok is True and cmp_["all_pass"] is True


# ── live gate + RiskGuard integration ─────────────────────────────────────────

def _trades(pnls: list[float]) -> list[dict]:
    return [{"pnl": float(p), "closed_at": "2026-01-01T00:00:00", "ticker": "AAA"} for p in pnls]


@pytest.fixture
def tracker(tmp_path):
    return PositionTracker(store_path=str(tmp_path / "pos.json"))


@pytest.fixture
def _floors_pass(monkeypatch):
    """Floors deliberately permissive so only the baseline half can reject."""
    monkeypatch.setattr(cfg, "PAPER_SCORECARD_PERFORMANCE_GATE_ENABLED", True)
    monkeypatch.setattr(cfg, "PAPER_SCORECARD_PROFIT_FACTOR_FLOOR", 1.0)
    monkeypatch.setattr(cfg, "PAPER_SCORECARD_PROB_PROFITABLE_FLOOR", 0.5)
    monkeypatch.setattr(cfg, "PAPER_SCORECARD_REQUIRE_POSITIVE_EXPECTANCY", True)


def _strong_paper_record() -> list[float]:
    # 14 wins / 6 losses → win rate 70%, profit factor ~4.67.
    return [100.0] * 14 + [-50.0] * 6


def test_live_gate_blocks_when_paper_diverges_from_the_baseline(
    tracker, monkeypatch, tmp_path, _floors_pass
):
    # A paper record that clears the absolute floors but is far below a baseline
    # validated at a 95% win rate — exactly the case the floors alone let through.
    path = str(tmp_path / "baseline.json")
    save_baseline(
        _validation_result(win_rate=95.0, profit_factor=8.0),
        path=path, now=datetime.now(timezone.utc),
    )
    monkeypatch.setattr(cfg, "PAPER_SCORECARD_BASELINE_PATH", path)
    tracker._trades = _trades(_strong_paper_record())

    passes, card = meets_live_performance_gate(tracker, initial_capital=50_000)
    assert card["acceptance"]["all_pass"] is True       # floors alone would allow it
    assert passes is False                              # the baseline half rejects
    assert "paper-vs-validated-backtest" in card["gate_reason"]
    assert card["baseline_comparison"]["all_pass"] is False


def test_live_gate_allows_when_paper_tracks_the_baseline(
    tracker, monkeypatch, tmp_path, _floors_pass
):
    path = str(tmp_path / "baseline.json")
    save_baseline(_validation_result(win_rate=60.0, profit_factor=3.0),
                  path=path, now=datetime.now(timezone.utc))
    monkeypatch.setattr(cfg, "PAPER_SCORECARD_BASELINE_PATH", path)
    tracker._trades = _trades(_strong_paper_record())

    passes, card = meets_live_performance_gate(tracker, initial_capital=50_000)
    assert passes is True
    assert card["gate_reason"] == ""
    assert card["baseline_comparison"]["all_pass"] is True


def test_live_gate_needs_no_baseline_when_only_floors_are_enabled(
    tracker, monkeypatch, _floors_pass
):
    monkeypatch.setattr(cfg, "PAPER_SCORECARD_BASELINE_GATE_ENABLED", False)
    tracker._trades = _trades(_strong_paper_record())
    passes, card = meets_live_performance_gate(tracker, initial_capital=50_000)
    assert passes is True
    assert "baseline_comparison" not in card


def test_live_gate_reports_both_failures_together(tracker, monkeypatch, tmp_path):
    monkeypatch.setattr(cfg, "PAPER_SCORECARD_PERFORMANCE_GATE_ENABLED", True)
    monkeypatch.setattr(cfg, "PAPER_SCORECARD_PROFIT_FACTOR_FLOOR", 1.2)
    monkeypatch.setattr(cfg, "PAPER_SCORECARD_BASELINE_PATH", str(tmp_path / "absent.json"))
    tracker._trades = _trades([50.0] * 6 + [-100.0] * 14)  # losing record

    passes, card = meets_live_performance_gate(tracker, initial_capital=50_000)
    assert passes is False
    assert "below the configured floors" in card["gate_reason"]
    assert "paper-vs-validated-backtest" in card["gate_reason"]


def _portfolio() -> dict:
    return {"total_equity": 50_000.0, "buying_power": 45_000.0, "position_count": 0}


def _entry_plan() -> dict:
    return {
        "ticker": "MSFT", "signal": "STRONG_BUY",
        "entry": {"type": "LIMIT", "price": 189.40}, "stop_loss": {"price": 186.35},
        "targets": {"T1": {"price": 191.69}, "T2": {"price": 196.07}, "T3": {"price": 203.84}},
        "position": {"shares": 10, "position_value": 1894.0, "risk_dollars": 30.5},
        "reasoning": ["Golden Cross"],
    }


def test_riskguard_live_blocks_on_baseline_divergence(
    tracker, monkeypatch, tmp_path, _floors_pass
):
    import src.alpaca.executor as _ex
    monkeypatch.setattr(_ex, "_lookup_days_to_earnings", lambda _t: None)
    monkeypatch.setattr(cfg, "SAFETY", {})          # isolate the graduation gate
    monkeypatch.setattr(cfg, "ALPACA_PAPER", False)  # live path
    path = str(tmp_path / "baseline.json")
    save_baseline(_validation_result(win_rate=95.0, profit_factor=8.0),
                  path=path, now=datetime.now(timezone.utc))
    monkeypatch.setattr(cfg, "PAPER_SCORECARD_BASELINE_PATH", path)
    tracker._trades = _trades(_strong_paper_record())

    ok, reason = RiskGuard().check(_entry_plan(), _portfolio(), tracker)
    assert ok is False
    assert "validated" in reason.lower()


def test_riskguard_paper_path_ignores_the_baseline_gate(
    tracker, monkeypatch, tmp_path, _floors_pass
):
    """The graduation gate guards REAL money only — paper trading must not need
    a baseline to keep running (otherwise you could never build the paper record
    the gate is asking for)."""
    import src.alpaca.executor as _ex
    monkeypatch.setattr(_ex, "_lookup_days_to_earnings", lambda _t: None)
    monkeypatch.setattr(cfg, "SAFETY", {})
    monkeypatch.setattr(cfg, "ALPACA_PAPER", True)   # paper path
    monkeypatch.setattr(cfg, "PAPER_SCORECARD_BASELINE_PATH", str(tmp_path / "absent.json"))
    tracker._trades = []

    ok, reason = RiskGuard().check(_entry_plan(), _portfolio(), tracker)
    assert ok is True, reason


# ── signal-source guard (PR #57 review, audit B1) ─────────────────────────────

def test_baseline_records_the_signal_source(monkeypatch):
    monkeypatch.setattr(cfg, "BACKTEST_SIGNAL_SOURCE", "composite")
    assert _baseline()["provenance"]["signal_source"] == "composite"
    monkeypatch.setattr(cfg, "BACKTEST_SIGNAL_SOURCE", "proxy")
    assert _baseline()["provenance"]["signal_source"] == "proxy"


def test_proxy_baseline_is_refused_by_the_gate(monkeypatch):
    """A proxy baseline measures the SMA/RSI rule; paper measures the composite.

    Comparing them would emit a confident verdict from two unrelated strategies,
    so the gate must refuse rather than compare.
    """
    monkeypatch.setattr(cfg, "BACKTEST_SIGNAL_SOURCE", "proxy")
    cmp_ = compare_paper_to_baseline(_card(), _baseline(), now=NOW)
    assert cmp_["all_pass"] is False
    assert "signal_source=proxy" in cmp_["reason"]
    assert "composite" in cmp_["reason"]


def test_composite_baseline_is_accepted(monkeypatch):
    monkeypatch.setattr(cfg, "BACKTEST_SIGNAL_SOURCE", "composite")
    assert compare_paper_to_baseline(_card(), _baseline(), now=NOW)["all_pass"] is True


def test_baseline_without_a_recorded_source_is_refused(monkeypatch):
    """Baselines saved before the source was recorded must fail closed."""
    monkeypatch.setattr(cfg, "BACKTEST_SIGNAL_SOURCE", "composite")
    b = _baseline()
    b["provenance"].pop("signal_source")
    cmp_ = compare_paper_to_baseline(_card(), b, now=NOW)
    assert cmp_["all_pass"] is False
    assert "unknown" in cmp_["reason"]


def test_source_guard_applies_on_the_live_gate(tracker, monkeypatch, tmp_path, _floors_pass):
    """End-to-end: a proxy baseline must not let real-money entries through."""
    monkeypatch.setattr(cfg, "BACKTEST_SIGNAL_SOURCE", "proxy")
    path = str(tmp_path / "baseline.json")
    save_baseline(_validation_result(win_rate=60.0, profit_factor=3.0),
                  path=path, now=datetime.now(timezone.utc))
    monkeypatch.setattr(cfg, "PAPER_SCORECARD_BASELINE_PATH", path)
    tracker._trades = _trades(_strong_paper_record())

    passes, card = meets_live_performance_gate(tracker, initial_capital=50_000)
    assert card["acceptance"]["all_pass"] is True   # floors alone would allow it
    assert passes is False
    assert "signal_source=proxy" in card["gate_reason"]
