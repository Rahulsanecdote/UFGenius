"""Tests for catalyst-triggered alerts (src/catalysts/catalyst_alerts.py).

Offline: the wire fetch is injected, so no keys and no network. These cover the
behaviour that matters — the tier gate, symbol routing, the universe filter,
halt suppression, dedup, the spam cap, and the fail-soft contract.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.catalysts.catalyst_alerts import CatalystAlerter, format_catalyst_alert
from src.catalysts.news_feed import NewsHeadline
from src.utils import config as cfg

_NOW = datetime(2026, 8, 17, 14, 30, tzinfo=timezone.utc)


def _news(title, symbols, published=None, source="Wire"):
    return NewsHeadline(title=title, source=source, provider="alpaca",
                        published=published or _NOW, symbols=list(symbols))


@pytest.fixture(autouse=True)
def _enabled(monkeypatch):
    monkeypatch.setattr(cfg, "CATALYST_ALERTS_ENABLED", True)
    monkeypatch.setattr(cfg, "CATALYST_ALERTS_UNIVERSE", "all")
    monkeypatch.setattr(cfg, "CATALYST_ALERTS_TIERS", ["strong"])
    monkeypatch.setattr(cfg, "CATALYST_ALERTS_LOOKBACK_SEC", 300)
    monkeypatch.setattr(cfg, "CATALYST_ALERTS_DEDUP_TTL_SEC", 3600)
    monkeypatch.setattr(cfg, "CATALYST_ALERTS_MAX_PER_RUN", 5)
    monkeypatch.setattr(cfg, "CATALYST_ALERTS_SUPPRESS_HALTED", True)
    monkeypatch.setattr("src.data.halts.active_halts", lambda *a, **k: {})


def _poll(alerter, items, **kw):
    return alerter.poll(now=_NOW, send=False, fetch=lambda *a, **k: items, **kw)


class TestTierGate:
    def test_strong_catalyst_fires(self):
        out = _poll(CatalystAlerter(), [_news("FDA approves ACME's lead drug", ["ACME"])])
        assert [f["ticker"] for f in out] == ["ACME"]
        assert out[0]["tier"] == "strong"
        assert out[0]["direction"] == "long"

    def test_non_qualifying_tiers_are_ignored(self):
        items = [
            _news("Why is ACME stock soaring today?", ["ACME"]),   # weak
            _news("ACME hosts investor day", ["BCME"]),            # moderate
            _news("ACME opens a new office", ["CCME"]),            # none
        ]
        assert _poll(CatalystAlerter(), items) == []

    def test_configured_tiers_are_honoured(self, monkeypatch):
        monkeypatch.setattr(cfg, "CATALYST_ALERTS_TIERS", ["strong", "dilution"])
        out = _poll(CatalystAlerter(), [
            _news("ACME prices public offering of common stock", ["ACME"]),
        ])
        assert out[0]["tier"] == "dilution"
        # Dilution leans short — it is an overhang, not an opportunity.
        assert out[0]["direction"] == "short"

    def test_each_headline_is_classified_alone(self):
        # A strong story in the batch must not promote an unrelated weak one.
        items = [
            _news("FDA approves ACME device", ["ACME"]),
            _news("Why is ZZZZ stock soaring today?", ["ZZZZ"]),
        ]
        assert [f["ticker"] for f in _poll(CatalystAlerter(), items)] == ["ACME"]


class TestSymbolRouting:
    def test_a_story_alerts_every_symbol_it_names(self):
        out = _poll(CatalystAlerter(),
                    [_news("BigCo to acquire ACME for $2B", ["ACME", "BIGCO"])])
        assert {f["ticker"] for f in out} == {"ACME", "BIGCO"}

    def test_story_without_symbols_alerts_nothing(self):
        assert _poll(CatalystAlerter(), [_news("FDA approves something", [])]) == []


class TestUniverseFilter:
    def test_watchlist_mode_filters_to_the_watchlist(self, monkeypatch):
        monkeypatch.setattr(cfg, "CATALYST_ALERTS_UNIVERSE", "watchlist")
        monkeypatch.setattr("src.data.universe.get_custom_watchlist",
                            lambda: ["ACME"])
        out = _poll(CatalystAlerter(), [
            _news("FDA approves ACME drug", ["ACME"]),
            _news("FDA approves OTHER drug", ["OTHER"]),
        ])
        assert [f["ticker"] for f in out] == ["ACME"]

    def test_empty_watchlist_is_a_no_op(self, monkeypatch):
        monkeypatch.setattr(cfg, "CATALYST_ALERTS_UNIVERSE", "watchlist")
        monkeypatch.setattr("src.data.universe.get_custom_watchlist", lambda: [])
        assert _poll(CatalystAlerter(), [_news("FDA approves ACME", ["ACME"])]) == []

    def test_all_mode_takes_the_firehose(self):
        # No watchlist involvement: an unknown ticker still alerts, which is the
        # whole point of early discovery.
        out = _poll(CatalystAlerter(), [_news("FDA approves NEWCO drug", ["NEWCO"])])
        assert [f["ticker"] for f in out] == ["NEWCO"]


class TestSuppressionAndDedup:
    def test_halted_symbol_is_suppressed(self, monkeypatch):
        from src.data.halts import HaltRecord
        monkeypatch.setattr("src.data.halts.active_halts",
                            lambda *a, **k: {"ACME": HaltRecord("ACME", reason_code="LUDP")})
        assert _poll(CatalystAlerter(), [_news("FDA approves ACME drug", ["ACME"])]) == []

    def test_suppression_can_be_disabled(self, monkeypatch):
        from src.data.halts import HaltRecord
        monkeypatch.setattr(cfg, "CATALYST_ALERTS_SUPPRESS_HALTED", False)
        monkeypatch.setattr("src.data.halts.active_halts",
                            lambda *a, **k: {"ACME": HaltRecord("ACME")})
        assert len(_poll(CatalystAlerter(), [_news("FDA approves ACME drug", ["ACME"])])) == 1

    def test_same_story_is_not_re_alerted(self):
        alerter = CatalystAlerter()
        items = [_news("FDA approves ACME drug", ["ACME"])]
        assert len(_poll(alerter, items)) == 1
        assert _poll(alerter, items) == []          # same instance, same story

    def test_a_different_story_for_the_same_symbol_still_fires(self):
        alerter = CatalystAlerter()
        assert len(_poll(alerter, [_news("FDA approves ACME drug", ["ACME"])])) == 1
        assert len(_poll(alerter, [_news("ACME wins $50M contract", ["ACME"])])) == 1

    def test_max_per_run_caps_the_batch(self, monkeypatch):
        monkeypatch.setattr(cfg, "CATALYST_ALERTS_MAX_PER_RUN", 2)
        items = [_news(f"FDA approves DRUG{i} therapy", [f"SYM{i}"]) for i in range(5)]
        assert len(_poll(CatalystAlerter(), items)) == 2


class TestFailSoft:
    def test_disabled_is_a_no_op(self, monkeypatch):
        monkeypatch.setattr(cfg, "CATALYST_ALERTS_ENABLED", False)
        assert _poll(CatalystAlerter(), [_news("FDA approves ACME", ["ACME"])]) == []

    def test_fetch_failure_never_raises(self):
        def boom(*a, **k):
            raise RuntimeError("wire down")
        assert CatalystAlerter().poll(now=_NOW, send=False, fetch=boom) == []

    def test_halt_lookup_failure_does_not_block_alerts(self, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("halt feed down")
        monkeypatch.setattr("src.data.halts.active_halts", boom)
        # Fail-open: a halt-feed outage must not mute the wire.
        assert len(_poll(CatalystAlerter(), [_news("FDA approves ACME drug", ["ACME"])])) == 1


class TestMessage:
    def test_message_carries_the_headline_as_a_receipt(self):
        msg = format_catalyst_alert(
            "ACME", "strong", _news("FDA approves ACME's lead drug", ["ACME"]))
        assert "ACME" in msg
        assert "FDA approves ACME's lead drug" in msg
        # It must not read as a recommendation, or as a prediction.
        assert "NOT a trade instruction" in msg
        assert "not a prediction" in msg

    def test_lookback_is_passed_to_the_fetch(self, monkeypatch):
        monkeypatch.setattr(cfg, "CATALYST_ALERTS_LOOKBACK_SEC", 120)
        seen = {}

        def _fetch(symbols, *, since):
            seen["since"] = since
            return []

        CatalystAlerter().poll(now=_NOW, send=False, fetch=_fetch)
        assert seen["since"] == _NOW - timedelta(seconds=120)


class TestDedupPruning:
    """The dedup map must not grow for the worker's lifetime.

    The TTL only ever changed the comparison, so in `universe: all` the
    firehose supplied an unbounded stream of new (symbol, story) keys
    (CodeRabbit).
    """

    def test_expired_entries_are_pruned(self, monkeypatch):
        monkeypatch.setattr(cfg, "CATALYST_ALERTS_DEDUP_TTL_SEC", 600)
        alerter = CatalystAlerter()
        assert len(_poll(alerter, [_news("FDA approves ACME drug", ["ACME"])])) == 1
        assert len(alerter._recent) == 1

        # A later poll, past the TTL, with an unrelated story: the stale key goes.
        later = _NOW + timedelta(seconds=1200)
        alerter.poll(now=later, send=False,
                     fetch=lambda *a, **k: [_news("FDA approves BCME drug", ["BCME"],
                                                  published=later)])
        assert list(alerter._recent) == [("BCME", "FDA approves BCME drug")]

    def test_live_entries_survive_the_prune(self, monkeypatch):
        monkeypatch.setattr(cfg, "CATALYST_ALERTS_DEDUP_TTL_SEC", 3600)
        alerter = CatalystAlerter()
        items = [_news("FDA approves ACME drug", ["ACME"])]
        assert len(_poll(alerter, items)) == 1
        # Still inside the TTL → key retained, so the story stays deduped.
        assert alerter.poll(now=_NOW + timedelta(seconds=60), send=False,
                            fetch=lambda *a, **k: items) == []
        assert len(alerter._recent) == 1
