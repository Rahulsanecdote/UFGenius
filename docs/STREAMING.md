# Real-time Price Streaming (MOVERS Phase 8)

Phases 5–7 react on a **poll** cadence: the always-on worker re-fetches intraday
bars each cycle, so a price move is only noticed at the next REST poll. Phase 8
adds a **push** source — Alpaca's trade websocket — giving the worker a live tape
so it sees a print the instant it happens.

It is **opt-in and fail-open**. Default off; if it can't start (disabled, no
credentials, `alpaca-py` missing, or a socket error) the worker simply keeps
polling exactly as before. Nothing here touches the money path — the stream is a
data source, not a gate.

## How it works

`src/streaming/price_stream.py` → `PriceStream` wraps Alpaca's
`StockDataStream`. The rest of UFGenius is synchronous ("no `async def` without
cause"), so the asyncio websocket is **quarantined**:

- it runs its own event loop inside a **daemon thread** (`client.run()`);
- the async trade handler does only trivial, lock-guarded work (store the last
  price) and never raises back into the SDK;
- the only surface the rest of the app touches is a **synchronous, thread-safe
  snapshot** — `latest(symbol)`, `snapshot()`, `status()`.

Subscriptions are driven by **diff**: the worker calls `set_symbols(...)` with
its current watch set each cycle, and `PriceStream` subscribes the additions /
unsubscribes the removals (both thread-safe mid-run per the SDK). Unsubscribed
symbols have their cached price dropped.

```
worker cycle ──► monitor.active()  ──►  stream.set_symbols([tickers])
                                            │  (Alpaca trade websocket, own thread)
                                            ▼
                          stream.snapshot() / status()
                                            │
                       state.publish(stream_status=…, stream_prices=…)
                                            │  data/movers_worker.json
                                            ▼
                     dashboard  GET /api/movers-worker  ──►  ⚡ Streaming pill
                                                             + live price on chips
```

## Enabling it

Streaming needs `ALPACA_API_KEY` / `ALPACA_SECRET_KEY` (the same paper/live
credentials the broker uses — market-data access, not order access).

```yaml
# config.yaml
movers:
  stream:
    enabled: true       # default false
    feed: iex           # iex (free) | sip (paid, full-market tape)
    stale_sec: 10       # a tick older than this is shown as a quiet tape
    max_symbols: 30     # cap on concurrently-subscribed symbols
```

Or via env: `MOVERS_STREAM_ENABLED=true`, `MOVERS_STREAM_FEED=iex`,
`MOVERS_STREAM_STALE_SEC=10`, `MOVERS_STREAM_MAX_SYMBOLS=30`.

## Verifying

```bash
python bot.py --mode stream                 # stream today's top movers' tape
python bot.py --mode stream --ticker AAPL   # a single symbol
```

Prints each symbol's latest streamed price every few seconds. The **IEX** feed
has no after-hours tape, so run it during market hours to see ticks; a quiet tape
prints "waiting for first ticks".

## Cadence note (important, honest)

The websocket makes the **worker's** view real-time. The **dashboard** is a
separate process and reads the shared JSON snapshot, so it reflects the live
prices at the worker's *publish* cadence (`movers.worker.poll_interval_sec`,
default 60s; floor 15s) and the dashboard's own refresh (20s for the worker
panel). In other words: the tape is captured sub-second by the worker, but the
browser sees it at the state-publish/refresh cadence — not sub-second in the UI.
Truly sub-second *browser* updates would need a push channel (SSE/WebSocket) from
the dashboard, which this phase deliberately does not add.

## Limits / not-yet

- The stream currently feeds **last-trade price** only. Bar-derived signals
  (relative volume, momentum, VWAP) still come from the REST enrichment, so
  invalidation rules are unchanged — Phase 8 surfaces a live price, it does not
  yet re-derive the quality score from the tape.
- One stream lives in the **worker** process. The dashboard does not open its own
  stream (that would duplicate the feed and re-introduce async into the web app).
