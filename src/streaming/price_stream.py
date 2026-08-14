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
        return {
            "live": bool(self._running),
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
        from alpaca.data.live import StockDataStream

        feed = DataFeed.SIP if self._feed == "sip" else DataFeed.IEX
        return StockDataStream(self._api_key, self._secret_key, feed=feed)
