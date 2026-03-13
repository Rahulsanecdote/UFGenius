"""API validation/security tests for dashboard endpoints."""

from __future__ import annotations

import pandas as pd
import pytest

import dashboard
from src.utils.security import InMemoryRateLimiter, issue_dashboard_ui_token


@pytest.fixture(autouse=True)
def _reset_security_state(monkeypatch):
    monkeypatch.setattr(dashboard.config, "DASHBOARD_ALLOW_REMOTE", False)
    monkeypatch.setattr(dashboard.config, "DASHBOARD_API_KEY", "")
    monkeypatch.setattr(dashboard.config, "DASHBOARD_API_KEYS", "")
    monkeypatch.setattr(dashboard.config, "DASHBOARD_RATE_LIMIT_PER_MIN", 1000)
    monkeypatch.setattr(dashboard, "_rate_limiter", InMemoryRateLimiter(1000))
    yield


@pytest.fixture
def client():
    return dashboard.app.test_client()


def test_healthz_available_without_auth(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.get_json()["status"] == "ok"


def test_invalid_ticker_rejected(client):
    response = client.get("/api/scan-ticker?ticker=BAD$$$")
    assert response.status_code == 400
    assert "invalid" in response.get_json()["error"].lower()


def test_non_numeric_account_size_rejected(client):
    response = client.get("/api/scan-ticker?ticker=AAPL&account_size=abc")
    assert response.status_code == 400
    assert "numeric" in response.get_json()["error"].lower()


def test_negative_account_size_rejected(client):
    response = client.get("/api/scan?account_size=-1")
    assert response.status_code == 400
    assert "positive" in response.get_json()["error"].lower()


def test_internal_error_is_sanitized(client, monkeypatch):
    def _boom(*_args, **_kwargs):
        raise RuntimeError("very sensitive internal failure")

    monkeypatch.setattr(dashboard, "scan_single_ticker", _boom)
    response = client.get("/api/scan-ticker?ticker=AAPL&account_size=10000")
    assert response.status_code == 500
    payload = response.get_json()
    assert payload["error"] == "Internal server error"
    assert "sensitive" not in payload["error"].lower()


def test_rate_limiting_enforced(client, monkeypatch):
    monkeypatch.setattr(dashboard, "_rate_limiter", InMemoryRateLimiter(1))
    monkeypatch.setattr(dashboard, "run_daily_scan", lambda **_kwargs: {"ok": True})

    first = client.get("/api/scan?account_size=10000")
    second = client.get("/api/scan?account_size=10000")

    assert first.status_code in (200, 500)  # first may pass to handler
    assert second.status_code == 429


def test_remote_mode_requires_api_key(client, monkeypatch):
    monkeypatch.setattr(dashboard.config, "DASHBOARD_ALLOW_REMOTE", True)
    monkeypatch.setattr(dashboard.config, "DASHBOARD_API_KEY", "secret")
    monkeypatch.setattr(dashboard, "run_daily_scan", lambda **_kwargs: {"ok": True})

    no_key = client.get("/api/scan?account_size=10000")
    with_key = client.get("/api/scan?account_size=10000", headers={"X-API-Key": "secret"})

    assert no_key.status_code == 401
    assert with_key.status_code == 200


def test_remote_mode_allows_bearer_or_multi_keys(client, monkeypatch):
    monkeypatch.setattr(dashboard.config, "DASHBOARD_ALLOW_REMOTE", True)
    monkeypatch.setattr(dashboard.config, "DASHBOARD_API_KEY", "")
    monkeypatch.setattr(dashboard.config, "DASHBOARD_API_KEYS", "key1,key2")
    monkeypatch.setattr(dashboard, "run_daily_scan", lambda **_kwargs: {"ok": True})

    bearer = client.get("/api/scan?account_size=10000", headers={"Authorization": "Bearer key2"})
    bad = client.get("/api/scan?account_size=10000", headers={"Authorization": "Bearer nope"})

    assert bearer.status_code == 200
    assert bad.status_code == 401


def test_remote_mode_allows_signed_dashboard_ui_token(client, monkeypatch):
    monkeypatch.setattr(dashboard.config, "DASHBOARD_ALLOW_REMOTE", True)
    monkeypatch.setattr(dashboard.config, "DASHBOARD_API_KEY", "secret")
    monkeypatch.setattr(dashboard, "run_daily_scan", lambda **_kwargs: {"ok": True})

    token = issue_dashboard_ui_token()
    response = client.get("/api/scan?account_size=10000", headers={"X-Dashboard-Token": token})

    assert response.status_code == 200


def test_index_embeds_dashboard_ui_token_when_remote_enabled(client, monkeypatch):
    monkeypatch.setattr(dashboard.config, "DASHBOARD_ALLOW_REMOTE", True)
    monkeypatch.setattr(dashboard.config, "DASHBOARD_API_KEY", "secret")

    response = client.get("/")

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "const API_TOKEN =" in html
    assert "Analysis Workspace" in html
    assert "Provider Health" in html
    assert "scanSpotlights" in html
    assert "scan.pipeline_note || scan.alert" in html
    assert "[hidden] {" in html
    assert "display: none !important;" in html


def test_price_history_rejects_invalid_range(client):
    response = client.get("/api/price-history?ticker=AAPL&range=bad")

    assert response.status_code == 400
    assert "range" in response.get_json()["error"].lower()


def test_price_history_returns_chart_payload(client, monkeypatch):
    idx = pd.date_range("2024-01-01", periods=5, freq="D")
    sample = pd.DataFrame(
        {
            "Open": [10, 11, 12, 13, 14],
            "High": [11, 12, 13, 14, 15],
            "Low": [9, 10, 11, 12, 13],
            "Close": [10, 11, 12, 13, 14],
            "Volume": [1000, 1100, 1200, 1300, 1400],
        },
        index=idx,
    )
    monkeypatch.setattr(dashboard, "fetch_ohlcv", lambda *_args, **_kwargs: sample)

    response = client.get("/api/price-history?ticker=AAPL&range=1M")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ticker"] == "AAPL"
    assert payload["status"] == "READY"
    assert len(payload["points"]) == 5
    assert "accessible_summary" in payload["summary"]


def test_regime_endpoint_includes_cache_freshness(client, monkeypatch):
    monkeypatch.setattr(
        dashboard,
        "detect_market_regime",
        lambda: {"regime": "NEUTRAL_CHOPPY", "strategy": {"bias": "NEUTRAL"}, "flags": []},
    )
    monkeypatch.setattr(
        dashboard,
        "get_regime_cache_freshness",
        lambda **_kwargs: {"any_regime_stale": True, "max_age_human": "2h 30m"},
    )

    response = client.get("/api/regime")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["regime"] == "NEUTRAL_CHOPPY"
    assert payload["cache_freshness"]["any_regime_stale"] is True
    assert payload["cache_freshness"]["max_age_human"] == "2h 30m"
