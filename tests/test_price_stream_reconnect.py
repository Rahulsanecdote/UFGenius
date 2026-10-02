"""The price stream's reconnect loop, against a local websocket server.

On 2026-10-02 the Render dashboard (512 MB) was OOM-killed twice, each time
after ~10 minutes of Alpaca answering the stream's login with "connection limit
exceeded" about twice a second. alpaca-py's own loop retries instantly and never
closes the refused socket; ``price_stream._guarded_stream_class()`` replaces that
loop. These tests run the REAL alpaca-py client (the guarded subclass) against a
server on 127.0.0.1 that answers like Alpaca — no credentials, no internet.
"""

from __future__ import annotations

import asyncio
import threading
import time

import msgpack
import pytest
import websockets
from websockets.asyncio.server import serve

import src.utils.config as cfg
from src.streaming import price_stream
from src.streaming.price_stream import PriceStream

_LIMIT = {"T": "error", "code": 406, "msg": "connection limit exceeded"}
_NO_PLAN = {"T": "error", "code": 409, "msg": "insufficient subscription"}


class FakeAlpaca:
    """Answers the data-stream handshake the way Alpaca does.

    mode: ``refuse`` (406, then keeps the socket open until the CLIENT closes
    it — the case that leaks), ``refuse_close`` (406, then closes), ``no_plan``
    (409), ``accept`` (authenticates, acks the subscription, sends one AAPL
    trade), ``drop_first`` (like accept, but drops the first connection right
    after the subscription).
    """

    def __init__(self, mode: str):
        self.mode = mode
        self.total = 0
        self.open = 0
        self.max_open = 0
        self.subscribes: list[dict] = []
        self._lock = threading.Lock()
        self._loop = None
        self._stop = None
        self.port = None

    async def _handler(self, ws):
        with self._lock:
            self.total += 1
            n = self.total
            self.open += 1
            self.max_open = max(self.max_open, self.open)
        try:
            await ws.send(msgpack.packb([{"T": "success", "msg": "connected"}]))
            await ws.recv()                                    # the auth message
            if self.mode in ("refuse", "refuse_close", "no_plan"):
                await ws.send(msgpack.packb([_NO_PLAN if self.mode == "no_plan" else _LIMIT]))
                if self.mode == "refuse_close":
                    await ws.close()
                else:
                    await ws.wait_closed()
                return
            await ws.send(msgpack.packb([{"T": "success", "msg": "authenticated"}]))
            self.subscribes.append(msgpack.unpackb(await ws.recv()))
            await ws.send(msgpack.packb([{"T": "subscription", "trades": ["AAPL"], "quotes": [],
                                          "bars": []}]))
            if self.mode == "drop_first" and n == 1:
                await ws.close()
                return
            await ws.send(msgpack.packb([{
                "T": "t", "S": "AAPL", "i": 1, "x": "V", "p": 187.25, "s": 100,
                "t": msgpack.Timestamp.from_unix_nano(time.time_ns()), "c": ["@"], "z": "C",
            }]))
            await ws.wait_closed()
        except websockets.ConnectionClosed:
            pass
        finally:
            with self._lock:
                self.open -= 1

    def start(self):
        ready = threading.Event()

        async def main():
            self._loop = asyncio.get_running_loop()
            self._stop = asyncio.Event()
            async with serve(self._handler, "127.0.0.1", 0) as server:
                self.port = next(iter(server.sockets)).getsockname()[1]
                ready.set()
                await self._stop.wait()

        threading.Thread(target=lambda: asyncio.run(main()), daemon=True).start()
        assert ready.wait(5), "fake server did not start"
        return self

    def stop(self):
        loop, self._loop = self._loop, None
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(self._stop.set)

    @property
    def url(self):
        return f"ws://127.0.0.1:{self.port}"


@pytest.fixture
def server():
    started = []

    def _make(mode):
        srv = FakeAlpaca(mode).start()
        started.append(srv)
        return srv

    yield _make
    for srv in started:
        srv.stop()


def _client(srv, backoff=0.05, cap=0.2):
    return price_stream._guarded_stream_class()(
        "k", "s", url_override=srv.url, backoff_sec=backoff, backoff_max_sec=cap)


async def _noop(_trade):
    pass


def _run(client, seconds):
    t = threading.Thread(target=client.run, daemon=True)
    t.start()
    time.sleep(seconds)
    return t


def _stop(client, thread, timeout=3.0):
    client.stop()
    thread.join(timeout)
    return not thread.is_alive()


# ── the OOM: a refused login ─────────────────────────────────────────────────

def test_a_refused_login_is_closed_and_backed_off_not_leaked(server):
    srv = server("refuse")
    client = _client(srv)
    client.subscribe_trades(_noop, "AAPL")
    t = _run(client, 1.5)
    assert srv.max_open <= 1               # each refused socket is closed before the next try
    assert 4 <= srv.total <= 15            # 0.05 → 0.1 → 0.2 (cap) …, not thousands
    state = client.guard_state
    assert state["connected"] is False
    assert state["failures"] >= 4
    assert "connection limit exceeded" in state["last_error"]
    assert _stop(client, t)
    time.sleep(0.2)
    assert srv.open == 0


def test_upstream_alpaca_py_still_leaks__the_reason_the_override_exists(server):
    """If this starts failing, alpaca-py has fixed its loop upstream and the
    guarded subclass may no longer be needed."""
    from alpaca.data.live import StockDataStream
    srv = server("refuse")
    client = StockDataStream("k", "s", url_override=srv.url)
    client.subscribe_trades(_noop, "AAPL")
    t = _run(client, 0.3)
    leaked = srv.open
    client.stop()
    t.join(1)
    assert leaked > 20


def test_backoff_doubles_to_the_cap(server):
    srv = server("refuse_close")
    client = _client(srv, backoff=0.1, cap=0.4)
    client.subscribe_trades(_noop, "AAPL")
    t = _run(client, 1.6)                  # attempts at ~0, 0.1, 0.3, 0.7, 1.1, 1.5
    assert 4 <= srv.total <= 7
    assert client.guard_state["retry_at"] is not None
    assert _stop(client, t)


def test_stop_is_honoured_during_a_long_backoff(server):
    srv = server("refuse")
    client = _client(srv, backoff=60, cap=60)
    client.subscribe_trades(_noop, "AAPL")
    t = _run(client, 0.5)
    assert srv.total == 1
    started = time.monotonic()
    assert _stop(client, t)
    assert time.monotonic() - started < 2


def test_a_feed_the_plan_does_not_cover_gives_up(server):
    srv = server("no_plan")
    client = _client(srv)
    client.subscribe_trades(_noop, "AAPL")
    t = _run(client, 0.8)
    assert not t.is_alive()
    assert srv.total == 1
    assert "insufficient subscription" in client.guard_state["last_error"]


# ── the CPU spin: nothing subscribed yet ─────────────────────────────────────

def test_waiting_for_a_first_subscription_does_not_spin():
    client = price_stream._guarded_stream_class()("k", "s", url_override="ws://127.0.0.1:9")
    cpu0 = time.process_time()
    t = _run(client, 1.0)
    used = time.process_time() - cpu0
    assert used < 0.3, f"idle stream used {used:.2f}s of CPU in 1s"   # upstream: ~1.0
    assert _stop(client, t)


# ── the happy path still works ───────────────────────────────────────────────

def test_connects_subscribes_and_delivers_trades_through_pricestream(server, monkeypatch):
    monkeypatch.setattr(cfg, "MOVERS_STREAM_ENABLED", True)
    srv = server("accept")
    s = PriceStream(api_key="k", secret_key="s",
                    client_factory=lambda k, sec, feed: _client(srv))
    assert s.start(["AAPL"])
    deadline = time.time() + 3
    while time.time() < deadline and s.latest("AAPL") is None:
        time.sleep(0.05)
    try:
        assert s.latest("AAPL")["price"] == 187.25
        assert srv.subscribes and srv.subscribes[0]["trades"] == ["AAPL"]
        status = s.status()
        assert status["connected"] is True and status["connect_failures"] == 0
        assert status["last_error"] is None
    finally:
        srv.stop()
        s.stop()


def test_a_dropped_connection_reconnects_and_clears_the_failure_count(server):
    srv = server("drop_first")
    client = _client(srv)
    client.subscribe_trades(_noop, "AAPL")
    t = _run(client, 1.0)
    assert srv.total == 2
    assert client.guard_state == {**client.guard_state, "connected": True, "failures": 0,
                                  "last_error": None}
    # Connected, alpaca-py's _consume only checks for stop between 5s recv
    # timeouts; dropping the server first gets the loop into a backoff pause.
    srv.stop()
    assert _stop(client, t)


def test_status_reports_a_refused_stream_as_live_but_not_connected(server, monkeypatch):
    monkeypatch.setattr(cfg, "MOVERS_STREAM_ENABLED", True)
    srv = server("refuse")
    s = PriceStream(api_key="k", secret_key="s",
                    client_factory=lambda k, sec, feed: _client(srv, backoff=5, cap=5))
    s.start(["AAPL"])
    time.sleep(0.5)
    try:
        st = s.status()
        assert st["live"] is True and st["connected"] is False
        assert st["connect_failures"] == 1
        assert st["last_error"] == "connection limit exceeded"
        assert 0 < st["retry_in_seconds"] <= 5
    finally:
        s.stop()


# ── the private surface the override depends on ──────────────────────────────

def test_alpaca_py_still_has_the_members_the_override_uses():
    from alpaca.data.live import StockDataStream
    c = StockDataStream("k", "s")
    for attr in ("_handlers", "_stop_stream_queue", "_should_run", "_running", "_ws", "_loop"):
        assert hasattr(c, attr), attr
    for meth in ("_start_ws", "_send_subscribe_msg", "_consume", "_run_forever", "stop_ws", "run"):
        assert callable(getattr(StockDataStream, meth, None)), meth
    assert set(c._handlers) >= {"trades", "cancelErrors", "corrections"}


def test_the_production_client_is_the_guarded_one(monkeypatch):
    monkeypatch.setattr(cfg, "MOVERS_STREAM_RECONNECT_BACKOFF_SEC", 3.0)
    monkeypatch.setattr(cfg, "MOVERS_STREAM_RECONNECT_BACKOFF_MAX_SEC", 90.0)
    client = PriceStream(api_key="k", secret_key="s")._build_client()
    assert isinstance(client, price_stream._guarded_stream_class())
    assert (client._backoff_sec, client._backoff_max_sec) == (3.0, 90.0)
