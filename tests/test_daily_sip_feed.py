"""Daily bars come from the consolidated SIP tape, not one venue.

Measured 2026-10-02 against Alpaca with the free plan's keys:

    daily volume, 2026-10-01    IEX          SIP           IEX share
    AAPL                        1,479,671    50,149,031    3.0%
    BLK                            26,247       553,866    4.7%
    BRK.B                         273,664     7,064,144    3.9%

So with IEX every absolute volume threshold ran ~25-30x stricter than written
(the paper trader rejected BLK as ILLIQUID at 28,514). The free plan refuses SIP
for anything newer than 15 minutes — "subscription does not permit querying
recent SIP data", HTTP 403 — so daily requests end early, and a 403 falls back
to IEX rather than losing the data. Intraday stays on IEX: real-time SIP is paid.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

import src.utils.config as cfg
from src.data import fetcher


def _bar(day: int, v: float) -> dict:
    return {"t": f"2026-09-{day:02d}T04:00:00Z", "o": 1.0, "h": 1.0, "l": 1.0, "c": 1.0, "v": v}


class _Resp:
    def __init__(self, status: int, body=None, text: str = ""):
        self.status_code, self._body, self.text = status, body, text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._body


class _Alpaca:
    """Free-plan behaviour: SIP only for requests ending >= 15 minutes ago."""

    def __init__(self, sip_allowed: bool = True):
        self.sip_allowed = sip_allowed
        self.calls: list[dict] = []

    def get(self, url, headers=None, params=None, timeout=None):
        self.calls.append(dict(params))
        end = datetime.fromisoformat(params["end"].replace("Z", "+00:00"))
        recent = end > datetime.now(timezone.utc) - timedelta(minutes=15)
        if params["feed"] == "sip" and (recent or not self.sip_allowed):
            return _Resp(403, text='{"message":"subscription does not permit querying recent SIP data"}')
        vol = 550_000 if params["feed"] == "sip" else 26_000
        rows = [_bar(d, vol) for d in range(1, 4)]
        if url.endswith("/v2/stocks/bars"):
            return _Resp(200, {"bars": {s: rows for s in params["symbols"].split(",")},
                               "next_page_token": None})
        return _Resp(200, {"bars": rows})


def _end_offset_min(params) -> float:
    end = datetime.fromisoformat(params["end"].replace("Z", "+00:00"))
    return (datetime.now(timezone.utc) - end).total_seconds() / 60


@pytest.fixture
def alpaca(monkeypatch):
    def _install(**kw):
        fake = _Alpaca(**kw)
        monkeypatch.setattr(cfg, "ALPACA_API_KEY", "k")
        monkeypatch.setattr(cfg, "ALPACA_SECRET_KEY", "s")
        monkeypatch.setattr(cfg, "ALPACA_DAILY_FEED", "sip")
        monkeypatch.setattr(cfg, "ALPACA_SIP_DELAY_MIN", 16.0)
        monkeypatch.setattr(fetcher, "get_retry_session", lambda: fake)
        fetcher._SIP_REFUSAL_WARNED.clear()
        return fake
    return _install


# ── which feed each request asks for ─────────────────────────────────────────

def test_daily_bars_ask_for_sip_ending_early_enough_to_be_served(alpaca):
    fake = alpaca()
    df = fetcher._download_ohlcv_via_alpaca("BLK", period="1y", interval="1d")
    assert fake.calls[0]["feed"] == "sip"
    assert 15.5 < _end_offset_min(fake.calls[0]) < 17
    assert len(fake.calls) == 1
    assert df["Volume"].iloc[-1] == 550_000 and df.attrs["volume_feed"] == "sip"


def test_intraday_bars_stay_real_time_on_iex(alpaca):
    fake = alpaca()
    df = fetcher._download_ohlcv_via_alpaca("BLK", period="5d", interval="5m")
    assert fake.calls[0]["feed"] == "iex"
    assert _end_offset_min(fake.calls[0]) < 1
    assert df.attrs["volume_feed"] == "iex"


def test_the_batch_path_asks_for_sip_too(alpaca):
    fake = alpaca()
    out = fetcher._download_ohlcv_batch_via_alpaca(["BLK", "AAPL"], period="1y", interval="1d")
    assert fake.calls[0]["feed"] == "sip" and 15.5 < _end_offset_min(fake.calls[0]) < 17
    assert all(f.attrs["volume_feed"] == "sip" for f in out.values())


def test_iex_is_still_selectable(alpaca, monkeypatch):
    fake = alpaca()
    monkeypatch.setattr(cfg, "ALPACA_DAILY_FEED", "iex")
    fetcher._download_ohlcv_via_alpaca("BLK", period="1y", interval="1d")
    assert fake.calls[0]["feed"] == "iex" and _end_offset_min(fake.calls[0]) < 1


def test_a_real_time_sip_plan_can_drop_the_delay(alpaca, monkeypatch):
    fake = alpaca()
    monkeypatch.setattr(cfg, "ALPACA_SIP_DELAY_MIN", 0.0)
    fake.sip_allowed = True
    fetcher._download_ohlcv_via_alpaca("BLK", period="1y", interval="1d")
    # The fake enforces the free plan, so a 0 delay gets refused and falls back —
    # which is exactly what the knob must NOT be set to on the free plan.
    assert [c["feed"] for c in fake.calls] == ["sip", "iex"]


# ── refusal: fall back, keep the data, say so once ───────────────────────────

def test_a_refused_sip_request_falls_back_to_iex_and_says_so(alpaca, caplog):
    fake = alpaca(sip_allowed=False)
    with caplog.at_level(logging.WARNING, logger="src.data.fetcher"):
        df = fetcher._download_ohlcv_via_alpaca("BLK", period="1y", interval="1d")
        fetcher._download_ohlcv_via_alpaca("AAPL", period="1y", interval="1d")
    assert [c["feed"] for c in fake.calls] == ["sip", "iex", "sip", "iex"]
    assert _end_offset_min(fake.calls[1]) < 1          # IEX may run to now
    assert not df.empty and df.attrs["volume_feed"] == "iex-fallback"
    assert caplog.text.count("Alpaca refused SIP daily bars") == 1


def test_a_refused_batch_chunk_falls_back_whole(alpaca):
    fake = alpaca(sip_allowed=False)
    out = fetcher._download_ohlcv_batch_via_alpaca(["BLK", "AAPL"], period="1y", interval="1d")
    assert [c["feed"] for c in fake.calls] == ["sip", "iex"]
    assert sorted(out) == ["AAPL", "BLK"]
    assert all(f.attrs["volume_feed"] == "iex-fallback" for f in out.values())


# ── cached frames from before the switch ─────────────────────────────────────

def _frame(tag=None) -> pd.DataFrame:
    df = pd.DataFrame({"Open": [1.0], "High": [1.0], "Low": [1.0], "Close": [1.0], "Volume": [26_000]},
                      index=pd.to_datetime(["2026-10-01"]))
    if tag:
        df.attrs["volume_feed"] = tag
    return df


@pytest.mark.parametrize("tag,usable", [
    (None, False),            # cached before the switch: IEX volume, unlabelled
    ("iex", False),           # fetched while the daily feed was IEX
    ("sip", True), ("consolidated", True), ("iex-fallback", True),
])
def test_daily_cache_entries_are_judged_by_where_their_volume_came_from(monkeypatch, tag, usable):
    monkeypatch.setattr(cfg, "ALPACA_DAILY_FEED", "sip")
    assert fetcher._cached_frame_usable(_frame(tag), "1d") is usable


def test_intraday_and_iex_configs_take_any_cached_frame(monkeypatch):
    monkeypatch.setattr(cfg, "ALPACA_DAILY_FEED", "sip")
    assert fetcher._cached_frame_usable(_frame(None), "5m")
    monkeypatch.setattr(cfg, "ALPACA_DAILY_FEED", "iex")
    assert fetcher._cached_frame_usable(_frame(None), "1d")


def test_a_pre_switch_cache_entry_is_refetched_not_served(monkeypatch):
    monkeypatch.setattr(cfg, "ALPACA_DAILY_FEED", "sip")
    monkeypatch.setattr(fetcher.cache, "get", lambda key: _frame(None))
    written = {}
    monkeypatch.setattr(fetcher.cache, "set", lambda key, value, ttl=None: written.update({key: value}))
    fresh = _frame("sip")
    fresh["Volume"] = 550_000
    monkeypatch.setattr(fetcher, "_download_ohlcv_once", lambda *a, **k: fresh)
    df = fetcher.fetch_ohlcv("BLK", period="1y", interval="1d")
    assert df["Volume"].iloc[-1] == 550_000
    assert written["ohlcv:BLK:1y:1d"].attrs["volume_feed"] == "sip"


def test_frames_from_other_providers_are_labelled_consolidated(monkeypatch):
    monkeypatch.setattr(fetcher.cache, "get", lambda key: None)
    monkeypatch.setattr(fetcher.cache, "set", lambda *a, **k: None)
    monkeypatch.setattr(fetcher, "_download_ohlcv_once", lambda *a, **k: _frame(None))
    assert fetcher.fetch_ohlcv("BLK", period="1y", interval="1d").attrs["volume_feed"] == "consolidated"


# ── the consequence the fix exists for ───────────────────────────────────────

def test_blk_is_no_longer_rejected_as_illiquid():
    """The 20-day average the ILLIQUID filter reads, at the measured volumes."""
    from src.signals.filters import run_disqualification_filters
    idx = pd.date_range("2026-09-01", periods=30, freq="B")

    def frame(volume):
        return pd.DataFrame({"Open": 900.0, "High": 905.0, "Low": 895.0, "Close": 900.0,
                             "Volume": volume}, index=idx)

    def illiquid(df):
        reasons = run_disqualification_filters("BLK", df, {}, {"market_cap": 1.4e11})
        return any(r.startswith("ILLIQUID") for r in reasons)

    assert illiquid(frame(26_247))          # IEX: what the paper trader saw
    assert not illiquid(frame(553_866))     # SIP: the stock's actual volume
