# CLAUDE.md — UFGenius AI Assistant Guide

## Project Overview

**UFGenius** is an autonomous **stock signal bot**. It scans an equity universe,
scores each ticker across technical / volume / sentiment / fundamental / macro
dimensions, emits BUY/SELL/HOLD signals with risk-aware trade plans, and can
place risk-gated orders through Alpaca. It ships a Flask web dashboard and a CLI.

> ⚠️ Educational use only. **Not financial advice.** Paper-trade before risking real money.

**Stack:** Python 3.12, Flask + gunicorn (dashboard/API), yfinance + optional
providers (Alpha Vantage / Polygon / Finnhub) for data, `alpaca-py` for the
broker, pandas/NumPy for computation, VADER + PRAW + NewsAPI for sentiment,
`fredapi` for macro. There is **no** React frontend, no FastAPI, and no
application database — the dashboard is a single self-contained HTML page
rendered via `render_template_string`, and state is file-/JSON-based. (A small
SQLite file backs only the dashboard rate limiter; there is no relational
data model.)

---

## Repository Structure

```
UFGenius/
├── bot.py                     ← CLI entry point (scan / paper / live / backtest / portfolio)
├── dashboard.py               ← Flask app: HTML dashboard + JSON API
├── wsgi.py / Procfile         ← gunicorn entry (wsgi:app) for production hosts
├── diagnose.py                ← pipeline diagnostics
├── config.yaml                ← strategy / risk / schedule config (non-secret)
├── .env(.example)             ← API keys + runtime tuning (secret; gitignored)
├── requirements.txt / .lock / constraints.txt
├── render.yaml                ← Render deployment (gunicorn + dashboard hardening)
├── src/
│   ├── core/          ← typed models (Instrument, TickerSnapshot, …) + provider contracts
│   ├── data/          ← OHLCV/universe fetch (retry/cache); providers/ adapters + registry
│   ├── features/      ← feature registry/store + weighting policies (Phase 3)
│   ├── technical/     ← trend, momentum, volatility, volume, support/resistance
│   ├── fundamental/   ← fundamentals fetch + scoring (Piotroski, Altman Z, valuation)
│   ├── sentiment/     ← news / social (Reddit) / insider sentiment
│   ├── macro/         ← market-regime detection (VIX, breadth, FRED)
│   ├── signals/       ← generator (composite score → signal), filters, context, trade_plan
│   ├── scanner/       ← daily_scan (universe orchestration) + gap_scanner
│   ├── screener/      ← named pre-trade filter presets (oversold-bounce/ma-bounce/breakout)
│   ├── alerts/        ← telegram / email notifications
│   ├── backtest/      ← portfolio backtest engine (daily MTM, commission + slippage)
│   ├── alpaca/        ← portfolio (read-only), orders, executor (+RiskGuard), position_tracker
│   ├── portfolio/     ← volatility/correlation-aware position sizing (advisory, Phase 4)
│   ├── risk/          ← portfolio-level risk engine (leverage/heat/cluster/drawdown; advisory, Phase 4)
│   └── utils/         ← config, logging, HTTP retry/session, dashboard security
└── tests/             ← pytest suite (unit + `integration`-marked network tests)
```

---

## Development Workflows

### Setup

```bash
python -m venv .venv && source .venv/bin/activate
python -m pip install -r requirements.lock       # reproducible install
# or: pip install -r requirements.txt -c constraints.txt
cp .env.example .env                             # fill in keys you need
```

Only `ALPACA_API_KEY`/`ALPACA_SECRET_KEY` are needed for portfolio/execution;
data works on yfinance alone. `ALPACA_PAPER=true` (default) keeps you on paper.

### CLI (`bot.py`)

```bash
python bot.py --mode scan                       # one full-universe scan
python bot.py --mode scan --ticker AAPL         # single ticker
python bot.py --mode screen --preset oversold-bounce  # filter a universe by a named screener preset → watchlist
python bot.py --mode paper                      # scheduled scans, log only
python bot.py --mode live                        # scheduled scans + alerts
python bot.py --mode live --execute             # + submit orders to the PAPER account
python bot.py --mode live --live-execute        # + REAL-MONEY orders (needs ALPACA_PAPER=false)
python bot.py --mode live --execute --dry-run   # preview orders, submit nothing
python bot.py --mode backtest --start 2022-01-01 --end 2023-12-31
python bot.py --mode intraday-backtest --entry breakout --interval 5m  # OOS check for the intraday entries (breakout / sweep_reclaim)
python bot.py --mode forecast-coverage --ticker AAPL --horizon 5  # is a forecast INTERVAL calibrated, and worth more than trailing vol?
python bot.py --mode validate --start 2022-01-01 --end 2023-12-31  # walk-forward + OOS + bootstrap edge check (P0.1)
python bot.py --mode validate --save-baseline   # + persist the OOS metrics as the paper-vs-backtest reference
python bot.py --mode optimize --start 2022-01-01 --end 2023-12-31  # in-sample grid search + overfitting haircut + OOS confirm (P0.2)
python bot.py --mode portfolio                  # read-only Alpaca portfolio
python bot.py --mode intraday-scan              # continuous intraday candidate scan → queue (P1.2)
python bot.py --mode earnings-calendar          # build/refresh the earnings calendar (P1.4)
python bot.py --mode movers-worker              # always-on movers worker (MOVERS Phase 5)
python bot.py --mode premarket-scan             # pre-market gap screener → ranked research watchlist (--penny: gates from the penny rails)
python bot.py --mode stream                     # live Alpaca trade-stream diagnostic (MOVERS Phase 8)
python bot.py --mode alert-test                 # verify Telegram delivery with THIS process's credentials
```

Execution safety: `--execute` targets the **paper** account and refuses to run
if `ALPACA_PAPER=false`; `--live-execute` is the only real-money path and
refuses unless `ALPACA_PAPER=false`. Every order passes `RiskGuard`
(`src/alpaca/executor.py`), which enforces the subset of `config.yaml`
`safety_rules` listed under Architecture & Conventions.

### Dashboard

```bash
python dashboard.py                     # local: http://127.0.0.1:5001
gunicorn --bind 0.0.0.0:$PORT wsgi:app  # production (see Procfile / render.yaml)
```

**Security:** when the app is network-exposed (`DASHBOARD_ALLOW_REMOTE=true` or
a `PORT` env var is present) it **requires** `DASHBOARD_API_KEY`/
`DASHBOARD_API_KEYS` and fails closed at startup without one. Send the key as
`X-API-Key: <key>` or `Authorization: Bearer <key>`. A rate limiter runs before
auth; behind a trusted proxy set `DASHBOARD_TRUST_PROXY=true`.

### Tests

Unit tests run with **every credential blanked** (`tests/conftest.py`,
`_no_ambient_credentials`), whatever your shell exports — so a unit test can
never reach a live provider, broker or Telegram chat by accident, and results
match CI's keyless environment. A test that needs a key sets its own.
`tests/test_credential_isolation.py` fails if a credential is added to
`config.py` without being added to that list. `integration`-marked tests are
exempt.

```bash
pytest                 # unit tests (integration/network tests excluded by default)
pytest -m integration  # opt into the network-hitting tests
pytest --cov=src       # coverage
```

---

## Architecture & Conventions

- **Config is centralized** in `src/utils/config.py` — it reads `config.yaml`
  (non-secret strategy/risk knobs) and `.env` (secrets + tuning). Never hardcode
  thresholds; add a `config.X` accessor and a `config.yaml`/env key.
- **Data flow:** `scanner/daily_scan` → `signals/context` (one fetch per ticker
  via `data/providers`) → `technical`/`fundamental`/`sentiment`/`macro` scores →
  `signals/generator.generate_signal` (weighted composite → label via
  `SIGNAL_THRESHOLDS`) → `signals/filters` (hard disqualifiers) →
  `signals/trade_plan` (entry/stop/targets/sizing) → alerts / executor.
- **Money paths are gated:** sizing never forces a share it can't afford
  (returns a `skip` plan); `RiskGuard` (`src/alpaca/executor.py`) blocks entries
  that breach `safety_rules` — max positions, single-position cap, cash reserve,
  per-trade risk, daily trade count, bear-market, duplicate-ticker,
  `stop_loss_required`, daily/weekly realized-loss limits, post-loss cooldown,
  earnings-week (P1.4 **calendar-backed** via `src/catalysts/earnings_calendar.py`,
  yfinance fallback), the **P1.4 catalyst-tag veto** (`src/catalysts/catalyst_gate.py`
  — blocks entries whose `catalyst_tags` hit `catalysts.veto_tags`),
  `paper_trade_days_required` (live only — **P0.4** upgraded this from a tenure
  check to tenure **plus** a paper-scorecard performance gate: the realized
  paper trades must clear configured floors before real money; a second,
  **opt-in** half then requires those same paper metrics to stay **within
  tolerance of the validated out-of-sample backtest** —
  `src/backtest/baseline.py`, config `paper_scorecard.baseline_*`, reference
  saved by `--mode validate --save-baseline`. One-sided, so only paper
  *underperformance* blocks; fail-closed on a missing/stale/unvalidated
  baseline. Each half is separately enabled and the gate passes only if every
  enabled half passes), and the **P0.3 circuit breakers**
  (global operator halt, broker-error breaker, data-staleness breaker — checked
  first, block new entries only). Realized-loss limits and the cooldown read a
  realized-P&L ledger the monitor writes at each exit; the position tracker also
  writes a per-**trade** outcome ledger (P0.4) that `src/alpaca/scorecard.py`
  turns into backtest-comparable metrics for the live performance gate; the
  circuit-breaker state (halt flag + broker-error trail) is a JSON file shared
  between the dashboard and CLI (`src/alpaca/circuit_breaker.py`, config
  `circuit_breakers:`). Every fill's expected-vs-realized price is recorded to the
  P2.1 execution-quality ledger (`src/alpaca/execution_quality.py`) as adverse
  slippage + implementation shortfall; with `execution_quality:
  use_measured_slippage` the backtest cost model uses that **measured** slippage.
  P2.2 smart order handling (`src/alpaca/smart_orders.py`, config `smart_orders:`,
  default off) prices the entry as a marketable limit crossing the market by an
  offset tuned to that measured slippage. Live trading needs an explicit flag +
  `ALPACA_PAPER=false`.
- **Exits are one OCO per tranche** (`orders.place_oco_exit`, executor
  `_finalize_entry_fill` / `_check_exits_oco`): once the monitor sees the entry
  fill, each of T1/T2/T3 gets its own GTC OCO — take-profit limit (the parent)
  plus a stop leg at the plan's stop — sized to that tranche, so together they
  cover exactly the filled shares. It used to place a full-size stop and then
  three target limit sells, which **cannot work on Alpaca**: an open sell order
  reserves its shares, and a second sell for them is refused with HTTP 403
  "insufficient qty available for order" (Alpaca's own error guide, #26). Every
  target would have been rejected and a trade could only ever exit at its stop,
  so the paper scorecard would have measured a stop-only strategy the backtest
  never modelled. With OCOs a target fill leaves the remaining tranches guarded
  by their own stop legs — nothing to resize, no unprotected window. Records
  opened before the change keep the legacy path (`exit_mode` "").
  **Order status is an enum** — `orders.order_status()` reads `.value`. alpaca-py
  returns `OrderStatus`, a `(str, Enum)`, and `str()` of it is
  `"OrderStatus.FILLED"` on 3.11–3.13, so the monitor's
  `str(order.status).lower() == "filled"` could **never** match a real broker
  response: a filled entry stayed `pending_fill`, no stop was ever placed, and an
  expired entry was never cleaned up (so it held a `max_positions` slot
  forever). Both bugs were invisible to the suite because every mock carried
  plain strings and no mock modelled share reservation, and invisible in
  practice because the paper account had never placed an order.
  `tests/test_broker_contract.py` runs the lifecycle against a fake that returns
  real alpaca-py `Order` objects and enforces the reservation rule; putting the
  old comparison back fails 13 of its tests. Still unverified against the live
  paper broker. **Residual gap:** protection exists only once the monitor
  (every `MONITOR_INTERVAL_MIN`, market hours) has seen the fill, so a dead
  monitor thread still means an unprotected position.
- **Scheduled scans run in New York time** (`bot._SCHEDULE_TZ`): `schedule:`
  slots are market wall-clock times, but the `schedule` library reads them as
  host-local time, and Render runs UTC — every slot fired four hours early
  under EDT (the 09:25 open scan at 05:25 ET). Needs `pytz`, now pinned. Two
  properties of `_schedule_scan` to know before running it unattended: it
  **scans once immediately at startup** (so a redeploy triggers a scan at
  whatever time it lands), and the 11:00/14:00 slots rarely pass anything,
  because the pre-filter's `RVOL >= 1.3` is computed on today's **partial**
  daily bar — measured 10:35 ET 2026-09-30: AAPL 0.38, MSFT 0.46, JPM 0.09,
  XOM 0.15, and 0 of 503 passed.
- **Intraday data (P1.1):** `fetch_intraday()` (`src/data/fetcher.py`) is the
  entry point for 1m/5m/… bars — same provider abstraction as daily, but with a
  **boundary-aligned** intraday cache TTL (`intraday.cache_boundary_align`,
  default on — the cache expires just after the next bar boundary + a settle
  grace, so a just-closed bar is picked up on the next poll; `_ttl_for_interval`)
  and the look-ahead guards in `src/data/lookahead.py`
  (order/dedupe, drop future-labelled bars, `as_of` clamp, stale-frame check).
  **The returned index keeps the provider's timezone** — the guards convert a
  local copy of the index for their own comparison and hand the frame back with
  the original index, so yfinance's tz-aware `America/New_York` survives. That
  is deliberate (`intraday_features.current_session_bars` takes `.date()` off
  this index, so forcing UTC would re-bucket the extended session) but it means
  a caller must **convert** with `lookahead._as_naive`, never strip the tz:
  `replace(tzinfo=None)` keeps the wall clock and silently shifts an ET bar by
  the UTC offset. `movers._last_bar_time` did exactly that, storing an 08:41 ET
  bar as 08:41 "UTC" — four hours early. `_same_trading_day` compares ET
  calendar dates, and under EDT a four-hour backward shift still lands on the
  right day, so `require_fresh_session` was correct *by coincidence*; under EST
  a 04:00–04:59 ET pre-market bar moves onto the previous ET date and a live
  candidate is suppressed as `stale_session_data`.
  Use it (not `fetch_ohlcv`) for anything real-time; daily bars still use
  `fetch_ohlcv`. Knobs live under `config.yaml` `intraday:` / `INTRADAY_*`.
- **Pre-market screener:** `--mode premarket-scan` (`src/scanner/premarket_scan.py`,
  config `premarket:`) ranks extended-hours gappers by evidence-backed factors
  (time-of-day RVOL — rewarded only above a liquidity floor, its sign is
  conditional; banded gap score that penalises extremes; PM dollar volume;
  float rotation; catalyst = earnings calendar + a keyword-classified news
  feed (`src/catalysts/news_feed.py`: Alpaca News → yfinance → NewsAPI, tiers
  strong/moderate/weak/dilution, fail-soft)) and tags candidates
  continuation/fade_risk/neutral.
  **Publication dates are checked at classification, not only at fetch.**
  `classify_headlines()` read `h.title` and nothing else, which left three
  holes: an **undated** headline earned full catalyst credit (every fetcher's
  cutoff reads `published is not None and published < since`, so `None` sails
  past a window it was never measured against); within a tier the *first*
  headline in list order won rather than the newest, and provider order is no
  recency guarantee; and the winning headline's age was never returned, so the
  alert formatter printed "just now" for an undated headline — asserting the
  one fact an alert premised on *"published moments ago"* must not invent.
  It now takes `now`/`max_age_hours`/`allow_undated`, skips stale and undated
  headlines (counted in `skipped_stale`/`skipped_undated`, never silently),
  prefers the newest match as the receipt, treats a future-dated timestamp as
  broken rather than fresh, and returns `published`/`age_hours` so callers can
  disclose it (`catalyst_age_hours` on the snapshot and in the API row; the
  alert line carries "(16h ago)" past an hour). Config `premarket.news.
  allow_undated` (default false). **Not** solved by this: a story republished
  today about an old event — SRZN on 2026-09-24 carried a 2026-09-08 IND
  submission the stock had already fallen 2% on, and the republication's date
  was genuinely today. The event date lives in the body text, which this module
  never fetches; catching it needs event-date extraction or first-seen tracking
  across polls.
  **A headline only classifies a security it actually names.** The wire attaches
  a story to every ticker its BODY mentions, so a market wrap arrives tagged
  with a dozen symbols while its headline concerns one of them at most: "Crude
  Oil Rises Over 4%; Darden Earnings Miss Views" reached SRZN on 2026-09-24 and
  scored `moderate` off *Darden's* earnings. Two gates, because the two paths
  hold different data. The per-symbol path (`catalyst_news_for` → screener)
  knows the ticker AND the company name, so `headline_concerns()` tests the
  subject directly — the ticker as a standalone case-sensitive token, the full
  company name, or its distinctive leading word (`_company_core`: "Surrozen"
  for "Surrozen Inc", since the title omits the suffix; tokens under four chars
  are discarded as ordinary English). The batch/firehose path
  (`catalyst_alerts`) carries symbols only, where that test is unusable —
  "Surrozen Files IND" contains no "SRZN" — so it gates on symbol COUNT
  instead (`movers.catalyst_alerts.max_story_symbols`, default 6, 0 disables):
  a story the wire attached to more tickers than that is a roundup. Both are
  opt-in per call and counted in `skipped_offtopic`. `_newsapi_identity_ok` now
  delegates to the same test, which fixes a false negative it carried (it
  required the FULL company name as a substring). Residual: a wrap naming
  exactly the cap or fewer still passes the count gate — "Dow Tumbles Over 100
  Points" carried 6 and cleared it, scoring `none` only because its headline
  matched no tier pattern. Config `premarket.news.require_subject` (default
  true). **Screener only** — firewalled from the
  money path, no filter loosened; `fetch_ohlcv/fetch_intraday(prepost=True)`
  supplies the 4:00–9:30 ET bars (yfinance flag; Alpaca/Polygon already span
  the extended session; cache keys get an `:ext` suffix). Free Finviz cannot
  see pre-market (Elite-only) — the finviz provider is prior-day context only.
  Thresholds/weights in `config.yaml` `premarket:` with cited provenance;
  see `docs/PREMARKET_SCREENER.md`.
- **Pre-market movers discovery** (`src/scanner/premarket_movers.py`, config
  `premarket_movers:`, universe `PREMARKET`): the universe the screener above
  needs. The regular-session movers chain reports the **previous** session
  before 09:30 (observed 2026-08-18 09:02 ET: the movers list carried the prior
  day's closing prices and 44 of 50 names had no extended-hours print), so
  `MOVERS` + the pre-market screener never overlap usefully — during the window
  the screener needs, that universe is stale; when it refreshes, the window has
  closed. Same `list`-vs-`None` contract as the movers chain. What is new here
  is that **coverage is a disclosed property**: `polygon` is `market_wide` (full
  snapshot — a fresh 08:00 gapper is visible), `yahoo` is `bounded_pool` (ranks
  the prior session's lists; keyless, so it always exists, and structurally
  blind to a name that was quiet yesterday). Falling back changes what the list
  *could* contain, so the serving provider and its coverage class ride along in
  `universe_discovery` (API), the dashboard status line, and the CLI header —
  and an empty result outside 04:00–09:30 ET says the session is closed rather
  than reading as a quiet tape. Discovery only; the screener it feeds is itself
  firewalled from the money path.
- **Intraday scan → entry pipeline (P1.2/P1.3):** `--mode intraday-scan` runs a
  `ContinuousScanner` (`src/scanner/intraday_scan.py`) that scores live intraday
  bars for volume/momentum/breakout/gap and pushes deduped hits into a
  `CandidateQueue` (`candidate_queue.py`); an `IntradayConsumer`
  (`intraday_consumer.py`) drains it and runs the deterministic intraday entry
  evaluator (`src/signals/intraday_signal.py`: VWAP + opening-range breakout +
  volume, intraday-ATR stop via `src/technical/intraday_features.py`). Discovery
  + planning only — plans go to a pluggable sink (default log); execution reuses
  the gated `execute_trade_plan` path. Config: `continuous_scan:` / `intraday_signal:`
  (default **1m bars polled every 15s** — the `_MIN_INTERVAL_SEC` loop floor; with
  the boundary-aligned intraday cache the cache expires just after each bar closes,
  so reaction to a newly-closed bar tracks the ~15s poll cadence, one provider call
  per bar — still not sub-minute *resolution* (1m is the provider floor and the
  forming bar is dropped by the look-ahead guard); `dedup_ttl_sec` tracks ~1 bar so re-scored cached polls
  are suppressed while the next bar can still re-qualify).
  An **opt-in sweep-reclaim reversal** entry (`src/signals/sweep_reclaim.py`,
  config `sweep_reclaim:`, **default off**) is the counterpart to the breakout:
  it detects a sweep of a recent swing low (`lookback_bars`=15) + a reclaim close
  on volume (`reclaim_window_bars`=2), stops just below the swept wick, and builds
  the plan through the same `generate_trade_plan` money-path. Opt-in extensions
  (both default off): `level_anchors: [pdl, pml]` treats the previous-day /
  pre-market low as additional sweepable levels (highest swept-and-reclaimed
  level wins, `level_source` disclosed), and `entry_window_start/_end` restricts
  entries to an ET wall-clock window — A/B them via `--mode intraday-backtest`. When
  `SWEEP_RECLAIM_ENABLED`, the producer enqueues a structural `sweep` candidate
  (`sweep_reclaim_present`, a superset of graded entries) and the consumer runs
  the full grading only when the breakout doesn't fire; both are behind the flag,
  so off ⇒ intraday path unchanged. Timing hypothesis — `--mode validate` covers
  the daily composite only, so the intraday entries are checked out-of-sample by
  the separate **`--mode intraday-backtest`** harness
  (`src/backtest/intraday_engine.py`); still judge it on paper via
  `/api/paper-scorecard` + `/api/attribution` before real money. See
  `docs/SWEEP_RECLAIM.md`.
- **Pre-expansion detection (spike precursor)** (`src/signals/precursor.py`,
  config `precursor:`, **default off**): the third angle on the movers path
  being structurally late. Discovery reacts to magnitude *after* a name reaches
  a gainers list; the catalyst wire attacks that from the news side; this
  attacks it from **price structure** — the state a tape is in *before* the
  vertical part of a move. Four features, one or two parameters each: range
  **compression** (coil ATR vs a non-overlapping baseline ATR), **volume
  dry-up** during the coil (what separates a coil from a tape that merely went
  quiet), an **expansion trigger** (the newest bar clearing the coil high with
  both range and volume behind it), and **upper-range position + VWAP** (so the
  resolution being looked for is upward). Three non-overlapping windows —
  trigger / coil / baseline — because a coil window containing the trigger
  looks widest exactly when it fires, and a baseline containing the coil is a
  reference dragged toward the thing it measures. The stop hint is the **coil
  low**, an absolute level: back inside the range and the premise is false.
  **Not prediction** — contraction precedes expansion only in the weak sense
  that expansion has to come out of something; it says nothing about direction,
  and most coils resolve into noise. A coil alone is a **watch** state and
  never an entry, for the same reason. Deliberately *not* a candlestick-pattern
  library: individual candle shapes on 1m/5m microcap bars are mostly
  microstructure, and "every pattern × every parameter" is the search space
  that produces something beautiful in-sample and worthless out of it — the
  thing `--mode optimize`'s overfitting haircut and `candidate_ranking: rotate`
  exist to fight.
  **The binding constraint is warm-up, not any threshold.** `warmup_bars()` is
  coil + baseline + 2 = 28 at the defaults: 28 minutes on 1m bars, **140 on
  5m**. Replaying MSGY (2026-09-25, $2.13 → $5.95 between 09:30 and 11:07) at
  5m produced *nothing*, because the detector had no history to have an opinion
  with until after the move ended. **It is a 1-minute tool**; on 5m it is quiet
  through exactly the window morning momentum happens in.
  **Cost floor** (`precursor.min_risk_cost_multiple`, default 2.0, 0 disables):
  the stop must sit at least that multiple of modelled round-trip friction away
  from price. The coil low is close to price *by construction*, so on a
  high-priced name the entire risk unit can be smaller than the cost of getting
  in and out — losing arithmetic before the signal has any say. Measured
  2026-09-26 on 50 S&P names (1m, 09-21→25) *without* the floor: 127 trades,
  win rate 18.9%, **profit factor 0.06**, average loss **−2.83R**. Pulling one
  trade apart: $0.625/share of risk on a $339 stock (0.184% of price) against
  $1.356 of modelled round-trip cost (0.400%) — friction **2.17× the whole risk
  unit**, so a perfect entry stopping exactly at its stop still loses ~2R and a
  2R target nets nothing. At multiple N a loss costs ~(1 + 1/N)R and a 2R
  target nets ~(2 − 1/N)R, so N=2 is ~1.5R against 1.5R. Derived from
  `backtest_commission_pct` + `backtest_slippage_pct`, **not fitted to
  returns** — the distinction that separates it from the curve-fitting this
  module exists to avoid. It lives in the evaluator rather than the harness so
  the backtest and the live path cannot disagree about what a trade is.
  A/B on identical cached bars (15 frames, 5,619 bars, floor off then on)
  refused **10 of 10** entries, and shows why: the risk/cost ratio has a median
  of **0.20×** and **99.6% of bars sit below the 2× floor**. On 1m mega-cap
  bars the coil-low stop is routinely a *fifth* of the round-trip cost. The
  floor therefore makes the detector arithmetically inapplicable to that
  regime rather than blocking it by a hardcoded price filter — and it adapts:
  give it a cost model that matches the instrument and the same setups pass.
  Which is the other half of the lesson, since 0.4% round-trip is far too
  punitive for a name whose real spread is a basis point or two. The guard is
  only ever as right as the cost model it reads.
  **That cost model has now been measured** (`docs/COST_MODEL.md`, raw sample
  `docs/spread_sample_2026-09-28.json`: 29 quotes, 2026-09-28 09:52 ET). It is
  wrong in **both** directions. Above $500M/day dollar volume the median round
  trip is **0.0286%**, so 0.400% is 1.7–68× too punitive (AAPL: 0.0059%, i.e.
  68×). Below $50M/day the median is **1.6155%** and the worst 3.9216% (CHRN),
  so the same number is up to 10× too *generous* — a microcap backtest run on it
  reads as free money. Alpaca is commission-free, so the model's 0.100%/side
  commission leg is fictional outright and is half of it.
  **Dollar volume ranks the spread at −0.697; price alone at +0.079, i.e.
  noise** — so a price-tiered cost model would be worthless, and penny mode's
  belief that price × volume is the real liquidity gate is the one that holds.
  Residuals have a cause no cost model can see: ADRX had $40M/day and a 3.00%
  spread because it listed four days earlier; CHRN 0.9% of its shares floated.
  **Correcting the cost does not rescue the precursor, and which way it lands is
  not determined by the sample**: rescaling the A/B's measured 0.20× median
  risk/cost gives **0.32× (still refused)** at the liquid band's worst observed
  cost and **2.80× (clears)** at its median — an 8.3× swing on one name, LLY,
  whose 0.2385% sits 40× above AAPL's in the *same* band. The lesson is not the
  tier's value; it is that a tier exists at all. Cost has to be **per-symbol** —
  a live quote read on the live path, and on the backtest path a spread
  *estimator* from bars (Corwin–Schultz / Abdi–Ranaldo), which needs calibrating
  against measured quotes first or it reintroduces exactly the
  evaluator-vs-harness disagreement the floor's placement exists to prevent.
  `commission_pct`/`slippage_pct` are therefore **unchanged** for now: lowering
  modelled cost flatters every backtest, which is the one direction a system
  with no demonstrated edge must not drift by accident. Unmeasured still: the
  **$50M–$500M/day band has no observations**, where most of the S&P 500 lives;
  and market impact, of which the quoted spread is only the floor.
  That run is **not a fair test of the idea**, and both reasons are worth
  keeping: S&P mega-caps are the wrong regime (a 6-bar 1m coil on a $339 stock
  spans 0.18% of price; on a $3 microcap the same structure spans several
  percent), and the default cost model charges an illiquid-name spread on a
  name whose real round trip is a basis point or two.
  **The microcap re-run has now been done, and it settles the question: no
  edge** (`docs/precursor_microcap_2026-09-30.json`). 50 names drawn at random
  from the 815 listed common stocks priced $1–12 with ≥ $3M/day consolidated
  dollar volume **at the 2026-09-18 close** — before the window opens, so not
  selected on having moved — run on 1m **SIP** bars over seven sessions
  (09-21 → 09-29). The regime half of the hypothesis held: the median coil-low
  stop is 0.464% of price (vs 0.184% on the $339 name) and the average loss is
  −1.2R (vs −2.83R). That changes how fast it loses, not whether. **Gross
  expectancy before any friction is −0.041R, 95% CI [−0.253, +0.175]R**
  (bootstrap over 123 ticker-day clusters, since same-name-same-day trades
  share a tape), with P(> 0) = 34.8% and gross PF 0.94. And even the **top** of
  that interval nets **−3.30R per trade** at this cohort's measured friction —
  1.6155% round trip is 3.48R at a 0.464% stop. Every point of the cost sweep
  is negative, including 0.1% round trip (188 trades, −0.30R). The same 188
  entries re-priced from 0.1% to 1.6155% go from −0.30R to −4.24R, which is the
  whole story: the coil-low stop is structurally close to price, so the thinner
  the book, the larger a share of the risk unit the spread consumes — exactly
  the names where the coil is widest. There is no cost level at which this
  cohort's stops and its real spreads coexist profitably.
  **Run on SIP, never IEX, and that caveat is load-bearing.** Alpaca's default
  `iex` feed emits no bar for a minute with no IEX print, so thin names arrive
  mostly missing (SRFM: 98 of 390 regular-session minutes on IEX, 389 on SIP;
  FLX: 18 vs 139). The precursor counts in *bars*, so on IEX its 28-bar
  warm-up spans hours and its "volume dry-up" would describe IEX's sampling
  rather than the stock. Limits: one seven-session regime (late September),
  and 27% of trades in six names.
  Registered as a third entry in the intraday backtest
  (`--mode intraday-backtest --entry precursor --interval 1m`) so it is
  measurable out-of-sample *before* it is allowed to alert anywhere.
- **Intraday backtest harness (`--mode intraday-backtest`):** the out-of-sample
  check for the intraday entries (`src/backtest/intraday_engine.py`,
  `backtest_intraday`, config `intraday_backtest:`). Replays the breakout /
  sweep-reclaim evaluator bar-by-bar with **no look-ahead** (bar-T signal → bar
  T+1 **same-session** open fill), manages the position **intrabar** (stop-first,
  then T1/T2/T3 partials by the bar's high, same geometry as the live plan), and
  forces **flat at session end** (no overnight holds). Reuses the daily cost
  model; metrics are trade-based (expectancy in R, profit factor, win rate) plus
  a fixed-fractional-risk equity curve, and the acceptance check refuses a
  sub-`min_trades` sample. Honest about its biases (`bias_disclosures`:
  intrabar-ordering, no-concurrency, short/provider-dependent data). Necessary,
  not sufficient — still paper-trade after. See `docs/INTRADAY_BACKTEST.md`.
- **Finviz provider** (`src/data/providers/finviz.py`, config `finviz:`, **default
  off**): a supplementary **fundamentals snapshot + screener** source. Finviz has
  no free API, so it parses public HTML — tables are located by **content** (as
  audit M10 required of `universe.py`), requests are serialised behind a minimum
  interval and disk-cached, and every entry point **fails soft** (`None`/`[]`) so
  a restyle degrades to "no data". `fetch_fundamentals()` maps only fields Finviz
  states directly; values it doesn't publish (absolute debt, cash-flow lines) are
  left absent rather than derived, since `fundamental/scorer.py` already
  normalises over measurable criteria. It is **backfill only** in
  `fundamental/fetcher.py` — never overwrites a primary-source value — and is
  firewalled from the money path. `screen()` passes Finviz's own filter string
  through untouched. Note their terms restrict automated access; enabling it is
  deliberately an operator decision.
- **Logs are redacted at the logger** (`src/utils/logger.py`, `redact()` +
  `_RedactFilter`): `requests` and most provider SDKs put the **full request
  URL** into their exception text, and Polygon / Alpha Vantage / FMP
  authenticate by **query string**, so the ordinary idiom
  `log.warning(f"{symbol}: Polygon OHLCV failed ({exc})")` writes a live API key
  into `logs/bot.log` and into whatever the operator redirected stdout to.
  Observed 2026-09-28: a `--mode validate` run leaked a Polygon key into
  `data/validate.log` on the first HTTP 429, and from there into a terminal
  paste. The filter sits on the **logger**, not a handler (a handler filter is
  bypassed by any handler a caller attaches later) and redacts the **formatted**
  message, so printf-style `log.warning("%s failed (%s)", sym, exc)` is covered
  too. It masks `key=value` / `"key": "value"` for api-key/token/secret/password
  names, `Bearer`/`Basic` credentials (which carry the value after a space, so
  the key=value pattern cannot see them), and the literal secret *values* read
  from config — catching one logged outside a URL. Only the value is masked, so
  the rest of the URL stays debuggable. Never raises. This lives here rather
  than at the ~80 sites that interpolate an exception because the leak is a
  property of the exception text, not of any call site, and a rule that must be
  remembered 80 times gets missed on the 81st.
- **`period="max"` is a request, not a parse failure** (`src/data/fetcher.py`,
  `_resolve_period`): `_period_to_timedelta` returned `None` for *both* "give me
  everything" and "unparseable", and the two callers reading that `None` guessed
  differently — and both guessed wrong. The Alpaca gate read it as "cannot
  serve" and skipped Alpaca **silently**, so `fetch_ohlcv(ticker,
  period="max")` — which is what `src/backtest/engine.py` asks for on every
  ticker — never reached the one provider whose rate limits we were *not*
  hitting, with nothing in the log to say why. Polygon read the same `None` as
  "default to 365 days", so a backtest asking for full history was quietly
  handed **one year** whenever Polygon answered. Observed 2026-09-28: a
  `--mode validate` run had every ticker fall Alpaca → Polygon (429) → yfinance
  (429), and the only trace was `Polygon OHLCV failed … falling back to
  yfinance`. `_resolve_period` now maps `max` to `_MAX_HISTORY` (25y —
  deliberately longer than any provider's retention, so the API clamps instead
  of us guessing a horizon) and returns `None` **only** for genuinely invalid
  input. The Alpaca gate also names its skip reason rather than falling through
  in silence, since a silent skip is indistinguishable from having tried and
  failed, and Polygon's 365-day default now logs when it bites.
  **Backtest prefetch:** `run_backtest` warms the cache with one
  `fetch_ohlcv_batch` pass before the per-ticker loop, so ~500 blocking round
  trips per walk-forward window become cache hits paid once per run. Note that
  helper is a **parallel fan-out, not a multi-symbol request** — it threads
  `fetch_ohlcv`, so it is still one HTTP call per ticker. Calling it a "batch"
  would misdescribe the request volume; true multi-symbol batching against
  Alpaca's `symbols=` parameter remains undone.
- **Production volume is IEX volume — ~3% of the tape. OPEN, not fixed.**
  `ALPACA_DATA_FEED` defaults to `iex` (`src/data/fetcher.py:101`) and
  `render.yaml` does not set it, so wherever Alpaca serves bars — which is
  first in the chain whenever its keys are present, i.e. in production — every
  bar's `Volume` is the IEX venue's share only. Measured 2026-09-30 against
  consolidated 30-day averages: AAPL 2.8%, NVDA 2.5%, CDE 3.4%, HL 3.2%,
  KOS 7.1% — **median 3.2%, ~31× too small**. So every **absolute** volume
  threshold in `config.yaml` runs ~31× stricter in production than written:
  `filter_min_avg_volume` (100k — `signals/filters.py` reads
  `df["Volume"].tail(20).mean()` straight off the bars, so `ILLIQUID` rejects
  names with anything up to ~3.1M real shares/day), penny mode's
  `min_dollar_volume` ($3M → effectively ~$93M, i.e. almost no penny stock
  passes the penny rail), the screener presets' `min_avg_volume`, and
  `premarket.min_pm_dollar_volume`. Seen directly: at a $3M/day floor,
  IEX-priced selection admitted **6 of 604** names; the same floor on
  consolidated volume admitted **815 of 8,286**. Ratios (`rel_volume`, the
  precursor's dry-up) mostly cancel the capture fraction, but it varies
  2.5–7.1% by name, so they are noisier, not unbiased. And it is an
  **evaluator-vs-harness disagreement**: a backtest served by yfinance reads
  consolidated volume for the same bar production reads at 3%. Worse on
  **intraday**: IEX emits *no bar at all* for a minute without an IEX print,
  so thin names' 1m tapes arrive mostly missing (SRFM 98/390 minutes, FLX
  18/390) — the movers enrichment runs on exactly those tapes.
  **The fix is free for anything that does not need the last 15 minutes**: the
  free key reads `feed=sip` (consolidated; 99.5–105.7% of the tape) for any
  query ending ≥ 15 min ago, which covers daily bars and every backtest.
  Real-time intraday **cannot** use SIP on the free tier — that needs Alpaca's
  paid plan or another consolidated source. Deliberately **not** applied yet:
  correcting it makes production's *effective* filters looser — back to the
  values written in `config.yaml` — and admits many more names, which is an
  operator decision rather than a silent bug fix.
- **All network fetches** go through `src/utils/http.py` (timeouts + bounded
  retry), including the constituent-list fetches in `src/data/universe.py`
  (tables/headers are located by content, not position). `src/data/cache.py`
  is a TTL disk cache with atomic writes, a lock-guarded eviction sweep, and
  a stale-fallback path.
- **Observability (P2.3):** `src/observability/` is pure telemetry — it never
  gates or places an order. `metrics.py` (`MetricsLedger`, singleton
  `default_ledger()`, interprocess-`flock`ed writes like the breaker store)
  records one bounded JSON record per scan (latency, scanned/signal counts,
  buy-side label histogram, regime) and its `summary()` exposes avg/**p95**
  (nearest-rank) latency and a **data-gap** flag. Gap detection
  (`observability.data_gap_seconds`) is **default-disabled** — raw elapsed time
  can't tell an outage from a normal quiet period (overnight/weekends), so it's
  opt-in above the deployment's real cadence; active-outage detection is better
  driven by an external watchdog polling `/api/metrics`.
  `attribution.py` turns the P0.4 trade-outcome ledger into a per-signal-label
  scorecard. `alerting.py` sends opt-in operational alerts (breaker trips, data
  gaps) via `send_text_alert` — **default off** (`observability.alerts.enabled`),
  best-effort, never raises. `run_daily_scan` records each scan (best-effort);
  the executor alerts only on the broker-breaker false→true trip transition.
  Surfaced via `/api/metrics`, `/api/attribution`, and dashboard panels.
  **Alert outcome ledger** (`alert_outcomes.py`, config
  `observability.alert_outcomes`, **default on** — telemetry like the metrics
  ledger): every fired alert (movers setups, catalyst wire) is recorded and a
  resolver later measures the move from the alert instant to each configured
  horizon (default +30m/+2h) **in the direction the alert implied**, from 1m
  bars. Honesty rules: the baseline is the first 1m bar at/after the alert
  (never a stale list price), unmeasurable alerts are counted as `unresolved`
  rather than dropped from the denominator, the resolver is fetch-capped per
  worker cycle (`max_resolve_fetches_per_cycle`), and stale pendings are
  expired by a free sweep. The worker records after both alert paths and
  resolves every cycle — **including outside the scan window**, since alerts
  near the close have horizons landing after 16:00. Surfaced via
  `/api/alert-outcomes` and a per-source hit-rate/avg-move line in the
  dashboard worker strip. This is the evidence base for "are the alerts any
  good" — the automated version of hand-checking prices after the fact.
- **Forecast-interval calibration** (`src/research/interval_calibration.py`,
  `--mode forecast-coverage`, `docs/KRONOS_EVALUATION.md`): measurement only —
  `src/research/` may not import the executor or the broker, and a test asserts
  it. Written to evaluate **Kronos** (github.com/shiyu-coder/Kronos, MIT, AAAI
  2026) but forecaster-agnostic: anything answering "given bars to today, where
  is the close in `h` bars at this confidence" is scorable.
  **Coverage is necessary and nowhere near sufficient.** A 90% band should
  contain the realised close 90% of the time, but coverage cannot *rank*
  forecasters — widen the band and it is perfect and useless. So every run
  reports coverage **plus** width **plus** the Winkler **interval score**, the
  proper scoring rule that trades them off (widening costs width every window;
  missing costs `2/α` × the shortfall, 20× at the 90% level). Reading any one
  alone is the mistake.
  **And there is always a baseline**, because the upstream repo has none: it
  reports directional accuracy, MAE, a "<5% error" rate, correlation and a
  Sharpe against nothing, and two of those are actively misleading on price
  series (correlation of predicted vs actual *levels* is ~1 for any method
  including "tomorrow equals today"; a <5% one-day error is met by the naive
  forecast nearly always). `naive_gaussian` and `naive_empirical` are scored on
  the identical windows.
  **The harness is self-tested on synthetic data whose answer is known** — a
  measurement tool only ever pointed at real data has not been tested, it has
  been used, and a bug in it looks exactly like a finding. The tests assert that
  a correct band recovers nominal at 50/80/90/95%, that a band a third too
  narrow is caught with nominal *excluded* from the CI, that widening to
  guarantee coverage **and** narrowing to look sharp both lose on score, that no
  forecaster sees a bar past the origin, and that a forecaster declining the
  hard windows is counted rather than flattered.
  **The number that matters**: on a Gaussian random walk the *true process*
  scores 18.631 against `naive_empirical`'s 19.131 — a **2.6% edge for a
  forecaster that cannot be beaten**. The free baseline is within 3% of optimal,
  so the bar is not "is it calibrated" (trailing vol already is); it is whether
  real prices are predictable enough to clear that, and the entire available
  headroom is small.
  Origins are spaced `stride` (default `horizon`) so windows do **not** overlap —
  overlapping windows share most of their path, which makes hits correlated and
  the coverage CI far too narrow; `windows_overlap` is reported when overridden.
  Kronos itself is an **optional** dependency (`src/research/kronos_forecaster.py`,
  lazy torch import, `KronosUnavailable` with a stated reason, never a
  traceback). Three departures from the popular walkthrough, each verified in the
  repo source: `top_p=1.0` not 0.9 (nucleus truncation clips the tail and the
  clipped percentiles are then sold as uncertainty), 200 paths not 20 (at n=20
  the 5th/95th percentiles are the sample min/max), and one batched
  `predict_batch` call with `sample_count=1` — because `model/kronos.py:467` does
  `preds = np.mean(preds, axis=1)`, so `sample_count>1` averages the paths into a
  single line and destroys the distribution the whole exercise is about.
  **A pass licenses nothing about direction.** What a calibrated, sharper-than-naive
  band earns is a role in **stop distance and sizing** — where this system is
  weakest (both come from ATR alone) and where `docs/COST_MODEL.md` showed the
  binding constraint already lives: risk-unit versus friction is a range
  question, not a direction one.
- **Explainability (P3.1):** `src/explain/narrative.py` is an *optional* LLM
  layer that turns the **verified quant snapshot** into a plain-English bull/bear
  read for the dashboard/alerts. It is **advisory only** — no import of the
  executor/broker, structurally firewalled from the money path, and never raises.
  `build_snapshot()` sends only **structured verified fields** (scores, levels,
  regime, our own reason strings) — never raw news/social text — and the system
  prompt treats the snapshot as inert data and forbids buy/sell advice
  (prompt-injection sandbox). Uses the **Anthropic SDK** (`claude-opus-5`
  default; `anthropic` is an **optional** dependency — `requirements-explain.txt`,
  lazy-imported, only needed when enabled).
  **Cost-capped and default off** (`explain.enabled`): per-call `max_tokens` at
  `effort: low`, bounded input, and an interprocess-`flock`ed per-day call cap
  (reserved only after a usable client exists). Needs `ANTHROPIC_API_KEY`.
  Surfaced via `GET /api/explain?ticker=…` and an on-demand dashboard panel.
- **Portfolio + Risk Engine (roadmap Phase 4):** `src/portfolio/` (pure
  numpy/pandas volatility/correlation-aware sizing) and `src/risk/engine.py`
  (`PortfolioRiskEngine` → `RiskDecision`: portfolio-level gross-leverage,
  single-name-weight, portfolio-heat, correlated-cluster, and drawdown checks)
  are an **advisory, default-off** layer that *complements* `RiskGuard`, never
  replaces it. Firewalled from the money path (no executor/broker import) and
  fail-open (any internal error → *approve*, so a bug can't silently block
  trading). Default `portfolio.enabled=false` makes it a no-op surfaced only via
  `GET /api/portfolio-risk`; the separate opt-in `portfolio.gate_entries` lets
  `execute_trade_plan` consult it **after** RiskGuard approves — veto-only
  (tighten, never loosen). Config `portfolio:` / `PORTFOLIO_*`.
- **Async?** No — this is a synchronous codebase (Flask sync views, thread-pool
  fan-out for scans). Do not introduce `async def` without cause. The **one**
  sanctioned exception is `src/streaming/price_stream.py` (MOVERS Phase 8), where
  Alpaca's asyncio websocket is *quarantined*: it runs its own event loop inside
  a daemon thread and the only surface the rest of the app touches is a plain,
  lock-guarded snapshot (`latest`/`snapshot`/`status`). No `async` leaks past
  that file — callers stay synchronous. See `docs/STREAMING.md`.
- **Long HTTP work runs off the request path** (`src/scanner/scan_jobs.py`): a
  full-universe scan fans out over ~500 tickers and cannot finish inside
  `gunicorn --timeout 120` — `render.yaml` itself sized it at "60–120s", so the
  documented worst case *equalled* the kill deadline, and on a 0.1-CPU instance
  sharing four threads with the in-process worker it ran well past. gunicorn
  killed the worker mid-scan and the browser reported a dropped connection
  ("Failed to fetch"), which reads as a network fault rather than a timeout.
  `/api/scan` now starts a daemon-thread job and returns a handle; the client
  polls `/api/scan-status`. The scan is **not** shortened or capped — the wait
  moved off the request. **Single-flight** (a second caller joins the running
  job; two concurrent 500-ticker fan-outs thrash rather than halve the time) and
  bounded (finished jobs expire, registry trimmed). State is per-process, so it
  requires `--workers 1` — which `render.yaml` already mandates for the
  in-process worker — and an unrecognised job id says so rather than 404-ing
  blankly. `run_daily_scan(progress=…)` reports real stages; the batch fetch is
  one blocking call, so it stays a named stage instead of a fabricated
  percentage.
- **MOVERS real-time system (Phases 5–8):** the intraday discovery→alert→monitor
  stack runs as an always-on worker (`src/scanner/movers_worker.py`,
  `--mode movers-worker`) that continuously re-discovers movers, alerts on new
  qualifiers, and invalidates setups that break down. It publishes a shared JSON
  snapshot each cycle (`src/scanner/movers_state.py`, flock-guarded like the
  circuit breaker) that the dashboard reads via `/api/movers-worker` (Phase 7
  shared state — heartbeat, live watch set, recent alerts/invalidations). Phase 8
  adds an **opt-in, fail-open** live price tape (`PriceStream`, config
  `movers.stream`, default off): when enabled the worker keeps the tape
  subscribed to its live watch set and the snapshot carries live prices + stream
  status. Advisory/telemetry only — no import of the executor/broker; the stream
  is a data source, never a gate.
- **Catalyst-triggered alerts** (`src/catalysts/catalyst_alerts.py`, config
  `movers.catalyst_alerts`, **default off**): the movers path is structurally
  late — a name only reaches it after moving enough to appear on a provider's
  gainers list, and rediscovery runs every ~8 cycles. This fires on the **news
  wire** instead, where a catalyst has a definite publication time that precedes
  the price reaction. `news_feed.fetch_news_batch()` is one Alpaca request for a
  whole watchlist (or, with `universe: all`, the market-wide firehose, so a name
  can surface before anyone has listed it as a mover), so the worker polls it
  **every cycle** rather than on the discovery cadence.
  **The wire keeps its OWN window, wider than the worker's scan window**
  (`catalyst_alerts.window_start_et` / `window_end_et` / `weekdays_only`,
  default 04:00–20:00 ET weekdays; equal bounds = no time gate, end-before-start
  crosses midnight; `window_open()` self-gates `poll()` so every caller inherits
  it, and fails OPEN on a clock error). It used to share the 07:00–16:00 scan
  window, which muted it for exactly the hours the news breaks: **US earnings
  are overwhelmingly released after the close**, so an approval at 17:00 stayed
  invisible until 07:00 the next morning — by which point the pre-market had
  repriced it and the head start this layer exists for was gone. Discovery and
  the monitor stay gated on the scan window because they read **volume**, which
  the extended-hours tape does not supply (Yahoo publishes none at all); the
  monitor's rule 3 would read the after-hours volume collapse as "relative
  volume faded" on every watched name — the same false-invalidation the halt
  handling already guards with `skip_invalidation`. Nothing on the wire path
  reads volume, so none of that applies to it. Overnight and weekends are
  dropped because the next pre-market poll picks those up regardless.
  Each headline is
  classified **alone** by the existing `news_feed` tier taxonomy (so `strong`
  means what it means to the screener), routed to every ticker the wire attached
  to it, deduped per (symbol, story), and **suppressed for halted symbols**.
  Explicitly **not prediction** — it reports that a catalyst was published
  sooner than a price-derived scanner can notice the consequence; the alert text
  says so. Discovery/alerting only; fail-soft everywhere.
  **Only symbols the broker could act on are alerted**
  (`require_tradeable_symbol`, default on): `poll()` iterated `headline.symbols`
  raw, and the wire attaches whatever it likes. Observed 2026-09-28: it alerted
  `TSX:SGR` — the Toronto **target** of the Brixmor deal, and the only side of
  that deal which actually re-prices — a listing with no US intraday bars, so
  the alert-outcome ledger could not have measured it either. `_can_use_alpaca_symbol`
  was not reusable: it rejects only a `^` prefix, so `TSX:SGR` sails past it.
  `is_tradeable_symbol` requires a leading letter, up to five alphanumerics and
  an optional `.A`/`-B` class suffix; what it actually refuses is the
  separator-bearing token (`TSX:SGR`, `^GSPC`, `BTC/USD`). **Digits are allowed
  deliberately** — a letters-only rule is an extra claim about US symbols that
  is not reliably true, and the two errors do not cost the same: a stray
  alphanumeric token produces one checkable alert, while refusing a real ticker
  suppresses it with nothing to point at. Skips are counted
  (`skipped_untradeable`) and logged.
  **A deal headline says what is being bought.** `_STRONG_RE` matches
  `acquir\w+`, so it grades "X Acquires Y" and "Y To Be Acquired By X"
  identically — and in an acquisition the **target** re-prices toward the offer
  while the acquirer typically does not, so a side-blind `strong` is close to an
  inversion of where the move is. Five of that morning's six alerts were
  acquirers (FTAI, FIP, BRX twice, VLY). Resolving the side needs a
  symbol→company map, and this path carries symbols **only** — the same reason
  `headline_concerns` is unusable here — so `news_feed.deal_context()` does not
  guess it: it reports that the story is a purchase and, where the phrasing
  allows, the target, and the alert line says to check which side the symbol is
  on. The extracted target is a tell in itself — "27 Boeing 737-700 Aircraft"
  is a lessor's ordinary business, not an event. Its gate is a deliberate
  **superset** of `_STRONG_RE`'s deal branch, because `acquir\w+` does not match
  "acquisition" (no `r` after "acqui") and the two patterns had already drifted
  apart on exactly the FIP phrasing. `to buy`/`to purchase` label but are left
  **out** of `_STRONG_RE`: "Time To Buy Acme Stock" is commentary, and admitting
  it would let opinion earn the top catalyst tier.
  Every knob takes an env override (`CATALYST_ALERTS_ENABLED`, `_UNIVERSE`,
  `_TIERS`, `_LOOKBACK_SEC`, `_DEDUP_TTL_SEC`, `_MAX_PER_RUN`, `_WINDOW_START_ET`,
  `_WINDOW_END_ET`, `_WEEKDAYS_ONLY`,
  `_SUPPRESS_HALTED`, `_REQUIRE_TRADEABLE`) so a managed host can retune the wire without a commit +
  redeploy. Because every misconfiguration here presents as "the wire was
  quiet", the ones that *cannot* match anything are **loud**: an unknown tier or
  universe, and the default `universe: watchlist` with no `CUSTOM_WATCHLIST`,
  each log a warning once. Firing is visible on the dashboard worker strip —
  a `N catalyst` counter shown only when the layer is actually running (`features.catalyst_alerts`
  in the worker snapshot, since a bare `0` cannot distinguish "quiet" from "off")
  plus recent alerts with their headline, marked `(not delivered)` when Telegram
  creds are absent. That is the normal state on the Render *web* service, which
  deliberately carries no creds so the worker service alerts exactly once.
- **Held-back alerts are disclosed** (`MoversAlerter.last_suppressed()`): a
  candidate that clears `alerts.min_score` but is blocked by another rule is
  recorded with its reason (`no_intraday_data` / `stale_session_data` /
  `halted` / `already_alerted`) and published in the worker snapshot. Observed 2026-08-19: ZSTK ran +370%,
  scored 85, entered the watch set, and never alerted because
  `alerts.require_enriched` (default **on**) refuses a candidate whose intraday
  bars are missing — its score would then be the *discovery* score,
  `min(85, |change| × 2.5)`, i.e. magnitude alone, the weakest thing measured.
  The suppression is correct; being unable to tell it apart from "never
  discovered" was not. The dashboard shows `no_intraday_data`/
  `stale_session_data`/`halted` only (`already_alerted` is not a withheld
  decision), and sub-threshold candidates are never listed — the majority would
  bury the entries that mean something.
  **`require_fresh_session`** (default **on**) is the second half of that rule
  and closes what `require_enriched` structurally cannot see. The movers chain
  reports the **previous** session before 09:30 (the same staleness
  `premarket_movers` exists for), and the intraday fetch then returns
  *yesterday's* bars — so `enriched` is True, the metrics are real, and every
  one of them describes a finished day while the alert reads as live. Observed
  2026-09-25: alerts fired at 08:18–08:19 ET carried Thursday's closing prices
  and Thursday's day moves verbatim (HUBC $2.33 −27.4%, TRT $7.26 −36.6%,
  AVX $5.45 +32.9% — each an exact match for the prior session's close and
  change). `_enrich_candidate` now records the last closed bar's timestamp on
  the candidate (`bars_as_of`, naive UTC) and the alerter requires it to fall
  on the **same ET calendar date** as the alert. The ET date, not an age in
  hours, is what "this session" means, and it stays right across weekends and
  holidays with no market calendar. An unreadable timestamp counts as stale:
  the gate may only pass when freshness is *established*, never merely
  unrefuted. Held-back candidates are disclosed like every other reason. This
  suppresses the 07:00–09:30 phantom alerts; **`movers.premarket_discovery`**
  (default **on**) is what makes that window produce real candidates instead of
  correct silence — see below.
- **Pre-market discovery in the worker** (`movers.premarket_discovery`,
  default **on**; `movers_worker._discover_for_now` →
  `movers.fetch_premarket_candidates`): before 09:30 the regular chain answers
  about **yesterday's** session — the staleness `premarket_movers` was written
  for, and the one `require_fresh_session` refuses — so the 07:00–09:30 window
  discovered a list the alerter then correctly threw away, and silence was the
  right behaviour. The worker now routes that window to the live extended-hours
  tape and falls back to the regular chain for the rest of the day. Everything
  downstream is unchanged: same `_score`, same `_enrich_candidate`, same halt
  annotation, same alerter and every one of its gates. **Only the source of the
  list differs.**
  It fails **over, not open**: an empty or failed pre-market discovery does
  *not* substitute the regular chain, because substituting it is exactly how
  yesterday's list reached the morning window in the first place — an empty
  extended-hours list is a real answer. A clock/timezone fault falls back to the
  regular chain rather than taking discovery down.
  **`_enrich_candidate` gained `prepost`,** and that interaction is what makes
  or breaks this: without it the enrichment fetch returns REGULAR-session bars,
  which before the open are yesterday's, so every candidate would carry real
  metrics describing a finished session and `require_fresh_session` would
  suppress the whole list as `stale_session_data` — discovered and then thrown
  away, which is worse than quiet because it reads as the gate being broken.
  Two limits remain, both provider-shaped and both disclosed rather than fixed:
  **coverage** (`bounded_pool` providers rank prior-session lists and are
  structurally blind to a name that was quiet yesterday; the serving provider
  and its class land in `served_by["premarket"]`) and **volume** (the
  extended-hours tape carries little or none — Yahoo publishes none at all — so
  `rel_volume` pre-market is weak or absent, and a candidate too thin to score
  stays unenriched and is held back as `no_session_volume`. That is correct:
  participation is the thing we cannot measure there, so we do not claim to have
  measured it).
  **Enrichment refuses a tape that published prices but no volume.** It used to
  set `enriched=True` whenever `score_intraday_frame` returned anything, and
  measuring the keyless path at 08:43 ET on 2026-09-28 showed what that costs:
  14 candidates, every extended-hours bar carrying volume **0** (KOS had 50M
  shares across 09-22..09-25 and exactly 0 that morning), so `rel_volume` came
  back `0.0` and VWAP `None` for all of them — and enrichment claimed success.
  Not cosmetic: `_enriched_score` caps at gap(28) + rvol(30) + mom(24) +
  vwap(12) + brk(8), so with rvol and vwap both structurally 0 the ceiling is
  **60 against an alert floor of 70** — no candidate could alert whatever the
  size of the move. And it was **invisible**, because `last_suppressed()` only
  records candidates *above* the floor: a +50% gapper went 85 → 28 and then
  appeared in neither `fired` nor `suppressed`, identical to a quiet tape. The
  check is `vwap(df) is None`, which is true exactly when the current session's
  total volume is ≤ 0 — so it reuses that function's own failure condition
  rather than duplicating the test. Unenriched, the candidate keeps its
  magnitude base score, clears the floor on a real gap, and is disclosed
  (`enrich_blocked` → the alerter's reason, dashboard label). Renormalising the
  score over the measurable components was **rejected**: that manufactures an
  alert out of magnitude plus momentum, and magnitude alone is the weakest thing
  measured — the whole reason `require_enriched` exists. The worker's own scan window still starts at
  `continuous_scan.premarket_start_et` (07:00), so **04:00–07:00 remains outside
  it**. Surfaced as `features.premarket_discovery` in the worker snapshot, for
  the same reason the catalyst flag exists: a quiet morning looks identical
  whether the worker was reading the live tape or yesterday's list.
- **Movers provider chain** (`src/scanner/movers_providers.py`, config
  `movers.providers`, default `[alpaca, polygon, fmp]`): discovery used to be
  FMP-only, so an exhausted daily quota took the intraday path down entirely.
  Each source walks the chain; `movers.provider_mode` decides how.
  **`merge` (default)** queries every configured provider that serves the
  source and **unions** the answers. `first_wins` — the original chain, where
  the first provider that answers serves the source and the rest are never
  asked — made the *leading* provider's screener universe the entire candidate
  pool: a name outside it was invisible with nothing recorded in `withheld` or
  `suppressed`, because it never entered the pipeline at all. Observed
  2026-09-25: MSGY ran $2.13 → $5.95 (+202%) and never alerted, though
  replaying its own tape through the live scorer clears the alert floor for
  seven consecutive windows from 10:35 ET (100/100 at 10:45) and the LULD halt
  that legitimately silences it did not begin until ~11:07. Providers disagree
  about what a "mover" is — universe, float/liquidity floors, SIP vs IEX — so
  the union is the only pool that reflects the market rather than one vendor's
  screener. Cross-**provider** duplicates are resolved in `_fetch_source` by
  **chain order**, deliberately *not* by the cross-**source** rule one level
  down (which keeps the largest-magnitude change): that rule was reasoned about
  for two endpoints of one feed, and letting rival vendors compete under it
  would mean the most extreme quote always wins — and since these lists report
  **unadjusted** changes, that systematically selects whichever provider is
  most wrong, across the whole band below `suspect_change_pct` where the
  corporate-action guard never looks. A later provider only ever *adds*
  symbols. A provider failing beside a working one is now a **partial** outage
  (`<source>: <provider>: could_not_answer` in `source_errors`, dashboard reads
  `degraded`) where first-wins hid it; everything failing is still
  `no_provider_answered`. **Cost:** merge multiplies calls per source by the
  providers serving it, and `worker.rediscover_every_cycles` is set to **8**
  (not 5) to pay for it: ~68 discoveries/day and ~204 FMP calls, inside its
  250/day free tier, where 5 would be ~108 and ~324 — past it, and an exhausted
  FMP quota is the exact failure the chain was built for. The price is up to
  ~8 minutes before a brand-new mover is first seen, bought against a union
  that can see it at all. Watch `source_errors`.
  The adapters return `list`-vs-`None` on purpose: an **empty list is a real
  answer** (a quiet market) and stops the chain, while `None` means "could not
  answer" (no key, HTTP error, or a payload the API doesn't document — FMP
  replies to an exhausted quota with HTTP 200 and a JSON *object*, so
  "successful-looking but broken" has to be detectable). Providers declare the
  sources they support — Alpaca's screener has no price on most-actives rows,
  Polygon's snapshot has no most-actives endpoint, so both serve gainers/losers
  only and FMP alone covers `most_actives` — and an unsupported or unconfigured
  provider is *skipped*, never counted as a failure, which is why the health
  reason distinguishes `no_provider_configured` from `no_provider_answered`.
  Because providers compute "movers" differently (universe, floors, SIP vs
  IEX), the serving provider is recorded per source in `served_by` and named in
  the dashboard — a fallback changes the character of the list, so it is
  disclosed rather than swapped silently.
- **Sub-$1 discovery, stocks only** (`movers.min_price` / `premarket_movers.
  min_price` **$0.05**, `movers.exclude_derivatives` default **on**,
  `src/data/security_type.py`): the floor was $1.00, which is why NIVF
  ($0.072 → $0.25, +244% on 2026-09-30) never entered the pipeline. Both
  floors came down, because the worker discovers 07:00–09:30 from
  `premarket_movers` and NIVF did most of its run before the open. **The floor
  alone would have made discovery worse**: with it removed, 19 of the 40
  top-scoring watch slots went to sub-$1 names — mostly warrants and rights,
  since a $0.004 warrant moving −59% scores the discovery maximum — and NIVFW
  rode in beside NIVF, pushing real movers out. So derivatives are excluded in
  both paths, first in the candidate loop so the corporate-action guard never
  spends a fetch verifying one, and every exclusion is recorded
  (`excluded_derivatives`, plus `derivative_filter`: `asset_names` or
  `symbol_suffix`) rather than dropped silently. Live on 2026-09-30 at 09:57:
  73 candidates, 27 derivatives excluded by name, 11 sub-$1 **stocks** found,
  7 of 40 watch slots sub-$1.
  **Classified by Alpaca's asset NAME, not the ticker.** A W/R/U suffix rule
  misses OPENZ ("Series Z Warrants") and would flag SNOW. And a naive name rule
  was wrong too: `\bunits?\b` flagged every partnership, because an MLP's
  *common units are its equity* — Energy Transfer is "Energy Transfer LP Common
  Units representing limited partner interests", and Plains All American,
  Alliance Resource, CrossAmerica, UNG and the Grayscale trusts read the same
  way. An audit of all 14,389 asset names caught it before it shipped; units now
  count as derivatives only when they are not partnership/trust equity (the SPAC
  "Unit 1 CL A & 1/3 WT" bundles). The suffix rule is a fallback for when the
  asset list is unavailable, and on the 8,840 listed assets it wrongly drops one
  stock (PSNYW, a Polestar ADS) and misses 26 derivatives — erring toward
  admitting a stray warrant, the visible error, over hiding a stock, the
  invisible one. Credentials are checked **before** the disk cache, so a
  keyless caller (every unit test) never reads a map a live run left in
  `data/`. Sub-$1 prices are shown to **four decimals** (Reg NMS Rule 612 tick)
  in the Telegram alert and the dashboard's `formatPrice` — two decimals stated
  NIVF's $0.2477 as $0.25 and a $0.0636 name as $0.06. This is **discovery and
  alerting only**: RiskGuard, `signals/filters.py` and the penny rails are
  unchanged, and the worker cannot place an order. Expect many sub-$1 names to
  be held back as `no_intraday_data` — they are where production's IEX bars are
  thinnest (see the IEX note above).
- **Discovery-source health** (`movers.last_source_errors()`): every fetcher
  fails soft to `[]`, so a dead FMP key or an exhausted quota used to render
  identically to a quiet market ("no movers cleared the filters"). Failures are
  now recorded per run and surfaced by `/api/movers` as `available:false` +
  `reason` (nothing returned) or `degraded:true` + `source_errors` (partial).
- **Corporate-action guard** (config `movers.suspect_change_pct` /
  `suspect_agreement_pct`): the mover lists report an **unadjusted** quote
  change, so a reverse split's price multiple arrives looking like a move (YYAI
  1-for-20 on 2026-08-17 → "+1668%" on a stock up ~23%). Past
  `suspect_change_pct` the number is re-derived from our own **split-adjusted**
  bars (`_verified_change_pct`) and the two are compared, giving three outcomes:
  they **agree** within `suspect_agreement_pct` ⇒ the extreme move is
  *corroborated* and published (a split would have separated them by the split
  ratio, not matched); they **disagree** and ours is plausible ⇒ the artifact is
  *corrected* to ours; anything else — no bars at all, or two different
  implausible numbers — ⇒ **withheld**. Treating agreement as grounds for
  rejection is what hid CID HoldCo (DAIC) on 2026-08-22, a genuine
  $0.426 → $2.31 (+442%) on 6M shares; the residual risk (our provider *also*
  lagging the split, making the agreement spurious) is accepted for a
  discovery-only layer and reversible with `suspect_agreement_pct: 0`. Both
  surviving branches publish **our** number and flag it `change_verified` (the
  dashboard's `†`), because in neither case is it the feed's. Withheld
  candidates are **disclosed, not dropped silently** (`movers.last_withheld()`,
  `/api/movers` → `withheld`, and the movers status line): an unexplained
  absence is indistinguishable from a name that was never discovered, which is
  what left DAIC with nothing to point at.
- **Trade-halt awareness** (`src/data/halts.py`, config `movers.halts`): the
  movers feed reports a halted stock as a normal (usually top-ranked) mover.
  Halt state comes from Nasdaq Trader's official UTP feed (free, no key, all US
  venues), parsed **by local tag name** so a namespace change degrades to "no
  data" rather than wrong data, TTL-cached (~45s) with a 5-minute backoff after a
  failure so a dead feed can't cost a timeout on every worker cycle. Halted
  candidates are **flagged, not dropped** by default (`exclude_from_list`), kept
  out of alerts (`suppress_alerts` — you can't act on a halt), and **skipped by
  the monitor's invalidation rules** (`skip_invalidation`): while halted no
  trades print, so relative volume decays toward zero and rule 3 would report
  "relative volume faded" on a setup the market never let move. **Fails open** —
  an outage yields "no halts known" rather than muting every alert; callers can
  distinguish the two via `halts.last_fetch_ok()`.

### Adding a technical indicator
Add a vectorized function in the relevant `src/technical/*.py`, wire it into the
scorer that consumes it, and expose it through `signals/generator` if it should
affect the composite score. Add a dashboard route in `dashboard.py` only if the
UI needs it.

### Adding a config-driven threshold
Add the key to `config.yaml` (or `.env` for secrets/tuning), add a typed
accessor in `src/utils/config.py`, and read it at the point of use.

---

## Dashboard API

| Method | Path | Description |
|--------|------|-------------|
| GET | `/healthz` | Liveness check |
| GET | `/` | HTML dashboard |
| GET | `/api/diagnose` | Pipeline/connectivity diagnostics |
| GET | `/api/price-history` | OHLCV series for a ticker/range |
| GET | `/api/regime` | Current market regime |
| GET | `/api/scan-ticker?ticker=AAPL` | Single-ticker scan + trade plan |
| GET | `/api/scan` | **Starts** a full-universe scan in the background; returns a job handle immediately (`joined_existing:true` if one was already running) |
| GET | `/api/scan-status?job=<id>` | Poll a scan job: `running` (with `stage`/`done`/`total`) → `done` (carries `result`) / `error` / `unknown`. Omit `job` for the currently-running one |
| GET | `/api/scan-gaps` | Pre-market gap scan |
| GET | `/api/scan-breakouts` | Volume-breakout scan |
| GET | `/api/scan-premarket` | Pre-market gap screener: ranked research watchlist from extended-hours bars with continuation/fade_risk evidence profiles — screener only, not signals. `?universe=PREMARKET` discovers the live extended-hours tape and returns `universe_discovery` (serving provider + market_wide/bounded_pool coverage) |
| GET | `/api/paper-scorecard` | Paper-trading scorecard: backtest-comparable metrics on realized trades (P0.4), plus the paper-vs-validated-backtest tolerance comparison |
| GET | `/api/execution-quality` | Realized slippage / implementation shortfall from recorded fills (P2.1) |
| GET | `/api/metrics` | Scan metrics: latency (avg/p95), signal counts, data-gap state (P2.3) |
| GET | `/api/attribution` | Per-signal realized outcome (win rate / avg return / P&L) from the trade ledger (P2.3) |
| GET | `/api/alert-outcomes` | Measured forward outcomes of fired alerts (movers/catalyst): per source+horizon hit rate, avg/median in-direction move, pending/unresolved counts |
| GET | `/api/explain?ticker=AAPL` | Optional AI bull/bear narrative for a ticker's verified signal — advisory only (P3.1) |
| GET | `/api/portfolio-risk` | Advisory portfolio-level risk snapshot: gross leverage / heat / per-name weights vs limits (roadmap Phase 4; `available:false` when disabled) |
| GET | `/api/movers` | Ranked market-wide movers (MOVERS discovery); `?enrich=true` adds early-momentum ranking. `withheld` lists candidates the corporate-action guard refused to publish, with the feed's claim and the reason. `available:false` only when **no** provider in `movers.providers` is configured or answers — Alpaca keys alone are enough, since the chain became `[alpaca, polygon, fmp]` |
| GET | `/api/movers-worker` | Live state of the always-on movers worker: heartbeat, cycle stats, watch set, recent alerts/invalidations, `features` (which opt-in layers are running), and `suppressed` (qualifying candidates held back, with the reason) (Phase 7 shared state; `available:false` when the worker isn't running) |
| GET | `/api/breaker-state` | Circuit-breaker / kill-switch state (P0.3) |
| POST | `/api/breaker` | Flip the global halt switch (`{"action":"halt"\|"resume"}`) |
| POST | `/api/alert-test` | Send a test Telegram message using the deployment's own credentials; reports Telegram's error description (credentials described by shape only, never returned) |
| POST | `/api/clear-cache` | Clear the market-data cache |

All `/api/*` routes are rate-limited, and authenticated when the app is
network-exposed (see Dashboard security above).

---

## Key config (`config.yaml`)

`account_size`, `risk_per_trade`, `max_position_pct`, `scan_universe`,
`signal_weights` (technical/volume/sentiment/fundamental/macro), `signal_thresholds`
(score→label), `atr_stop_multiplier`, `target_rr_ratios`/`target_exit_pcts`,
`filter_*` (disqualifier thresholds), and `safety_rules` (max positions,
loss/exposure limits, cooldowns) — `RiskGuard` enforces the position/exposure/
trade-count/bear-market/duplicate rules, plus stop-required, daily/weekly
realized-loss limits, post-loss cooldown, earnings-week (P1.4 calendar-backed),
the P1.4 catalyst-tag veto (`catalysts:`), and the live-only paper graduation
gate (tenure + `paper_scorecard:` floors + the opt-in `baseline_*` tolerance
check against the validated backtest).

**Penny mode** (`penny:` / `ALLOW_PENNY_STOCKS`, default off): an opt-in
low-price/small-cap profile that does **not** disable protection — it swaps the
standard disqualifiers (`src/signals/filters.py`) for penny-specific **hard
rails**: a dollar-volume floor (price × avg volume — the real liquidity gate), a
positive market-cap floor (not 0), a price band, a tighter chaser-trap, and the
bankruptcy check **stays on**. Scan/paper only until validated. See
`docs/PENNY_MODE.md`.

**Which strategy the backtest tests** (`signal_source`, audit B1): `composite`
replays the **live `generate_signal` scorer** point-in-time
(`src/backtest/composite_signal.py`) — real thresholds and labels, entering on
STRONG_BUY/BUY at ≥ `composite_min_score`. Only technical+volume are
reconstructible for a past bar (sentiment/fundamentals/macro are served only as
of today), so those weights are dropped and the rest renormalised — a **subset**
of the live composite, disclosed in every result. `proxy` (the default) is the
legacy hardcoded SMA/SMA/RSI rule and says nothing about the composite. Composite
mode costs ~28 ms/bar; `composite_stride` scores every Nth bar to trade fidelity
for runtime. Candidate ordering when position slots are scarce is
`candidate_ranking` (default `rotate`, a name-neutral date-seeded shuffle —
audit B2 found plain alphabetical order let ticker *name* pick the trades).

Backtest frictions are configurable via `commission_pct`/`slippage_pct`
(config.yaml) or `BACKTEST_COMMISSION_PCT`/`BACKTEST_SLIPPAGE_PCT` (env).
Entries fill at the **next bar's open** after a signal (no same-bar lookahead);
results include `cost_model` and `bias_disclosures` (survivorship / daily
granularity) so reported returns are read with the right caveats. Survivorship
bias can be corrected by supplying a point-in-time membership file via
`universe_history_path` (config.yaml) or `BACKTEST_UNIVERSE_HISTORY_PATH`
(env) — see `src/backtest/universe_history.py` for the JSON format; entries
are then gated by membership on the entry date.

---

## Git Workflow

- Default branch: `main`
- Feature branches: `claude/<feature-name>-<id>`
- Commit messages: descriptive, imperative (`Add RSI divergence to composite`,
  `Fix stop gap-through in backtest`).

## Known Gaps / Contribution Areas

- Backtest survivorship bias is corrected only when a point-in-time membership
  file is supplied (`universe_history_path`); without one it remains and is
  disclosed in results. Build one with
  `python -m src.backtest.build_universe_history` (reconstructs S&P 500
  membership from Wikipedia's constituents + changes tables; early history is
  floored at the oldest change date). Even with the file, price history for
  delisted names depends on the data provider (yfinance drops many).
- No auth layer beyond the dashboard API key; the CLI/broker path trusts local env.
  (The dashboard deliberately emits NO CORS headers — the API is same-origin
  only; the browser UI prompts for the API key and sends it as `X-API-Key`.)
- Alembic/DB not used — there is no relational database in this project.
