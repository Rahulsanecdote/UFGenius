"""Shared pytest fixtures."""

from __future__ import annotations

import pytest

import src.utils.config as cfg


# Every credential config.py reads. A unit test must never reach a real
# provider, broker or Telegram chat because the developer's shell happened to
# have keys in it — and "happened to" is exactly how it was discovered:
# tests/test_movers.py::test_no_key_returns_empty blanked FMP_KEY alone, from
# when discovery was FMP-only. Once the chain became [alpaca, polygon, fmp], a
# shell with Alpaca keys made that "no key" test call the live Alpaca screener
# and fail, while CI — which has no keys — kept passing it. The test was
# asserting its premise, not testing it.
#
# Blanking here reproduces CI's conditions for every unit test. Tests that need
# a key still set one (monkeypatch / patch.object run after this fixture), and
# `integration`-marked tests are exempt, since reaching the network is their
# job. Covering it once here rather than per test is the same call the logger
# makes about redaction: a rule that must be remembered N times is missed on
# the N+1th.
_CREDENTIALS = (
    "ALPACA_API_KEY", "ALPACA_SECRET_KEY", "POLYGON_KEY", "FMP_KEY",
    "ALPHA_VANTAGE_KEY", "FINNHUB_KEY", "NEWSAPI_KEY", "FRED_API_KEY",
    "REDDIT_CLIENT_SECRET", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
    "EMAIL_PASSWORD", "EXPLAIN_API_KEY", "DASHBOARD_API_KEY", "DASHBOARD_API_KEYS",
)


@pytest.fixture(autouse=True)
def _no_ambient_credentials(request, monkeypatch):
    """Blank every credential for unit tests, whatever the shell exports."""
    if request.node.get_closest_marker("integration"):
        return
    for name in _CREDENTIALS:
        if hasattr(cfg, name):
            monkeypatch.setattr(cfg, name, "")


@pytest.fixture(autouse=True)
def _isolate_circuit_breaker_state(tmp_path, monkeypatch):
    """Point the P0.3 circuit-breaker state file at a per-test temp path.

    ``execute_trade_plan`` persists broker-error and halt state to
    ``config.CIRCUIT_STATE_PATH``. Without this, tests that drive the execution
    path would write to the real ``data/circuit_breaker.json`` and could trip the
    broker breaker for later tests (cross-test contamination). Each test gets a
    fresh, isolated state file. Individual tests may still override this path.
    """
    monkeypatch.setattr(cfg, "CIRCUIT_STATE_PATH", str(tmp_path / "circuit_breaker.json"))


@pytest.fixture(autouse=True)
def _isolate_execution_quality_ledger(tmp_path, monkeypatch):
    """Point the P2.1 execution-quality ledger at a per-test temp path.

    The executor records every fill to ``config.EXEC_QUALITY_LEDGER_PATH`` via a
    lazy singleton; isolate the path AND reset the singleton so execution tests
    never write to the real ``data/execution_quality.json`` or leak across tests.
    """
    import src.alpaca.execution_quality as _eq
    monkeypatch.setattr(cfg, "EXEC_QUALITY_LEDGER_PATH", str(tmp_path / "execution_quality.json"))
    monkeypatch.setattr(_eq, "_default", None)


@pytest.fixture(autouse=True)
def _isolate_metrics_ledger(tmp_path, monkeypatch):
    """Point the P2.3 scan-metrics ledger at a per-test temp path.

    ``run_daily_scan`` records each scan to ``config.METRICS_LEDGER_PATH`` via a
    lazy singleton; isolate the path AND reset the singleton so scan/metrics tests
    never write to the real ``data/metrics.json`` or leak across tests.
    """
    import src.observability.metrics as _metrics
    monkeypatch.setattr(cfg, "METRICS_LEDGER_PATH", str(tmp_path / "metrics.json"))
    monkeypatch.setattr(_metrics, "_default", None)


@pytest.fixture(autouse=True)
def _isolate_explain_ledger(tmp_path, monkeypatch):
    """Point the P3.1 explain daily-call ledger at a per-test temp path.

    The explainability layer writes a per-day call counter to
    ``config.EXPLAIN_CALL_LEDGER_PATH`` for its cost cap; isolate it so tests
    never touch the real ``data/explain_calls.json``.
    """
    monkeypatch.setattr(cfg, "EXPLAIN_CALL_LEDGER_PATH", str(tmp_path / "explain_calls.json"))


@pytest.fixture(autouse=True)
def _isolate_peak_equity_store(tmp_path, monkeypatch):
    """Point the Phase 4 equity high-water mark at a per-test temp path.

    The portfolio drawdown halt persists a peak-equity value to
    ``config.PORTFOLIO_PEAK_EQUITY_PATH``; isolate it so gate/dashboard tests
    never read or write the real ``data/portfolio_peak_equity.json``.
    """
    monkeypatch.setattr(
        cfg, "PORTFOLIO_PEAK_EQUITY_PATH", str(tmp_path / "portfolio_peak_equity.json")
    )


@pytest.fixture(autouse=True)
def _disable_halt_feed(monkeypatch):
    """Keep the halt lookup off the network for the suite by default.

    ``movers.halts.enabled`` ships ON, so without this every discovery test
    would pay a real connect-timeout to the Nasdaq feed. Tests that exercise
    halt behaviour re-enable it and patch the HTTP boundary (tests/test_halts.py).
    """
    monkeypatch.setattr(cfg, "MOVERS_HALTS_ENABLED", False)
    monkeypatch.setattr(cfg, "MOVERS_HALT_SKIP_INVALIDATION", False)


@pytest.fixture(autouse=True)
def _sandbox_alert_outcomes(monkeypatch, tmp_path):
    """Keep the alert-outcome ledger out of the repo's data/ for the suite.

    ``observability.alert_outcomes`` ships ON, so a worker test that doesn't
    inject a ledger would otherwise write data/alert_outcomes.json in the
    working tree. Tests that exercise the ledger re-enable it against their own
    tmp path (tests/test_alert_outcomes.py — its fixture runs after this one).
    """
    monkeypatch.setattr(cfg, "ALERT_OUTCOMES_ENABLED", False)
    monkeypatch.setattr(cfg, "ALERT_OUTCOMES_PATH", str(tmp_path / "alert_outcomes.json"))


class _MemCache(dict):
    """Just the cache surface src/fundamental/market_cap.py uses."""

    def get(self, key, default=None):
        return super().get(key, default)

    def set(self, key, value, ttl=None):
        self[key] = value


@pytest.fixture(autouse=True)
def _offline_market_cap_fallbacks(request, monkeypatch):
    """Keep the market-cap fallback chain off the network and off data/.

    ``market_cap.sec_fallback`` ships ON and needs no key, so the credential
    blanking above cannot stop it: without this, any unit test that reaches
    ``fetch_fundamentals`` with no market cap would query SEC EDGAR. And the
    last-known-value cache lives in data/, where a live run leaves real values
    that would silently answer a test's "unknown market cap" premise. Tests of
    the chain itself re-enable SEC and patch the HTTP boundary.
    """
    if request.node.get_closest_marker("integration"):
        return
    import src.fundamental.market_cap as _mc
    monkeypatch.setattr(cfg, "SEC_MARKET_CAP_ENABLED", False)
    monkeypatch.setattr(_mc, "cache", _MemCache())
    monkeypatch.setattr(_mc, "_retry_after", 0.0)
