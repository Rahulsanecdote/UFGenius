"""Tests for the Phase 8 price stream (streaming/price_stream.py).

The whole websocket lifecycle is exercised with a fake stream client — no
network and no live event loop. The async tick handler is driven directly with
``asyncio.run`` (it does only synchronous, lock-guarded work).
"""

import asyncio
import threading

import pytest

import src.utils.config as cfg
from src.streaming.price_stream import PriceStream


class FakeTrade:
    def __init__(self, symbol, price, size=100, ts=None):
        self.symbol = symbol
        self.price = price
        self.size = size
        self.timestamp = ts  # None → handler falls back to utcnow iso


class FakeStream:
    """Stand-in for alpaca StockDataStream: records calls, run() blocks like the loop."""
    def __init__(self, *args, **kwargs):
        self.handler = None
        self.subscribed = set()
        self.unsubscribed = set()
        self.ran = False
        self.stopped = False
        self._ev = threading.Event()

    def subscribe_trades(self, handler, *syms):
        self.handler = handler
        self.subscribed.update(syms)

    def unsubscribe_trades(self, *syms):
        for s in syms:
            self.subscribed.discard(s)
        self.unsubscribed.update(syms)

    def run(self):
        self.ran = True
        self._ev.wait()      # block until stop(), mimicking the real event loop

    def stop(self):
        self.stopped = True
        self._ev.set()


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setattr(cfg, "MOVERS_STREAM_ENABLED", True)
    monkeypatch.setattr(cfg, "MOVERS_STREAM_FEED", "iex")
    monkeypatch.setattr(cfg, "MOVERS_STREAM_STALE_SEC", 10.0)
    monkeypatch.setattr(cfg, "MOVERS_STREAM_MAX_SYMBOLS", 30)


def _stream(fake):
    return PriceStream(api_key="k", secret_key="s",
                       client_factory=lambda *a, **k: fake)


def test_disabled_returns_false(monkeypatch):
    monkeypatch.setattr(cfg, "MOVERS_STREAM_ENABLED", False)
    s = PriceStream(api_key="k", secret_key="s", client_factory=lambda *a, **k: FakeStream())
    assert s.start(["AAPL"]) is False
    assert s.is_live() is False


def test_missing_credentials_returns_false(enabled):
    s = PriceStream(api_key="", secret_key="", client_factory=lambda *a, **k: FakeStream())
    assert s.start(["AAPL"]) is False


def test_client_build_failure_is_soft(enabled):
    def boom(*a, **k):
        raise RuntimeError("no alpaca-py")
    s = PriceStream(api_key="k", secret_key="s", client_factory=boom)
    assert s.start(["AAPL"]) is False
    assert s.is_live() is False


def test_start_subscribe_snapshot_stop(enabled):
    fake = FakeStream()
    s = _stream(fake)
    try:
        assert s.start(["AAPL", "TSLA"]) is True
        assert s.is_live() is True
        assert fake.ran is True                 # loop running on the thread
        assert fake.subscribed == {"AAPL", "TSLA"}
        # deliver a tick through the async handler
        asyncio.run(fake.handler(FakeTrade("AAPL", 191.23)))
        px = s.latest("AAPL")
        assert px is not None and px["price"] == 191.23 and px["fresh"] is True
        assert "AAPL" in s.snapshot()
        st = s.status()
        assert st["live"] is True and st["subscribed_count"] == 2 and st["tick_count"] == 1
    finally:
        s.stop()
    assert fake.stopped is True
    assert s.is_live() is False


def test_set_symbols_diffs_subscriptions(enabled):
    fake = FakeStream()
    s = _stream(fake)
    try:
        s.start(["AAPL"])
        asyncio.run(fake.handler(FakeTrade("AAPL", 10.0)))
        s.set_symbols(["AAPL", "NVDA"])         # add NVDA
        assert fake.subscribed == {"AAPL", "NVDA"}
        s.set_symbols(["NVDA"])                  # drop AAPL
        assert "AAPL" in fake.unsubscribed
        assert fake.subscribed == {"NVDA"}
        assert s.latest("AAPL") is None          # cached price dropped on unsubscribe
    finally:
        s.stop()


def test_stale_flag_when_tick_is_old(enabled):
    fake = FakeStream()
    s = _stream(fake)
    try:
        s.start(["AAPL"])
        asyncio.run(fake.handler(FakeTrade("AAPL", 5.0)))
        recent = s.latest("AAPL")
        old = s.latest("AAPL", now=recent and 10_000_000_000.0)  # far future
        assert recent["fresh"] is True
        assert old["fresh"] is False
    finally:
        s.stop()


def test_normalize_caps_and_dedupes(enabled, monkeypatch):
    monkeypatch.setattr(cfg, "MOVERS_STREAM_MAX_SYMBOLS", 2)
    fake = FakeStream()
    s = _stream(fake)
    try:
        s.start(["aapl", "AAPL", "tsla", "NVDA"])   # dedupe (case) + cap at 2
        assert fake.subscribed == {"AAPL", "TSLA"}
    finally:
        s.stop()


def test_handler_ignores_bad_frames(enabled):
    fake = FakeStream()
    s = _stream(fake)
    try:
        s.start(["AAPL"])
        asyncio.run(fake.handler(object()))          # no symbol/price → ignored
        asyncio.run(fake.handler(FakeTrade("AAPL", None)))
        assert s.snapshot() == {} or s.latest("AAPL") is None
    finally:
        s.stop()


def test_set_symbols_noop_when_not_running(enabled):
    fake = FakeStream()
    s = _stream(fake)
    # never started → no crash, no subscriptions
    s.set_symbols(["AAPL"])
    assert fake.subscribed == set()
