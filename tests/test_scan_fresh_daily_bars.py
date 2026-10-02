"""Each scan reads daily bars fetched during that scan.

The daily cache lives 24h. On 2026-10-02 the paper trader's 14:00 ET scan
fetched the S&P 500 (503/503 in 16 requests); its 16:30 ET scan made no request
at all and judged those same bars, which ended 13:44 ET. A Monday 06:00 fetch
would have served Friday's close to every Monday scan. run_daily_scan now raises
a floor on cached daily frames to its own start time.
"""

from __future__ import annotations

import time

import pandas as pd
import pytest

import src.utils.config as cfg
from src.data import fetcher


def _frame(close: float, fetched_at=None) -> pd.DataFrame:
    df = pd.DataFrame({"Open": [close], "High": [close], "Low": [close], "Close": [close],
                       "Volume": [1_000_000]}, index=pd.to_datetime(["2026-10-02"]))
    df.attrs["volume_feed"] = "sip"
    if fetched_at is not None:
        df.attrs["fetched_at"] = fetched_at
    return df


@pytest.fixture
def store(monkeypatch):
    """An in-memory cache plus a counting downloader that returns close=200."""
    mem: dict = {}
    downloads: list = []
    monkeypatch.setattr(cfg, "ALPACA_DAILY_FEED", "sip")
    monkeypatch.setattr(fetcher.cache, "get", lambda key: mem.get(key))
    monkeypatch.setattr(fetcher.cache, "set", lambda key, value, ttl=None: mem.__setitem__(key, value))
    monkeypatch.setattr(fetcher, "_download_ohlcv_once",
                        lambda symbol, *a, **k: downloads.append(symbol) or _frame(200.0))
    monkeypatch.setattr(fetcher, "_download_ohlcv_batch_via_alpaca", lambda *a, **k: {})
    return mem, downloads


def test_without_a_floor_the_cache_answers_as_before(store):
    """Backtests and dashboard reads: no scan running, the 24h cache is kept."""
    mem, downloads = store
    mem["ohlcv:BLK:1y:1d"] = _frame(100.0)                      # no fetched_at at all
    assert fetcher.fetch_ohlcv("BLK", period="1y")["Close"].iloc[-1] == 100.0
    assert downloads == []


def test_a_scan_refetches_bars_cached_by_an_earlier_scan(store):
    mem, downloads = store
    two_thirty_ago = time.time() - 2.5 * 3600                  # the 14:00 ET fetch
    mem["ohlcv:BLK:1y:1d"] = _frame(100.0, fetched_at=two_thirty_ago)
    fetcher.require_daily_bars_fetched_since(time.time())      # the 16:30 ET scan starts
    df = fetcher.fetch_ohlcv("BLK", period="1y")
    assert df["Close"].iloc[-1] == 200.0 and downloads == ["BLK"]
    # Written back with its fetch time, so the rest of this scan hits the cache.
    assert fetcher.fetch_ohlcv("BLK", period="1y")["Close"].iloc[-1] == 200.0
    assert downloads == ["BLK"]


def test_bars_cached_before_fetch_times_were_recorded_are_refetched_too(store):
    mem, downloads = store
    mem["ohlcv:BLK:1y:1d"] = _frame(100.0)                      # written before this change
    fetcher.require_daily_bars_fetched_since(time.time())
    fetcher.fetch_ohlcv("BLK", period="1y")
    assert downloads == ["BLK"]


def test_the_batch_refetches_only_what_predates_the_scan(store):
    mem, downloads = store
    start = time.time()
    mem["ohlcv:OLD:1y:1d"] = _frame(100.0, fetched_at=start - 9000)
    mem["ohlcv:NEW:1y:1d"] = _frame(150.0, fetched_at=start + 1)   # fetched during this scan
    fetcher.require_daily_bars_fetched_since(start)
    out = fetcher.fetch_ohlcv_batch(["OLD", "NEW"], period="1y", interval="1d")
    assert downloads == ["OLD"]
    assert out["OLD"]["Close"].iloc[-1] == 200.0 and out["NEW"]["Close"].iloc[-1] == 150.0


def test_the_floor_only_rises(monkeypatch):
    fetcher.require_daily_bars_fetched_since(1000.0)
    fetcher.require_daily_bars_fetched_since(500.0)
    assert fetcher._daily_fetched_floor == 1000.0


def test_intraday_frames_are_not_affected(store):
    fetcher.require_daily_bars_fetched_since(time.time())
    assert fetcher._cached_frame_usable(_frame(1.0), "5m")


def test_the_feed_check_still_applies_to_fresh_frames(store):
    fetcher.require_daily_bars_fetched_since(time.time() - 60)
    fresh_iex = _frame(1.0, fetched_at=time.time())
    fresh_iex.attrs["volume_feed"] = "iex"
    assert not fetcher._cached_frame_usable(fresh_iex, "1d")


# ── the scan sets the floor before its first daily read ──────────────────────

def _stub_scan(monkeypatch, seen: dict):
    import src.scanner.daily_scan as ds

    def regime():
        seen["floor_at_regime"] = fetcher._daily_fetched_floor
        return {"regime": "BEAR_RISK_OFF", "regime_score": 10, "strategy": "cash", "vix_level": 30}

    monkeypatch.setattr(ds, "detect_market_regime", regime)
    monkeypatch.setattr(ds, "_check_data_gap", lambda: None)
    monkeypatch.setattr(ds, "_record_scan_metrics", lambda *a, **k: None)
    monkeypatch.setitem(cfg.SAFETY, "trade_in_bear_market", False)
    return ds


def test_run_daily_scan_raises_the_floor_before_the_regime_read(monkeypatch):
    seen: dict = {}
    ds = _stub_scan(monkeypatch, seen)
    monkeypatch.setattr(cfg, "SCAN_REFRESH_DAILY_BARS", True)
    before = time.time()
    ds.run_daily_scan()
    assert before - 1 <= seen["floor_at_regime"] <= time.time()


def test_the_refresh_can_be_switched_off(monkeypatch):
    seen: dict = {}
    ds = _stub_scan(monkeypatch, seen)
    monkeypatch.setattr(cfg, "SCAN_REFRESH_DAILY_BARS", False)
    ds.run_daily_scan()
    assert seen["floor_at_regime"] == 0.0
