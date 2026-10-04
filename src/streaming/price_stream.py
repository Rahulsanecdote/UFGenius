"""Real-time price streaming via Alpaca's market-data websocket (Phase 8).

Phases 5–7 react on a *poll* cadence: the worker re-fetches intraday bars every
cycle, so an invalidation is only noticed at the next poll. This module adds a
**push** source — Alpaca's trade websocket — so the worker has a live tape and
can see a price move the instant it prints, not on the next REST cycle.

Design — async, quarantined
---------------------------
The rest of UFGenius is deliberately synchronous ("no ``async def`` without
cause"). Alpaca's ``StockDataStream`` is asyncio-based, so this module is the one
place async is allowed — and it is **sealed off**: the websocket runs its own
event loop inside a daemon thread, and the only surface the rest of the app
touches is a plain, lock-guarded snapshot (``latest`` / ``snapshot`` / ``status``).
No ``async`` leaks past this file; callers stay synchronous.

The SDK is built for exactly this: ``run()`` owns its loop in the thread, while
``subscribe_*`` / ``unsubscribe_*`` / ``stop()`` are safe to call from another
thread (they hop onto the loop via ``run_coroutine_threadsafe``). We track the
desired symbol set and drive subscriptions by diff.

Opt-in and fail-open, like every other advisory layer (explain, portfolio,
alerts): default **off**, and if it is disabled, the credentials are missing,
``alpaca-py`` is absent, or the socket errors, ``start()`` returns ``False`` and
the system keeps running on its REST polling exactly as before. Nothing here
touches the money path — it is a data source, not a gate.

The reconnect loop is ours, not alpaca-py's
-------------------------------------------
alpaca-py 0.43.1's ``DataStream._run_forever`` has two defects, and on
2026-10-02 the first one OOM-killed the dashboard twice (512 MB, 15:19 and 16:15
UTC):

1. **A refused login is retried instantly, and the refused socket is never
   closed.** Alpaca's free data plan allows one websocket per account; a second
   client is answered "connection limit exceeded". ``_auth`` raises
   ``ValueError``, the generic handler logs a traceback and loops with
   ``asyncio.sleep(0)``, and ``_connect`` overwrites ``self._ws`` with a new
   socket — the old one is never closed. Render logged that refusal about
   twice a second for ten minutes before each kill. Against a local server
   that refuses the same way and keeps the socket open, it reached 1.1 GB in
   60 seconds (17,306 open sockets).
2. **With nothing subscribed it busy-waits.** It polls for a first
   subscription with ``asyncio.sleep(0)``, which on an otherwise empty event
   loop is a spin: one full core, measured at 99%. The dashboard starts its
   stream before the first discovery cycle, and on a 0.15-CPU instance that
   thread competes with every request for the GIL.

``_guarded_stream_class()`` subclasses ``StockDataStream`` and replaces that one
method: it waits for a subscription with a real sleep, closes the socket on
every failure, and backs off exponentially (``movers.stream.
reconnect_backoff_sec`` doubling to ``reconnect_backoff_max_sec``). It keeps
retrying, because the condition clears on its own — when the other client went
away at 16:21 the loop stopped by itself. The state lands in ``status()``
(``connected``, ``last_error``, ``connect_failures``) so the dashboard can say
the stream is refused rather than show it as live. It reaches into alpaca-py's
private members; ``tests/test_price_stream_reconnect.py`` pins the ones it uses,
so an upgrade that renames them fails CI instead of the stream.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Callable, Optional

from src.utils import config
from src.utils.logger import get_logger

log = get_logger(__name__)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


_IDLE_POLL_SEC = 0.5        # how often to look for a first subscription
_PAUSE_STEP_SEC = 0.25      # backoff sleeps in steps so stop() is honoured promptly
_CLOSE_TIMEOUT_SEC = 5.0    # bound on closing a socket the server won't close
_GUARDED_CLS = None


def _guarded_stream_class():
    """``StockDataStream`` with a reconnect loop that cannot run away.

    Built on first use because alpaca-py is an import only streaming needs.
    See the module docstring for the two upstream defects this replaces.
    """
    global _GUARDED_CLS
    if _GUARDED_CLS is not None:
        return _GUARDED_CLS

    import asyncio

    from alpaca.data.live import StockDataStream

    class GuardedStockDataStream(StockDataStream):
        def __init__(self, *args, backoff_sec: float = 2.0, backoff_max_sec: float = 300.0,
                     **kwargs) -> None:
            super().__init__(*args, **kwargs)
            self._backoff_sec = max(0.01, float(backoff_sec))
            self._backoff_max_sec = max(self._backoff_sec, float(backoff_max_sec))
            self._guard = {"connected": False, "failures": 0,
                           "last_error": None, "retry_at": None}

        @property
        def guard_state(self) -> dict:
            return dict(self._guard)

        def _set_guard(self, **kw) -> None:
            self._guard = {**self._guard, **kw}     # swap, never mutate in place

        def _has_subscription(self) -> bool:
            return any(v for k, v in self._handlers.items()
                       if k not in ("cancelErrors", "corrections"))

        async def _discard_socket(self) -> None:
            ws, self._ws = self._ws, None
            self._running = False
            if ws is None:
                return
            try:
                await asyncio.wait_for(ws.close(), _CLOSE_TIMEOUT_SEC)
            except Exception:
                try:
                    ws.transport.abort()
                except Exception:
                    pass

        async def _pause(self, seconds: float) -> None:
            end = time.monotonic() + seconds
            while self._should_run:
                left = end - time.monotonic()
                if left <= 0:
                    return
                await asyncio.sleep(min(_PAUSE_STEP_SEC, left))

        async def _run_forever(self) -> None:
            self._loop = asyncio.get_running_loop()
            while not self._has_subscription():
                if not self._stop_stream_queue.empty():
                    self._stop_stream_queue.get(timeout=1)
                    return
                await asyncio.sleep(_IDLE_POLL_SEC)
            self._should_run = True
            self._running = False
            failures = 0
            while self._should_run:
                try:
                    if not self._running:
                        await self._start_ws()
                        await self._send_subscribe_msg()
                        self._running = True
                        if failures:
                            log.info(f"price stream: connected after {failures} failed attempt(s)")
                        failures = 0
                        self._set_guard(connected=True, failures=0, last_error=None, retry_at=None)
                    await self._consume()
                except Exception as exc:
                    await self._discard_socket()
                    self._set_guard(connected=False)
                    if not self._should_run:
                        break
                    reason = str(exc) or type(exc).__name__
                    if "insufficient subscription" in reason:
                        log.error(f"price stream: {reason} — feed not available on this plan; "
                                  "streaming off, REST polling continues")
                        self._set_guard(last_error=reason[:200], retry_at=None)
                        return
                    failures += 1
                    delay = min(self._backoff_max_sec, self._backoff_sec * 2 ** min(failures - 1, 30))
                    self._set_guard(failures=failures, last_error=reason[:200],
                                    retry_at=time.time() + delay)
                    log.warning(f"price stream: {reason} (attempt {failures}) — socket closed, "
                                f"retrying in {delay:.3g}s")
                    await self._pause(delay)
            self._set_guard(connected=False, retry_at=None)

    _GUARDED_CLS = GuardedStockDataStream
    return _GUARDED_CLS


class PriceStream:
    """Live last-trade prices for a dynamic symbol set (see module docstring).

    Usage (synchronous throughout)::

        s = PriceStream()
        if s.start(["AAPL", "TSLA"]):
            ...                     # ticks arrive on the background thread
            px = s.latest("AAPL")   # {"price": ..., "age_seconds": ...} or None
            s.set_symbols(["AAPL", "NVDA"])   # re-subscribe by diff
            s.stop()

    ``client_factory`` is injectable so the whole lifecycle can be unit-tested
    with a fake stream — no network, no live event loop.
    """

    def __init__(
        self,
        *,
        api_key: Optional[str] = None,
        secret_key: Optional[str] = None,
        feed: Optional[str] = None,
        client_factory: Optional[Callable[..., object]] = None,
    ) -> None:
        self._api_key = api_key if api_key is not None else config.ALPACA_API_KEY
        self._secret_key = secret_key if secret_key is not None else config.ALPACA_SECRET_KEY
        self._feed = (feed or config.MOVERS_STREAM_FEED or "iex").lower()
        self._client_factory = client_factory

        self._lock = threading.Lock()
        self._prices: dict[str, dict] = {}     # symbol -> {price, size, ts, recv}
        self._subscribed: set[str] = set()
        self._client = None
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._started_at: Optional[float] = None
        self._tick_count = 0

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def start(self, symbols: Optional[list[str]] = None) -> bool:
        """Open the stream and subscribe to ``symbols``. Returns True if live.

        No-op (returns False) when disabled, credentials are missing, the SDK is
        unavailable, or anything fails — the caller then just keeps polling.
        """
        if self._running:
            return True
        if not config.MOVERS_STREAM_ENABLED:
            log.debug("price stream: disabled (MOVERS_STREAM_ENABLED off)")
            return False
        if not (self._api_key and self._secret_key):
            log.info("price stream: no Alpaca credentials — streaming unavailable")
            return False
        try:
            client = self._build_client()
        except Exception as exc:  # missing dep / bad creds — never fatal
            log.warning(f"price stream: could not build client ({type(exc).__name__}: {exc})")
            return False

        syms = self._normalize(symbols)
        try:
            if syms:
                client.subscribe_trades(self._on_trade, *syms)
        except Exception as exc:
            log.warning(f"price stream: initial subscribe failed ({type(exc).__name__})")
            return False

        self._client = client
        self._subscribed = set(syms)
        self._running = True
        self._started_at = time.time()
        self._thread = threading.Thread(target=self._run, name="price-stream", daemon=True)
        self._thread.start()
        log.info(f"price stream: started (feed={self._feed}, {len(syms)} symbols)")
        return True

    def _run(self) -> None:
        """Thread body — runs the websocket's blocking event loop until stop."""
        try:
            self._client.run()
        except Exception as exc:  # a socket error must not crash the process
            log.warning(f"price stream: run loop ended ({type(exc).__name__}: {exc})")
        finally:
            self._running = False

    def stop(self) -> None:
        """Close the stream (idempotent, best-effort)."""
        self._running = False
        client = self._client
        if client is not None:
            try:
                client.stop()
            except Exception:  # best-effort teardown
                pass
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=2.0)
        self._client = None
        self._thread = None

    # ── dynamic subscription ─────────────────────────────────────────────────

    def set_symbols(self, symbols: list[str]) -> None:
        """Re-subscribe to exactly ``symbols`` (subscribe/unsubscribe by diff).

        Safe to call from the worker thread while the stream runs; a failure to
        adjust one side is logged and swallowed so monitoring continues.
        """
        if not self._running or self._client is None:
            return
        want = set(self._normalize(symbols))
        add = want - self._subscribed
        remove = self._subscribed - want
        if add:
            try:
                self._client.subscribe_trades(self._on_trade, *sorted(add))
                self._subscribed |= add
            except Exception as exc:
                log.debug(f"price stream: subscribe {sorted(add)} failed ({type(exc).__name__})")
        if remove:
            try:
                self._client.unsubscribe_trades(*sorted(remove))
                self._subscribed -= remove
            except Exception as exc:
                log.debug(f"price stream: unsubscribe {sorted(remove)} failed ({type(exc).__name__})")
            # Drop cached prices for symbols we no longer watch.
            with self._lock:
                for s in remove:
                    self._prices.pop(s, None)

    # ── the async tick handler (the only coroutine in the codebase) ───────────

    async def _on_trade(self, data) -> None:
        """Store the latest trade. Alpaca awaits this on the stream's own loop.

        Kept trivial and non-blocking: extract fields, take the lock briefly,
        write, release. Never raises back into the SDK.
        """
        try:
            symbol = getattr(data, "symbol", None)
            price = getattr(data, "price", None)
            if symbol is None or price is None:
                return
            ts = getattr(data, "timestamp", None)
            rec = {
                "price": float(price),
                "size": getattr(data, "size", None),
                "ts": ts.isoformat() if hasattr(ts, "isoformat") else _utcnow_iso(),
                "recv": time.time(),
            }
            with self._lock:
                self._prices[str(symbol)] = rec
                self._tick_count += 1
        except Exception:  # a bad frame must not kill the stream
            pass

    # ── synchronous read surface ─────────────────────────────────────────────

    def latest(self, symbol: str, now: Optional[float] = None) -> Optional[dict]:
        """Most recent trade for ``symbol`` with a computed ``age_seconds``.

        Returns None if we have never seen a tick for it. ``fresh`` is False once
        the tick is older than ``MOVERS_STREAM_STALE_SEC`` (the tape went quiet).
        """
        now = time.time() if now is None else now
        with self._lock:
            rec = self._prices.get(str(symbol).upper())
            if rec is None:
                return None
            rec = dict(rec)
        age = now - rec["recv"]
        stale_after = float(config.MOVERS_STREAM_STALE_SEC)
        return {
            "price": rec["price"],
            "size": rec["size"],
            "ts": rec["ts"],
            "age_seconds": round(age, 2),
            "fresh": age <= stale_after,
        }

    def snapshot(self, now: Optional[float] = None) -> dict:
        """All cached last-trade prices keyed by symbol (each with age/fresh)."""
        now = time.time() if now is None else now
        with self._lock:
            symbols = list(self._prices.keys())
        return {s: self.latest(s, now=now) for s in symbols}

    def status(self, now: Optional[float] = None) -> dict:
        """Serializable stream status for the worker snapshot / dashboard."""
        now = time.time() if now is None else now
        with self._lock:
            n_prices = len(self._prices)
            ticks = self._tick_count
            subscribed = sorted(self._subscribed)
        # Connection state, when the client reports it (the guarded alpaca
        # client does; injected fakes may not). "live" only says the thread
        # runs — a refused login is live and not connected.
        guard = getattr(self._client, "guard_state", None)
        guard = guard if isinstance(guard, dict) else {}
        retry_at = guard.get("retry_at")
        return {
            "live": bool(self._running),
            "connected": guard.get("connected"),
            "connect_failures": int(guard.get("failures") or 0),
            "last_error": guard.get("last_error"),
            "retry_in_seconds": round(max(0.0, retry_at - now), 1) if retry_at else None,
            "feed": self._feed,
            "subscribed": subscribed,
            "subscribed_count": len(subscribed),
            "priced_count": n_prices,
            "tick_count": ticks,
            "uptime_seconds": round(now - self._started_at, 1) if self._started_at else 0.0,
        }

    def is_live(self) -> bool:
        return bool(self._running)

    # ── internals ────────────────────────────────────────────────────────────

    @staticmethod
    def _normalize(symbols: Optional[list[str]]) -> list[str]:
        if not symbols:
            return []
        cap = max(0, int(config.MOVERS_STREAM_MAX_SYMBOLS))
        seen: list[str] = []
        for s in symbols:
            t = str(s).upper().strip()
            if t and t not in seen:
                seen.append(t)
        return seen[:cap] if cap else seen

    def _build_client(self):
        """Construct the underlying stream (injectable for tests)."""
        if self._client_factory is not None:
            return self._client_factory(self._api_key, self._secret_key, self._feed)
        # Lazy import — alpaca-py is only needed when streaming is enabled.
        from alpaca.data.enums import DataFeed

        feed = DataFeed.SIP if self._feed == "sip" else DataFeed.IEX
        return _guarded_stream_class()(
            self._api_key, self._secret_key, feed=feed,
            backoff_sec=config.MOVERS_STREAM_RECONNECT_BACKOFF_SEC,
            backoff_max_sec=config.MOVERS_STREAM_RECONNECT_BACKOFF_MAX_SEC,
        )
