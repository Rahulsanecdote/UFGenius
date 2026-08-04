"""Tests for misc hardening (audit L1, L6, L7, L9)."""

from __future__ import annotations

import threading

import numpy as np
import pandas as pd
import pytest

import dashboard
from src.technical.trend import calculate_trend_indicators
from src.technical.volatility import calculate_volatility_indicators
from src.utils import http


def _ohlcv(rows: int = 80) -> pd.DataFrame:
    rng = np.random.default_rng(7)
    close = 100 + np.cumsum(rng.normal(0, 1, rows))
    dates = pd.date_range("2024-01-01", periods=rows, freq="D")
    return pd.DataFrame(
        {
            "Open": close, "High": close * 1.01, "Low": close * 0.99,
            "Close": close, "Volume": [1_000_000] * rows,
        },
        index=dates,
    )


# ── L1: /api/diagnose returns a minimal status, not internals ────────────────

_FULL_DIAGNOSE = {
    "python": "3.12.1 (main) [GCC 13]",
    "yfinance_version": "0.2.40",
    "pandas_version": "2.2.0",
    "tests": {
        "AAPL": {
            "status": "OK", "rows": 5, "columns": ["Open", "Close"],
            "last_close": 189.4, "provider": "yfinance", "elapsed_sec": 0.4,
        },
        "SPY": {
            "status": "ERROR", "error": "HTTPSConnectionPool(host=...): boom",
            "provider_failures": [{"provider": "polygon", "reason": "401"}],
            "elapsed_sec": 1.2,
        },
    },
    "fundamentals": {"status": "OK", "market_cap": 3e12, "keys": ["a", "b"]},
}


def test_sanitized_diagnose_drops_internals():
    out = dashboard._sanitize_diagnose(_FULL_DIAGNOSE)
    flat = str(out)
    assert "python" not in out and "yfinance_version" not in out
    assert "GCC" not in flat and "HTTPSConnectionPool" not in flat
    assert "provider" not in flat and "columns" not in flat
    assert out["tests"]["AAPL"] == {"status": "OK", "rows": 5, "elapsed_sec": 0.4}
    assert out["status"] == "degraded"  # SPY probe errored
    assert out["fundamentals"] == {"status": "OK", "market_cap": 3e12}


def test_sanitized_diagnose_ok_when_all_probes_pass():
    full = {"tests": {"AAPL": {"status": "OK_CACHED", "rows": 5, "elapsed_sec": 0.1}},
            "fundamentals": {"status": "OK"}}
    assert dashboard._sanitize_diagnose(full)["status"] == "ok"


def test_diagnose_route_serves_sanitized_payload(monkeypatch):
    monkeypatch.setattr(dashboard, "diagnose", lambda: _FULL_DIAGNOSE)
    client = dashboard.app.test_client()
    payload = client.get("/api/diagnose").get_json()
    assert "python" not in payload
    assert "error" not in str(payload["tests"])


# ── L6: Bollinger/HV use population std ──────────────────────────────────────

def test_bollinger_uses_population_std():
    df = _ohlcv()
    ind = calculate_volatility_indicators(df)
    window = df["Close"].tail(20)
    expected_upper = window.mean() + 2 * window.std(ddof=0)
    sample_upper = window.mean() + 2 * window.std(ddof=1)
    assert float(ind["BB_UPPER"].iloc[-1]) == pytest.approx(expected_upper, rel=1e-9)
    assert float(ind["BB_UPPER"].iloc[-1]) != pytest.approx(sample_upper, rel=1e-9)


def test_hv_uses_population_std():
    df = _ohlcv()
    ind = calculate_volatility_indicators(df)
    log_ret = np.log(df["Close"] / df["Close"].shift(1)).tail(20)
    expected = log_ret.std(ddof=0) * np.sqrt(252) * 100
    assert float(ind["HV_20"].iloc[-1]) == pytest.approx(expected, rel=1e-9)


# ── L7: no future-shifted series in trend indicators ─────────────────────────

def test_chikou_span_removed_from_trend_indicators():
    ind = calculate_trend_indicators(_ohlcv())
    assert "chikou_span" not in ind  # Close.shift(-26) is future data at bar t


# ── L9: sessions are per-thread ──────────────────────────────────────────────

def test_session_reused_within_thread():
    assert http.get_retry_session() is http.get_retry_session()


def test_sessions_differ_across_threads():
    main_session = http.get_retry_session()
    other: list = []

    t = threading.Thread(target=lambda: other.append(http.get_retry_session()))
    t.start()
    t.join()

    assert other and other[0] is not main_session
