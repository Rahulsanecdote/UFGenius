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
    monkeypatch.setattr(
        dashboard.config, "PAPER_SCORECARD_BASELINE_PATH", str(tmp_path / "absent.json")
    )
    response = client.get("/api/paper-scorecard")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["n_trades"] == 0
    assert payload["acceptance"]["all_pass"] is False
    # No baseline saved → the comparison reports itself unavailable rather than
    # silently omitting the check.
    assert payload["baseline_comparison"]["available"] is False
    assert "--save-baseline" in payload["baseline_comparison"]["reason"]


def test_paper_scorecard_reports_the_baseline_comparison(client, monkeypatch, tmp_path):
    """With a baseline on disk the endpoint compares paper against it — advisory,
    regardless of whether the money-path gate is switched on."""
    from datetime import datetime, timezone

    from src.backtest.baseline import save_baseline

    path = str(tmp_path / "baseline.json")
    # Save as a composite-source baseline: a proxy one is refused outright by the
    # signal-source guard, which would short-circuit the check this test is for.
    monkeypatch.setattr(dashboard.config, "BACKTEST_SIGNAL_SOURCE", "composite")
    save_baseline(
        {"out_of_sample": {"win_rate_pct": 55.0, "profit_factor": 2.1},
         "verdict": {"validated": True},
         "split": {"out_of_sample": "2023-06-01 → 2023-12-31"}},
        path=path, now=datetime.now(timezone.utc),
    )
    monkeypatch.setattr(dashboard.config, "LIVE_POSITION_STORE_PATH", str(tmp_path / "pos.json"))
    monkeypatch.setattr(dashboard.config, "PAPER_SCORECARD_BASELINE_PATH", path)

    payload = client.get("/api/paper-scorecard").get_json()
    comparison = payload["baseline_comparison"]
    assert payload["baseline_gate_enabled"] is False
    assert comparison["all_pass"] is False           # no paper trades to compare yet
    assert comparison["comparable"] is False
    assert comparison["n_trades"] == 0
    assert "closed paper trades" in comparison["reason"]


def test_execution_quality_endpoint(client, monkeypatch, tmp_path):
    monkeypatch.setattr(dashboard.config, "EXEC_QUALITY_LEDGER_PATH", str(tmp_path / "eq.json"))
    response = client.get("/api/execution-quality")
    assert response.status_code == 200
    assert response.get_json()["n_fills"] == 0


def test_metrics_endpoint_empty(client, monkeypatch, tmp_path):
    import src.observability.metrics as metrics
    monkeypatch.setattr(dashboard.config, "METRICS_LEDGER_PATH", str(tmp_path / "m.json"))
    monkeypatch.setattr(metrics, "_default", None)
    response = client.get("/api/metrics")
    assert response.status_code == 200
    assert response.get_json()["n_scans"] == 0


def test_metrics_endpoint_reports_recorded_scan(client, monkeypatch, tmp_path):
    import src.observability.metrics as metrics
    monkeypatch.setattr(dashboard.config, "METRICS_LEDGER_PATH", str(tmp_path / "m.json"))
    monkeypatch.setattr(metrics, "_default", None)
    metrics.record_scan(9.0, 50, 4, label_counts={"BUY": 4}, regime="BULL")
    monkeypatch.setattr(metrics, "_default", None)  # force endpoint to reload from disk
    payload = client.get("/api/metrics").get_json()
    assert payload["n_scans"] == 1
    assert payload["last_scan_latency_sec"] == 9.0


def test_attribution_endpoint_empty(client, monkeypatch, tmp_path):
    monkeypatch.setattr(dashboard.config, "LIVE_POSITION_STORE_PATH", str(tmp_path / "pos.json"))
    response = client.get("/api/attribution")
    assert response.status_code == 200
    assert response.get_json()["overall"]["trades"] == 0


def test_explain_endpoint_disabled_is_graceful(client, monkeypatch):
    monkeypatch.setattr(dashboard.config, "EXPLAIN_ENABLED", False)
    response = client.get("/api/explain?ticker=AAPL")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["available"] is False


def test_explain_endpoint_returns_narrative_when_available(client, monkeypatch):
    monkeypatch.setattr(dashboard.config, "EXPLAIN_ENABLED", True)
    monkeypatch.setattr(dashboard, "scan_single_ticker",
                        lambda t, account_size=None: {"ticker": t, "composite_score": 70.0})
    import src.explain.narrative as nar
    monkeypatch.setattr(nar, "explain",
                        lambda plan=None, regime=None, client=None: {
                            "ticker": "AAPL", "narrative": "Bull vs bear.", "model": "claude-opus-5",
                            "disclaimer": "NOT financial advice."})
    response = client.get("/api/explain?ticker=AAPL")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["available"] is True
    assert payload["narrative"] == "Bull vs bear."


def test_explain_endpoint_rejects_bad_ticker(client, monkeypatch):
    monkeypatch.setattr(dashboard.config, "EXPLAIN_ENABLED", True)
    response = client.get("/api/explain?ticker=BAD$$$")
    assert response.status_code == 400


def test_portfolio_risk_endpoint_disabled_is_graceful(client, monkeypatch):
    monkeypatch.setattr(dashboard.config, "PORTFOLIO_ENABLED", False)
    response = client.get("/api/portfolio-risk")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["available"] is False


def test_portfolio_risk_endpoint_returns_book_metrics(client, monkeypatch):
    monkeypatch.setattr(dashboard.config, "PORTFOLIO_ENABLED", True)
    monkeypatch.setattr(dashboard.config, "PORTFOLIO_GATE_ENTRIES", False)
    import src.alpaca.portfolio as pf

    monkeypatch.setattr(
        pf,
        "get_portfolio_data",
        lambda: {
            "total_equity": 10_000,
            # Real provider shape: shares + current (per-share), no market_value.
            "holdings": [{"ticker": "AAPL", "shares": 10, "current": 250.0}],
        },
    )
    response = client.get("/api/portfolio-risk")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["available"] is True
    assert payload["gross_leverage"] == pytest.approx(0.25)  # 2500 / 10000
    assert payload["breaches"]["single_weight"] is True  # 25% > 20% cap
    assert payload["portfolio_heat"] is None  # no stop data → heat unmeasured
    assert payload["gate_entries"] is False


def test_portfolio_risk_endpoint_handles_unreadable_portfolio(client, monkeypatch):
    monkeypatch.setattr(dashboard.config, "PORTFOLIO_ENABLED", True)
    import src.alpaca.portfolio as pf

    monkeypatch.setattr(pf, "get_portfolio_data", lambda: {"error": "no broker keys"})
    payload = client.get("/api/portfolio-risk").get_json()
    assert payload["available"] is False
    assert "no broker keys" in payload["reason"]


def test_metrics_and_attribution_panels_present():
    # The dashboard HTML wires the new P2.3 panels + their loaders.
    assert 'id="metricsPanel"' in dashboard.HTML
    assert 'id="attributionPanel"' in dashboard.HTML
    assert 'id="execQualityPanel"' in dashboard.HTML
    assert "loadMetrics()" in dashboard.HTML
    assert "loadAttribution()" in dashboard.HTML


def test_ai_narrative_panel_present():
    # The P3.1 explainability panel + on-demand button are wired.
    assert 'id="aiNarrativePanel"' in dashboard.HTML
    assert 'id="aiNarrativeButton"' in dashboard.HTML
    assert "loadAiNarrative" in dashboard.HTML


def test_premarket_panel_present():
    # The pre-market screener panel, its penny toggle, and its loader are
    # wired (manual run only — no auto-refresh timer may reference it).
    assert 'id="premarketPanel"' in dashboard.HTML
    for element_id in ("pmRunButton", "pmPennyButton", "pmUniverse",
                       "pmTable", "pmBody", "pmStatus", "pmNearMisses"):
        assert f'id="{element_id}"' in dashboard.HTML, element_id
    assert "runPremarketScan" in dashboard.HTML
    assert "setPmPenny" in dashboard.HTML
    assert "/api/scan-premarket?universe=" in dashboard.HTML


def test_premarket_scan_stays_manual_only():
    # Every scan is a provider fan-out, so no timer may drive it — a future
    # setInterval/setTimeout regression would otherwise pass the wiring test
    # above (CodeRabbit). Tripwire, not a proof.
    import re
    assert not re.search(r"set(Interval|Timeout)\s*\([^;]{0,200}?runPremarketScan",
                         dashboard.HTML, re.DOTALL)
    # ...and it must not be added to the live-refresh loop or its visibility hook.
    live_refresh = dashboard.HTML.split("function startLiveRefresh")
    assert len(live_refresh) == 2, "startLiveRefresh moved — update this guard"
    assert "runPremarketScan" not in live_refresh[1][:2000]


@pytest.mark.parametrize("path", ["/api/metrics", "/api/attribution"])
def test_new_endpoints_require_api_key_when_remote(client, monkeypatch, path):
    # The global /api/ guard authenticates both new P2.3 routes in remote mode.
    monkeypatch.setattr(dashboard.config, "DASHBOARD_ALLOW_REMOTE", True)
    monkeypatch.setattr(dashboard.config, "DASHBOARD_API_KEY", "secret")
    assert client.get(path).status_code == 401
    assert client.get(path, headers={"X-API-Key": "secret"}).status_code == 200


@pytest.mark.parametrize("path", ["/api/metrics", "/api/attribution"])
def test_new_endpoints_are_rate_limited(client, monkeypatch, path):
    monkeypatch.setattr(dashboard, "_rate_limiter", InMemoryRateLimiter(1))
    first = client.get(path)
    second = client.get(path)
    assert first.status_code in (200, 500)
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


# ── /api/movers ───────────────────────────────────────────────────────────────

def test_movers_unavailable_without_fmp_key(client, monkeypatch):
    monkeypatch.setattr(dashboard.config, "FMP_KEY", "")
    r = client.get("/api/movers")
    assert r.status_code == 200
    data = r.get_json()
    assert data["available"] is False and data["movers"] == []


def test_movers_returns_ranked_list(client, monkeypatch):
    monkeypatch.setattr(dashboard.config, "FMP_KEY", "k")
    from src.scanner.movers import MoverCandidate

    fake = [
        MoverCandidate(ticker="NBIS", price=259.2, change_pct=34.1, direction="long",
                       sources=["gainers", "most_actives"], score=89.0, enriched=True,
                       rel_volume=10.3, momentum_pct=3.7, vwap_pct=6.4, is_breakout=True),
        MoverCandidate(ticker="BOXL", price=7.87, change_pct=167.7, direction="long",
                       sources=["gainers"], score=17.0, enriched=True,
                       rel_volume=0.1, momentum_pct=-17.8, vwap_pct=0.1),
    ]

    def _fake_fetch(**kwargs):
        assert "limit" in kwargs and "enrich" in kwargs   # endpoint passes both
        return fake

    monkeypatch.setattr("src.scanner.movers.fetch_market_movers", _fake_fetch)
    # fetch_market_movers is stubbed, so it never resets the thread-local run
    # health — pin it, or this reads whatever a previous test left behind.
    monkeypatch.setattr("src.scanner.movers.last_source_health",
                        lambda: {"attempted": ["gainers"], "succeeded": ["gainers"],
                                 "failed": [], "served_by": {"gainers": "fmp"}})
    r = client.get("/api/movers?enrich=true&limit=25")
    assert r.status_code == 200
    data = r.get_json()
    assert data["available"] is True
    assert data["count"] == 2 and data["enriched"] == 2 and data["enrich_requested"] is True
    assert data["movers"][0]["ticker"] == "NBIS"
    assert data["movers"][0]["direction"] == "long"


# ── /api/movers-worker (Phase 7 shared state) ───────────────────────────────────

def test_movers_worker_unavailable_when_not_running(client, monkeypatch, tmp_path):
    # Point at a non-existent state file → worker reported unavailable, not 500.
    monkeypatch.setattr(dashboard.config, "MOVERS_WORKER_STATE_PATH",
                        str(tmp_path / "nope.json"))
    r = client.get("/api/movers-worker")
    assert r.status_code == 200
    data = r.get_json()
    assert data["available"] is False and data["live"] is False


def test_movers_worker_reports_published_state(client, monkeypatch, tmp_path):
    path = str(tmp_path / "movers_worker.json")
    monkeypatch.setattr(dashboard.config, "MOVERS_WORKER_STATE_PATH", path)
    monkeypatch.setattr(dashboard.config, "MOVERS_WORKER_STATE_STALE_SEC", 180.0)
    from src.scanner.movers import MoverCandidate
    from src.scanner.movers_monitor import WatchState
    from src.scanner.movers_state import MoversWorkerState

    c = MoverCandidate(ticker="NBIS", price=10.0, change_pct=20.0, direction="long",
                       sources=["gainers"], score=88.0, rel_volume=2.5, enriched=True)
    MoversWorkerState(path=path).publish(
        cycle=7, stats={"discoveries": 3, "alerts": 2, "invalidations": 1},
        watching=[WatchState(candidate=c, entry_score=88.0)], scan_window_open=True)

    r = client.get("/api/movers-worker")
    assert r.status_code == 200
    data = r.get_json()
    assert data["available"] is True and data["live"] is True
    assert data["cycle"] == 7 and data["watching_count"] == 1
    assert data["watching"][0]["ticker"] == "NBIS"


def test_movers_reports_a_failed_discovery_source(client, monkeypatch):
    # A dead key / exhausted quota must not render as "no movers qualified".
    monkeypatch.setattr(dashboard.config, "FMP_KEY", "k")
    monkeypatch.setattr("src.scanner.movers.fetch_market_movers",
                        lambda **kwargs: [])
    monkeypatch.setattr("src.scanner.movers.last_source_health",
                        lambda: {"attempted": ["gainers"], "succeeded": [],
                                 "failed": ["gainers: HTTPError"]})
    payload = client.get("/api/movers").get_json()
    assert payload["available"] is False
    assert "gainers: HTTPError" in payload["reason"]
    assert "quota" in payload["reason"].lower()


def test_movers_flags_a_partial_source_failure_as_degraded(client, monkeypatch):
    # Some rows came back, but the list is incomplete — say so rather than
    # presenting it as the whole market.
    class _M:
        def as_dict(self):
            return {"ticker": "AAA", "price": 1.0, "change_pct": 5.0,
                    "direction": "long", "score": 50}
        enriched = False
    monkeypatch.setattr(dashboard.config, "FMP_KEY", "k")
    monkeypatch.setattr("src.scanner.movers.fetch_market_movers",
                        lambda **kwargs: [_M()])
    monkeypatch.setattr("src.scanner.movers.last_source_health",
                        lambda: {"attempted": ["gainers", "losers"],
                                 "succeeded": ["gainers"],
                                 "failed": ["losers: Timeout"]})
    payload = client.get("/api/movers").get_json()
    assert payload["available"] is True
    assert payload["degraded"] is True
    assert payload["source_errors"] == ["losers: Timeout"]


def test_movers_is_not_degraded_when_all_sources_are_healthy(client, monkeypatch):
    monkeypatch.setattr(dashboard.config, "FMP_KEY", "k")
    monkeypatch.setattr("src.scanner.movers.fetch_market_movers", lambda **kwargs: [])
    monkeypatch.setattr("src.scanner.movers.last_source_health",
                        lambda: {"attempted": ["gainers"], "succeeded": ["gainers"],
                                 "failed": []})
    payload = client.get("/api/movers").get_json()
    assert payload["available"] is True
    assert payload["degraded"] is False


def test_movers_stays_available_when_one_source_answered_empty(client, monkeypatch):
    # One source succeeded with an honestly empty list, another failed. That is
    # a degraded result, not an outage — reporting it unavailable would call a
    # quiet market a broken one (CodeRabbit).
    monkeypatch.setattr(dashboard.config, "FMP_KEY", "k")
    monkeypatch.setattr("src.scanner.movers.fetch_market_movers", lambda **kwargs: [])
    monkeypatch.setattr("src.scanner.movers.last_source_health",
                        lambda: {"attempted": ["gainers", "losers"],
                                 "succeeded": ["gainers"],
                                 "failed": ["losers: Timeout"]})
    payload = client.get("/api/movers").get_json()
    assert payload["available"] is True
    assert payload["degraded"] is True
    assert payload["count"] == 0


# ── background full scan (the scan no longer runs inside the request) ────────

def test_scan_endpoint_returns_a_job_not_a_result(client, monkeypatch):
    """/api/scan must hand back a job handle immediately.

    Inline, this endpoint fanned out ~500 tickers inside one request against
    `gunicorn --timeout 120` and the worker was killed mid-scan.
    """
    from src.scanner import scan_jobs

    scan_jobs.reset()
    monkeypatch.setattr(dashboard, "run_daily_scan",
                        lambda **_kwargs: {"total_scanned": 503})
    response = client.get("/api/scan?account_size=10000")
    assert response.status_code == 200
    body = response.get_json()
    assert body["status"] == "running"
    assert body["job_id"]
    assert "result" not in body        # the result arrives via the poll, not here
    scan_jobs.reset()


def test_scan_status_returns_the_result_when_done(client, monkeypatch):
    from src.scanner import scan_jobs
    import time

    scan_jobs.reset()
    monkeypatch.setattr(dashboard, "run_daily_scan",
                        lambda **_kwargs: {"total_scanned": 503, "strong_buys": []})
    job_id = client.get("/api/scan?account_size=10000").get_json()["job_id"]

    deadline = time.time() + 5
    body = {}
    while time.time() < deadline:
        body = client.get(f"/api/scan-status?job={job_id}").get_json()
        if body["status"] in ("done", "error"):
            break
        time.sleep(0.01)
    assert body["status"] == "done", body
    assert body["result"]["total_scanned"] == 503
    scan_jobs.reset()


def test_scan_status_unknown_job_explains_itself(client):
    body = client.get("/api/scan-status?job=nope").get_json()
    assert body["status"] == "unknown"
    assert "--workers 1" in body["error"]


def test_scan_status_without_a_job_id_reports_idle(client):
    from src.scanner import scan_jobs

    scan_jobs.reset()
    assert client.get("/api/scan-status").get_json()["status"] == "idle"


def test_alert_outcomes_endpoint(client, monkeypatch, tmp_path):
    from datetime import datetime, timedelta
    import pandas as pd
    from src.observability import alert_outcomes as ao

    monkeypatch.setattr(dashboard.config, "ALERT_OUTCOMES_ENABLED", True)
    monkeypatch.setattr(dashboard.config, "ALERT_OUTCOMES_PATH",
                        str(tmp_path / "ao.json"))
    monkeypatch.setattr(dashboard.config, "ALERT_OUTCOMES_HORIZONS_MIN", [30])
    monkeypatch.setattr(ao, "_default", None)   # fresh singleton on the new path

    t0 = datetime(2026, 8, 18, 14, 0)
    led = ao.default_ledger()
    led.record([{"ticker": "AAA", "direction": "long", "score": 80.0}],
               source="movers", now=t0)
    idx = pd.DatetimeIndex([t0 + timedelta(minutes=i) for i in range(35)])
    frame = pd.DataFrame({"Open": 100.0, "High": 100.0, "Low": 100.0,
                          "Close": [100.0 + i for i in range(35)],
                          "Volume": 1000}, index=idx)
    led.resolve(now=t0 + timedelta(minutes=31), fetch=lambda t: frame)

    body = client.get("/api/alert-outcomes").get_json()
    assert body["sources"]["movers"]["by_horizon"]["30"]["n"] == 1
    assert body["recent"][0]["ticker"] == "AAA"
    monkeypatch.setattr(ao, "_default", None)
