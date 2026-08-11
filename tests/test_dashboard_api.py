"""API validation/security tests for dashboard endpoints."""

from __future__ import annotations

import pytest

import dashboard
from src.utils.security import InMemoryRateLimiter


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


def test_breaker_state_endpoint(client, monkeypatch, tmp_path):
    monkeypatch.setattr(dashboard.config, "CIRCUIT_STATE_PATH", str(tmp_path / "cb.json"))
    response = client.get("/api/breaker-state")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["manual_halt"] is False
    assert payload["entries_blocked"] is False
    assert "broker_error_count" in payload


def test_breaker_halt_then_resume(client, monkeypatch, tmp_path):
    from src.alpaca.circuit_breaker import CircuitBreaker

    state_path = str(tmp_path / "cb.json")
    monkeypatch.setattr(dashboard.config, "CIRCUIT_STATE_PATH", state_path)

    halt = client.post("/api/breaker", json={"action": "halt", "reason": "manual test"})
    assert halt.status_code == 200
    assert halt.get_json()["state"]["manual_halt"] is True
    assert CircuitBreaker(path=state_path).load().manual_halt is True  # persisted

    resume = client.post("/api/breaker", json={"action": "resume"})
    assert resume.status_code == 200
    assert resume.get_json()["state"]["manual_halt"] is False


def test_breaker_rejects_bad_action(client, monkeypatch, tmp_path):
    monkeypatch.setattr(dashboard.config, "CIRCUIT_STATE_PATH", str(tmp_path / "cb.json"))
    response = client.post("/api/breaker", json={"action": "explode"})
    assert response.status_code == 400
    assert "action" in response.get_json()["error"].lower()


def test_paper_scorecard_endpoint(client, monkeypatch, tmp_path):
    monkeypatch.setattr(dashboard.config, "LIVE_POSITION_STORE_PATH", str(tmp_path / "pos.json"))
    response = client.get("/api/paper-scorecard")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["n_trades"] == 0
    assert payload["acceptance"]["all_pass"] is False


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


# ── dead UI-token path removed (audit M1) + security headers ─────────────────

def test_ui_token_machinery_is_gone():
    # Wiring the token up would have let any visitor of the unauthenticated
    # "/" page mint API credentials — the machinery must stay deleted.
    import src.utils.security as security

    assert not hasattr(security, "issue_dashboard_ui_token")
    assert "X-Dashboard-Token" not in dashboard.HTML
    assert "ui_token" not in dashboard.HTML


def test_ui_authenticates_with_plain_api_key():
    assert "X-API-Key" in dashboard.HTML  # browser sends the ordinary key
    assert "sessionStorage" in dashboard.HTML


def test_index_renders_without_token_template_var(client):
    response = client.get("/")
    assert response.status_code == 200


def test_security_headers_present_and_no_cors(client):
    response = client.get("/healthz")
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Referrer-Policy"] == "same-origin"
    # Same-origin only: no CORS grants anywhere.
    assert "Access-Control-Allow-Origin" not in response.headers


def test_api_responses_are_not_cached(client):
    response = client.get("/api/scan-ticker?ticker=BAD$$$")  # any /api/* response
    assert response.headers["Cache-Control"] == "no-store"
    assert "Access-Control-Allow-Origin" not in response.headers
