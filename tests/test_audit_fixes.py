"""Tests for audit remediation.

Covers the targeted bug fixes made on top of restoring the modules that commit
0f264d6 accidentally deleted. Offline-only; heavy-dep chains are guarded with
importorskip. The restored original suites (test_generator, test_executor,
test_position_tracker, test_orders, test_phase3_features, ...) cover those
modules' own behavior.
"""

import pathlib

import numpy as np
import pandas as pd
import pytest

_ROOT = pathlib.Path(__file__).parent.parent


# ── Regression guard: the mass-deleted modules must stay present ────────────
def test_previously_deleted_modules_present():
    """Guards against re-introducing the 0f264d6 accidental deletion."""
    required = [
        "src/core/models.py",
        "src/signals/generator.py",
        "src/alpaca/executor.py",
        "src/alpaca/orders.py",
        "src/alpaca/position_tracker.py",
        "src/features/__init__.py",
        "src/features/policies.py",
        "src/features/signal_features.py",
        "src/features/store.py",
    ]
    missing = [r for r in required if not (_ROOT / r).exists()]
    assert not missing, f"missing restored modules: {missing}"


# ── C4: stale-cache fallback methods exist and behave ───────────────────────
def test_cache_get_stale_and_metadata(tmp_path, monkeypatch):
    from src.data import cache

    monkeypatch.setattr(cache, "_CACHE_DIR", tmp_path)

    cache.set("k", {"v": 1}, ttl=100)
    assert cache.get("k") == {"v": 1}
    assert cache.get_stale("k") == {"v": 1}
    assert cache.get_metadata("k")["is_expired"] is False

    cache.set("k", {"v": 2}, ttl=-1)  # expire
    assert cache.get("k") is None            # fresh miss
    assert cache.get_stale("k") == {"v": 2}  # stale survives (not deleted on read)
    assert cache.get_metadata("k")["is_expired"] is True
    assert cache.get_metadata("k", allow_expired=False) is None
    assert cache.get_stale("missing") is None
    assert cache.get_metadata("missing") is None


# ── C3: position sizing never floors to 1 / oversizes ───────────────────────
def _synthetic_df(price: float, rows: int = 80) -> pd.DataFrame:
    idx = pd.date_range("2023-01-01", periods=rows, freq="D")
    close = np.linspace(price * 0.9, price, rows)
    return pd.DataFrame(
        {"Open": close, "High": close * 1.01, "Low": close * 0.99,
         "Close": close, "Volume": np.full(rows, 1_000_000)},
        index=idx,
    )


def test_sizing_skips_when_below_one_share():
    from src.signals.trade_plan import generate_trade_plan

    plan = generate_trade_plan(
        "XYZ", {"current_price": 1500.0, "signal": "BUY", "score": 70},
        account_size=1000, df=_synthetic_df(1500.0),
    )
    assert plan.get("skip") is True
    assert "position" not in plan


def test_sizing_respects_max_position_pct():
    from src.signals.trade_plan import generate_trade_plan
    from src.utils import config

    plan = generate_trade_plan(
        "XYZ", {"current_price": 1500.0, "signal": "BUY", "score": 70},
        account_size=100_000, df=_synthetic_df(1500.0),
    )
    assert not plan.get("skip")
    assert plan["position"]["position_value"] <= 100_000 * config.MAX_POSITION_PCT + 1e-6
    assert plan["position"]["shares"] >= 1


# ── H6: Altman Z returns None instead of a false "safe" score ────────────────
def test_altman_z_none_when_liabilities_missing():
    pytest.importorskip("yfinance")
    from src.fundamental.scorer import _altman_z

    base = {"total_assets": 1e9, "market_cap": 5e10, "revenue": 2e9, "ebit": 3e8}
    assert _altman_z(base) is None  # no liabilities => undefined, not "safe"
    z = _altman_z({**base, "total_liabilities": 4e8})
    assert z is not None and z < 1000


# ── H2: XFF rate-limit key is not spoofable via the leftmost entry ──────────
def test_resolve_client_ip_uses_rightmost_validated(monkeypatch):
    pytest.importorskip("flask")
    from types import SimpleNamespace

    from src.utils import config, security

    monkeypatch.setattr(config, "DASHBOARD_TRUST_PROXY", True)
    req = SimpleNamespace(
        headers={"X-Forwarded-For": "1.1.1.1, 2.2.2.2, 3.3.3.3"},
        remote_addr="9.9.9.9",
    )
    assert security.resolve_client_ip(req) == "3.3.3.3"  # proxy-appended, not spoofable

    req.headers["X-Forwarded-For"] = "1.1.1.1, not-an-ip"
    assert security.resolve_client_ip(req) == "9.9.9.9"  # garbage => socket peer
