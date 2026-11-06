# Early-momentum alerts

Off by default (`movers.early.enabled: false`, env `EARLY_ALERTS_ENABLED`).
Code: `src/scanner/early_movers.py`. Tests: `tests/test_early_movers.py`.

## Why the movers path is late

| Cause | Where |
|---|---|
| A name only exists once it is on a provider gainers list **and** up `movers.min_change_pct` (3%) | `src/scanner/movers.py` (`fetch_market_movers`, `abs(c.change_pct) < min_change`), `config.yaml movers.min_change_pct` |
| Discovery score is magnitude (`min(85, |chg|*2.5)`); the enriched score still gives up to 28 pts for the move, vs a 70 floor | `movers.py` `_score`, `_enriched_score`; `movers.alerts.min_score` |
| Rediscovery every 8 cycles (~8 min) to stay inside FMP's 250/day | `movers_worker.py` rediscovery block, `movers.worker.rediscover_every_cycles` |
| Enrichment on 5m bars; precursor needs 28 bars (140 min on 5m) | `movers.enrich_interval`, `config.yaml precursor:` |
| Stream carried trades only, for already-watched names; never triggered anything | `src/streaming/price_stream.py`, `movers_worker.py` |
| `src/signals/precursor.py` was only wired into the backtest | `src/backtest/intraday_engine.py` |

## The rule (`evaluate_early`)

On 1m bars of **today's regular session**, fire (long only) when all hold:

* `min_change_pct <= change <= max_change_pct` — small, not extended;
* `rvol_accel = mean(vol, last accel_bars) / mean(vol, prior base_bars) >= min_rvol_accel`;
* last close within `press_pct` of a level — session high, opening-range high
  (`opening_range_bars`), **premarket high**, **prior-day high** — or the
  precursor coil is present.

Prior-day high/close come from the last *completed* daily bar; the premarket
high from today's pre-09:30 ET 1m bars when the feed returned them.

## Three paths

1. **Gainers-list enrichment** — `movers._enrich_candidate` attaches `c.early`
   (with prior-day/premarket highs). Lowers the discovery floor to
   `early.min_change_pct` while enabled.
2. **Fixed watchlist** — `WatchlistScanner`, evaluated every
   `watchlist_every_cycles` worker cycles, independent of the gainers lists and
   rediscovery. One `fetch_ohlcv_batch(..., "1m")` per cycle (Alpaca
   multi-symbol endpoint, ~100 symbols/request); daily context once per day.
   Size capped by `watchlist_max`.
3. **Stream bars** — with `movers.stream.enabled` and `early.stream_bars`, the
   worker subscribes 1m bars (`subscribe_bars`) for watchlist + watched names
   on the **same** websocket and guarded reconnect loop. Bars queue on the
   stream thread; the worker drains them each cycle into a rolling per-symbol
   `BarBuffer` (seeded from the REST scan) and evaluates on each bar close.
   Latency is bounded by `worker.poll_interval_sec` (60s ≈ one bar).

All hits go through `MoversAlerter.process`: they skip **only** the
`min_score` floor; `alerts.enabled`, halt suppression, `require_fresh_session`,
dedup and `max_per_run` still apply. Alerts carry an `EARLY:` reason line.

## Config (`movers.early`)

| Key | Default | Env |
|---|---|---|
| enabled | false | EARLY_ALERTS_ENABLED |
| interval | 1m | EARLY_INTERVAL |
| min_change_pct / max_change_pct | 1.0 / 8.0 | EARLY_MIN_CHANGE_PCT / EARLY_MAX_CHANGE_PCT |
| accel_bars / base_bars | 3 / 20 | EARLY_ACCEL_BARS / EARLY_BASE_BARS |
| min_rvol_accel | 2.5 | EARLY_MIN_RVOL_ACCEL |
| press_pct | 0.5 | EARLY_PRESS_PCT |
| opening_range_bars | 5 | EARLY_OPENING_RANGE_BARS |
| watchlist / watchlist_file | [] / "" | EARLY_WATCHLIST (comma list) / EARLY_WATCHLIST_FILE |
| watchlist_max | 100 | EARLY_WATCHLIST_MAX |
| watchlist_every_cycles | 1 | EARLY_WATCHLIST_EVERY_CYCLES |
| stream_bars | true | EARLY_STREAM_BARS |
| buffer_bars | 120 | EARLY_BUFFER_BARS |

## Data-plan limits

* **IEX vs SIP.** Free Alpaca intraday bars and the stream are IEX-only — one
  venue, a few % of volume, very thin on small caps/sub-$1. Volume
  acceleration is noisy there and many names have no bars. Real-time SIP needs
  a paid plan (`movers.stream.feed: sip`, `ALPACA_DATA_FEED=sip`).
* **One websocket per account** on the free plan. Bars ride the existing
  socket; trades + bars share `stream.max_symbols`.
* **REST limits.** 200 req/min on Alpaca free; the watchlist costs ~1 request
  per 100 symbols per cycle (plus pagination). Path 1 adds a 1m and a daily
  fetch per enriched candidate (daily is cached).
* **FMP quota (250/day)** still bounds gainers-list rediscovery; the watchlist
  and stream paths do not use FMP.

Not prediction: most pressure-at-a-level fails. Judge it on the alert outcome
ledger (`source="early"`), not on hit counts.
