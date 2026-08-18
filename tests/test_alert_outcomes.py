"""Tests for the alert outcome ledger (src/observability/alert_outcomes.py).

Offline: the tape is an injected 1m frame. What is pinned is the honesty
contract — the baseline is the first bar at/after the alert (not a stale list
price), a short alert's favourable move scores positive, unmeasurable alerts
are counted rather than dropped, and the resolver's fetch budget is respected.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import pytest

from src.observability.alert_outcomes import AlertOutcomeLedger
from src.utils import config as cfg

_T0 = datetime(2026, 8, 18, 14, 0)          # naive UTC, matching frame indexes


def _frame(start: datetime, closes: list[float], step_min: int = 1) -> pd.DataFrame:
    idx = pd.DatetimeIndex([start + timedelta(minutes=i * step_min)
                            for i in range(len(closes))])
    return pd.DataFrame({"Open": closes, "High": closes, "Low": closes,
                         "Close": closes, "Volume": [1000] * len(closes)}, index=idx)


@pytest.fixture(autouse=True)
def _cfg(monkeypatch, tmp_path):
    monkeypatch.setattr(cfg, "ALERT_OUTCOMES_ENABLED", True)
    monkeypatch.setattr(cfg, "ALERT_OUTCOMES_HORIZONS_MIN", [30])
    monkeypatch.setattr(cfg, "ALERT_OUTCOMES_MAX_FETCHES_PER_CYCLE", 3)
    monkeypatch.setattr(cfg, "ALERT_OUTCOMES_MAX_RECORDS", 400)
    monkeypatch.setattr(cfg, "ALERT_OUTCOMES_PATH", str(tmp_path / "ao.json"))


def _ledger(tmp_path=None):
    return AlertOutcomeLedger()


def _fired(ticker="AAA", direction="long", **extra):
    return [{"ticker": ticker, "direction": direction, "score": 80.0, **extra}]


class TestRecord:
    def test_records_a_pending_outcome_per_horizon(self):
        led = _ledger()
        assert led.record(_fired(), source="movers", now=_T0) == 1
        rec = led.recent(1)[0]
        assert rec["ticker"] == "AAA"
        assert rec["outcomes"] == {"30": None}
        assert rec["baseline_price"] is None

    def test_disabled_records_nothing(self, monkeypatch):
        monkeypatch.setattr(cfg, "ALERT_OUTCOMES_ENABLED", False)
        assert _ledger().record(_fired(), source="movers", now=_T0) == 0

    def test_same_alert_within_a_minute_is_one_event(self):
        led = _ledger()
        led.record(_fired(), source="movers", now=_T0)
        assert led.record(_fired(), source="movers",
                          now=_T0 + timedelta(seconds=30)) == 0
        # A different source is a different alert (catalyst + movers can both
        # legitimately fire on the same name).
        assert led.record(_fired(tier="strong"), source="catalyst", now=_T0) == 1

    def test_catalyst_tier_is_kept(self):
        led = _ledger()
        led.record(_fired(tier="dilution", direction="short"),
                   source="catalyst", now=_T0)
        assert led.recent(1)[0]["tier"] == "dilution"


class TestResolve:
    def test_measures_from_the_first_bar_after_the_alert(self):
        # Bars: 100 at T0 (baseline), rising to 110 by T0+30 — a +10% move.
        led = _ledger()
        led.record(_fired(), source="movers", now=_T0)
        frame = _frame(_T0, [100.0 + i / 3.0 for i in range(35)])
        settled = led.resolve(now=_T0 + timedelta(minutes=31),
                              fetch=lambda t: frame)
        assert settled == 1
        rec = led.recent(1)[0]
        assert rec["baseline_price"] == 100.0
        out = rec["outcomes"]["30"]
        assert out["in_direction_pct"] == pytest.approx(10.0, abs=0.2)

    def test_short_alert_scores_a_fall_as_positive(self):
        # -10% price move after a `short` alert is the alert being RIGHT.
        led = _ledger()
        led.record(_fired(direction="short"), source="movers", now=_T0)
        frame = _frame(_T0, [100.0 - i / 3.0 for i in range(35)])
        led.resolve(now=_T0 + timedelta(minutes=31), fetch=lambda t: frame)
        out = led.recent(1)[0]["outcomes"]["30"]
        assert out["move_pct"] < 0
        assert out["in_direction_pct"] > 0

    def test_not_due_yet_is_left_pending(self):
        led = _ledger()
        led.record(_fired(), source="movers", now=_T0)
        frame = _frame(_T0, [100.0] * 10)
        assert led.resolve(now=_T0 + timedelta(minutes=5),
                           fetch=lambda t: frame) == 0
        assert led.recent(1)[0]["outcomes"]["30"] is None

    def test_no_bar_near_the_alert_is_unmeasurable_not_zero(self):
        # Tape exists but only starts 20 minutes after the alert (halt, thin
        # name): the alert-instant price is unknowable, and pretending the
        # first available bar was "the alert price" would invent a result.
        led = _ledger()
        led.record(_fired(), source="movers", now=_T0)
        frame = _frame(_T0 + timedelta(minutes=20), [100.0] * 20)
        led.resolve(now=_T0 + timedelta(minutes=31), fetch=lambda t: frame)
        assert led.recent(1)[0]["outcomes"]["30"] == {"unresolved": "no_baseline_bar"}

    def test_expired_horizons_are_swept_without_a_fetch(self):
        led = _ledger()
        led.record(_fired(), source="movers", now=_T0)
        calls = []

        def fetch(t):
            calls.append(t)
            return _frame(_T0, [100.0] * 5)

        led.resolve(now=_T0 + timedelta(hours=12), fetch=fetch)
        assert calls == []      # past hard expiry: retired for free
        assert led.recent(1)[0]["outcomes"]["30"] == {"unresolved": "expired_no_data"}

    def test_fetch_budget_is_respected(self, monkeypatch):
        monkeypatch.setattr(cfg, "ALERT_OUTCOMES_MAX_FETCHES_PER_CYCLE", 2)
        led = _ledger()
        for i, tk in enumerate(["AAA", "BBB", "CCC", "DDD"]):
            led.record(_fired(ticker=tk), source="movers", now=_T0)
        calls = []
        frame = _frame(_T0, [100.0] * 35)
        led.resolve(now=_T0 + timedelta(minutes=31),
                    fetch=lambda t: (calls.append(t), frame)[1])
        assert len(calls) == 2  # the rest wait for the next cycle

    def test_failed_fetch_keeps_the_alert_pending(self):
        led = _ledger()
        led.record(_fired(), source="movers", now=_T0)

        def boom(t):
            raise RuntimeError("provider down")

        assert led.resolve(now=_T0 + timedelta(minutes=31), fetch=boom) == 0
        assert led.recent(1)[0]["outcomes"]["30"] is None


class TestSummary:
    def _resolved_ledger(self):
        led = _ledger()
        led.record(_fired("UP"), source="movers", now=_T0)
        led.record(_fired("DOWN"), source="movers", now=_T0)
        frames = {
            "UP": _frame(_T0, [100.0 + i for i in range(35)]),      # winner
            "DOWN": _frame(_T0, [100.0 - i / 2 for i in range(35)]),  # loser
        }
        led.resolve(now=_T0 + timedelta(minutes=31), fetch=lambda t: frames[t])
        return led

    def test_hit_rate_and_averages(self):
        s = self._resolved_ledger().summary()
        h = s["sources"]["movers"]["by_horizon"]["30"]
        assert h["n"] == 2
        assert h["hit_rate"] == 0.5
        assert h["pending"] == 0

    def test_unmeasured_alerts_stay_in_the_summary(self):
        led = self._resolved_ledger()
        led.record(_fired("GHOST"), source="movers", now=_T0)
        led.resolve(now=_T0 + timedelta(hours=12), fetch=lambda t: None)
        h = led.summary()["sources"]["movers"]["by_horizon"]["30"]
        assert h["n"] == 2
        assert h["unresolved"] == 1   # visible, not silently dropped

    def test_sources_are_separated(self):
        led = self._resolved_ledger()
        led.record(_fired("NEWS", tier="strong"), source="catalyst", now=_T0)
        s = led.summary()
        assert set(s["sources"]) == {"movers", "catalyst"}
        assert s["sources"]["catalyst"]["by_horizon"]["30"]["pending"] == 1

    def test_persists_across_instances(self):
        self._resolved_ledger()
        fresh = AlertOutcomeLedger().load()
        assert fresh.summary()["sources"]["movers"]["by_horizon"]["30"]["n"] == 2

    def test_ledger_is_bounded(self, monkeypatch):
        monkeypatch.setattr(cfg, "ALERT_OUTCOMES_MAX_RECORDS", 5)
        led = _ledger()
        for i in range(12):
            led.record(_fired(ticker=f"T{i}"), source="movers",
                       now=_T0 + timedelta(minutes=2 * i))
        assert len(AlertOutcomeLedger().load().recent(100)) == 5
