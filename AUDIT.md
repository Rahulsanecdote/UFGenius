# UFGenius — Repository Audit

**Date:** 2026-08-03
**Scope:** Full repository (~9,400 LOC Python). Flask signal-bot + Alpaca portfolio, market-data pipeline, technical/fundamental/sentiment scoring, backtest engine, web dashboard.
**Method:** Parallel domain audits (web security, money-moving trade logic, data/indicator/backtest math, secrets/deps/config/tests). Headline findings were re-verified against source and, where noted, at runtime.

> ⚠️ Several findings show the product advertises safety features (risk framework, live execution, stale-data fallback) that are **not actually wired up**. Treat the current tree as **not safe for real-money use** until the CRITICAL items are resolved.

---

## Remediation status (2026-08-03)

**Root cause found.** Most CRITICAL/HIGH findings traced to a single accident: commit
`0f264d6` ("Add OTC penny stock support…") **deleted 27 files** — 9 core source
modules and 18 test files — while only intending to add `gap_scanner.py`. That
deletion is why the risk framework was "unenforced" (the `RiskGuard` that enforces
it was deleted), why order execution "didn't exist", and why "no tests" existed for
the money paths. The lost files were restored from `0f264d6^`.

Fixed on this branch (`claude/repo-audit-qnd1x4`):

| Finding | Resolution |
|---|---|
| C1 missing modules | Restored `generator.py`, `executor.py` (+`RiskGuard`), `orders.py`, `position_tracker.py`, `core/models.py`, and the whole `features/` package from history. Re-added the 5 config accessors they need (`SIGNAL_THRESHOLDS`, `EV_*`, `FEATURE_*`, `RESISTANCE_SNAP_DISCOUNT`, `LIVE_POSITION_STORE_PATH`). |
| C2 safety unenforced | **Resolved.** Restoring `executor.py`'s `RiskGuard` re-enabled the position/exposure/trade-count/bear-market/duplicate rules; a follow-up then wired the rest — `stop_loss_required`, `max_daily_loss_pct`/`max_weekly_loss_pct` and `cooldown_after_loss_hours` (via a realized-P&L ledger the monitor writes at each exit), `trade_earnings_week` (best-effort, when `days_to_earnings` is known), and `paper_trade_days_required` (live path only). |
| C3 sizing floor-to-1 | `trade_plan.py` no longer forces ≥1 share; returns a `skip` plan when a position can't fit inside `max_position_pct`. Guards `entry_price > 0`. |
| C4 stale-cache crash | Implemented `cache.get_stale()` / `get_metadata()`; `get()` no longer deletes expired entries on read; `set()` is now an atomic temp-file+`os.replace` write. |
| H1 exposed-without-auth | Auth is now gated on actual network exposure (`_is_remotely_exposed()`), and the app fails closed at startup if exposed without a key. |
| H2 XFF spoofing | `resolve_client_ip` uses the rightmost (proxy-appended) entry and validates it as an IP. |
| H3 execute guardrail | `--execute` now submits to the **paper** account (was a no-op); real orders require `--live-execute`; added `--dry-run` for preview. |
| H4 hardcoded filters | `filters.py` reads thresholds from config at call time (penny mode respected). |
| H6 Altman Z false-safe | `_altman_z` returns `None` when liabilities are unavailable instead of dividing by `1`. |
| M5 pytest hits network | `pytest.ini` excludes `integration` by default. |
| M8 profit_factor `inf` | Emits JSON `null` instead of `Infinity`. |
| L2 telegram not escaped | Messages are HTML-escaped via a `_format_message` helper (+ 4096 truncation). |
| H5 backtest optimism | Commission + slippage applied to every fill; stops fill at `min(close, stop)` (gap-through); costs configurable via `commission_pct`/`slippage_pct`. |
| M11 survivorship bias | **Fixed in a follow-up:** the backtest accepts a point-in-time membership file (`universe_history_path` / `BACKTEST_UNIVERSE_HISTORY_PATH`); entries are gated by membership on the entry date and the disclosure switches to "mitigated" (delisted-name price coverage still depends on the provider). Without a file, behavior and disclosure are unchanged. |
| M4 same-bar entry | **Fixed in a follow-up:** entries now fill at the NEXT bar's open after a signal (stop geometry from the signal bar's ATR), removing the same-bar-close lookahead. |
| M2 CLAUDE.md wrong project | Rewritten to match the real Flask signal-bot (routes, CLI, data flow, config). |
| Restored test suite | 18 recovered test files + new `test_audit_fixes.py`; **292 pass**, 3 network tests deselected by default. |

Nothing from the audit's still-open list remains. Same-bar entry (M4) and
survivorship gating (M11) are fixed — see the table above; no point-in-time
membership *dataset* ships with the repo, so M11's fix activates only when
the user supplies one.

M9 and M10 are fixed in a follow-up: the cache's post-write size sweep is
lock-guarded and tolerates files vanishing mid-sweep (concurrent workers
evicting each other's files no longer crash `set()`), and `universe.py` now
fetches constituent lists through `utils/http.py` (timeout + bounded retry),
selecting the Wikipedia table by its `Symbol` header and locating the iShares
CSV header row by content instead of `skiprows=9`.

M1 (dead UI-token path) is fixed by REMOVAL, not by wiring it up: "/" is
unauthenticated, so a server-issued signed token the API accepts would let
any visitor mint credentials — an auth bypass. The browser UI now prompts
for the ordinary dashboard API key on the first 401 (kept in sessionStorage,
sent as `X-API-Key`). The "open CORS" note was stale: no CORS headers were
ever emitted, and this stays deliberate (same-origin API); responses now also
carry `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`, and
`Cache-Control: no-store` on `/api/*`.

### Follow-up round — live-execution hardening

A second PR addresses correctness bugs in the restored real-money execution
path (`src/alpaca/executor.py`, `position_tracker.py`) surfaced by review:

| Issue | Fix |
|---|---|
| Exit tranche allocation could exceed the position (`(1,1,0)` for 1 share → overselling) | `_allocate_exit_tranches` clamps each tranche so they always sum to the held shares. |
| Partial entry fill treated as a completed entry (remaining shares left untracked/unprotected) | Partial fills cancel the unfilled remainder, then protect exactly the filled quantity. |
| Stop stayed sized to the initial position after target fills; stale full-size stop lived on after full exit | Stop is cancel-replaced to the remaining shares after each partial exit and cancelled when the position fully exits via targets. |
| Entry submitted but tracking failure left a live order with no stop | On any tracking failure after submit, the entry order is best-effort cancelled. |
| Position cap counted only broker positions, not in-flight `pending_fill` entries | RiskGuard adds the open tracker positions the broker does not yet report to the broker position count (not `max()`, which undercounts disjoint sets). |
| Closed positions permanently blocked re-entry for that ticker | Duplicate guard uses `has_open()` (ignores closed); stale prior-day closed records are pruned on load. |
| Tracker mutated by monitor + main thread with no lock | All tracker mutations/queries guarded by a reentrant lock. |
| One malformed record discarded all tracked positions on load | Records parsed independently; bad ones skipped, not fatal. |
| Monitor interval could busy-loop at 0/negative | Interval clamped to a ≥60s floor; `MONITOR_INTERVAL_MIN` exposed in config. |

Tests: **308 pass** (adds `test_live_execution_fixes.py`), 3 network tests
deselected by default. The RiskGuard rules that were still pending at this
point (loss limits, cooldown, earnings-week, stop-required, paper-trading-days)
were implemented in a subsequent PR — the C2 row above is the authoritative
status: **resolved**.

---

## Severity summary

| Sev | Count | Headlines |
|-----|-------|-----------|
| Critical | 4 | Missing runtime modules break execution & imports · position sizing floors to 1 share · safety_rules unenforced · stale-cache fallback raises `AttributeError` |
| High | 6 | Dashboard exposed unauthenticated when `PORT` set · `X-Forwarded-For` spoof bypasses rate limit · execute/paper guardrail inverted · disqualification filters hardcoded · backtest P&L optimistic · Altman Z always "safe" |
| Medium | ~13 | Dead UI-token auth path · CLAUDE.md describes wrong project · pytest hits live network · loose deps · long-only plan for SELL/HOLD · unvalidated exit % · Piotroski normalization · `profit_factor=inf` · non-atomic cache · universe fetch no timeout |
| Low | ~11 | Telegram not HTML-escaped · `/api/diagnose` info leak · sizing divide-by-zero edges · scheduler runs weekends · dead config keys · BB sample std · Ichimoku future leak · gap-scanner short window · shared Session |

---

## CRITICAL

### C1 — Referenced runtime modules are missing; execution and multiple import chains are broken *(verified)*
Three imported modules do not exist on disk or in git:

| Missing module | Imported by |
|---|---|
| `src/alpaca/executor.py` (`execute_trade_plan`, `start_monitor_thread`) | `bot.py:198`, `bot.py:297` |
| `src/alpaca/position_tracker.py` (`PositionTracker`) | `bot.py:55` |
| `src/core/models.py` (`Instrument`, `AssetClass`, `Fundamentals`, `TickerSnapshot`) | `src/signals/context.py:11`, `src/core/contracts.py:9`, `src/data/providers/yfinance_provider.py:7` |

`src/alpaca/` contains only `__init__.py` + `portfolio.py`; `src/core/` only `__init__.py` + `contracts.py`.

**Impact:** Any `bot.py --execute` / `--live-execute` run dies with `ModuleNotFoundError` at the point of placing orders (the import at `bot.py:198` is outside the per-plan try/except, so it also aborts `cmd_scan`). The entire order-placement / stop-attachment / position-monitoring path — the highest-risk, real-money code — has **no source and zero test coverage**. The Phase-3 provider/context chain (`yfinance_provider`, `context`, `contracts`) cannot import at all. The README's "Supports live order execution via Alpaca" is currently false.
**Fix:** Commit the missing modules (with tests), or remove the dead execution wiring and the README claim until the code exists. The safety gate in C2 belongs in the executor.

### C2 — `config.yaml` `safety_rules` are declared but almost entirely unenforced
The only safety rule read anywhere in code is `trade_in_bear_market` (`src/scanner/daily_scan.py:142`). Repo-wide, these are defined and **never referenced**: `max_positions`, `max_daily_loss_pct`, `max_weekly_loss_pct`, `cooldown_after_loss_hours`, `trade_earnings_week`, `stop_loss_required`, `max_trades_per_day`, `cash_reserve_pct`, `max_portfolio_risk_pct`, `max_single_position_pct`, `min_daily_volume`, `paper_trade_days_required`.

**Impact:** There is no daily/weekly loss kill-switch, no max-open-positions cap, no per-day trade cap, no earnings-week block, no loss cooldown, no cash reserve, no portfolio-risk aggregation. `bot.py` `_maybe_execute` (lines ~200-220) iterates over **all** `strong_buys + buys` (up to 10 plans) and would submit an entry for each — directly violating `max_positions: 5` and `max_trades_per_day: 3`.
**Fix:** Implement a pre-trade gate reading `config.SAFETY` that blocks/limits orders before the submit loop (count open positions & trades/day, check realized daily/weekly P&L vs limits, enforce cooldown, skip earnings week, require a resting stop when `stop_loss_required`).

### C3 — Position sizing floors to 1 share, breaching `max_position_pct` *(verified)*
`src/signals/trade_plan.py:119`
```python
shares = max(int(min(shares_by_risk, shares_by_max)), 1)
```
`int()` truncates toward zero, then `max(..., 1)` forces ≥1 share regardless of the risk and max-position caps. When the correctly-sized quantity is < 1 share (expensive stock and/or small account), both terms truncate to 0 and the code buys 1 share anyway.

**Impact:** With `account_size=10000`, `max_position_pct=0.10`, entry $1,500 → `shares_by_max = 1000/1500 → int → 0 → forced 1` → a $1,500 position = **15%** of the account (cap is 10%). At $5,000 entry it is 50%; on a $2,000 account, 250% — exceeding buying power. Neither `shares_by_max` nor buying power is honored once the floor triggers.
**Fix:** `shares = int(min(shares_by_risk, shares_by_max))`; if `shares < 1`, return a "position too small — skip" plan (no order). Never let the final size exceed `shares_by_max`.

### C4 — Stale-cache fallback calls methods that don't exist → `AttributeError` on every provider failure *(verified at runtime)*
`src/data/fetcher.py:824` & `:1093` call `cache.get_stale(...)`; `:1014` calls `cache.get_metadata(..., allow_expired=True)`. Neither exists in `src/data/cache.py` (only `get/set/evict_expired/clear_all/stats`). Runtime check: `hasattr(cache,'get_stale') == False`, `hasattr(cache,'get_metadata') == False`.

**Impact:** `_fallback_to_stale_cache()` is invoked from `fetch_ohlcv()`'s timeout handler (:862), generic-exception handler (:866), and empty/invalid-payload branch (:880) — none wrapped in try/except. So whenever a provider errors or returns an empty frame (the *common* path when Alpaca/Polygon are unconfigured and yfinance is rate-limited, or for a delisted/typo ticker), `fetch_ohlcv` raises `AttributeError` instead of returning empty. `backtest_signal_system` → `_prepare_ticker_history` (:333) has no try/except, so a single bad ticker crashes the whole backtest.
**Fix:** Implement `get_stale(key)` (load pickle ignoring expiry, do not unlink) and `get_metadata(key, allow_expired=True)`. Also: `cache.get()` (cache.py:31-33) unlinks expired files on read, which would defeat the fallback even once implemented — retain expired entries so `get_stale` can find them.

---

## HIGH

### H1 — Dashboard binds `0.0.0.0` but only authenticates under `DASHBOARD_ALLOW_REMOTE` *(verified — corroborated by two independent audits)*
`_runtime_host()` (`dashboard.py:3518-3523`) returns `0.0.0.0` whenever `PORT` is set **or** `DASHBOARD_ALLOW_REMOTE=true`, but the `before_request` guard (`dashboard.py:3629-3634`) only enforces API-key auth when `DASHBOARD_ALLOW_REMOTE` is true.

**Impact:** `PORT=8080 python dashboard.py` with the default `DASHBOARD_ALLOW_REMOTE=false` binds to all interfaces with **no authentication** — the full `/api/scan`, `/api/portfolio`, `/api/scan-ticker`, `/api/clear-cache` (state-changing) surface is public. Any PaaS/container that injects `PORT` triggers it. (Render is safe only because it also sets `DASHBOARD_ALLOW_REMOTE=true`.)
**Fix:** Bind `0.0.0.0` only when `DASHBOARD_ALLOW_REMOTE` is true (which already forces `has_auth_config()`), or require auth whenever the bind host is non-loopback. Fail closed.

### H2 — `X-Forwarded-For` handling is spoofable → rate-limit bypass & API-key brute force *(verified)*
`src/utils/security.py:88-93` — with `DASHBOARD_TRUST_PROXY=true` (set in render.yaml), `resolve_client_ip` trusts the **leftmost** XFF entry (`xff.split(",")[0]`). On Render the platform proxy *appends* the real client IP, so the leftmost value is attacker-controlled.

**Impact:** An attacker sends a different fake `X-Forwarded-For` per request; every request gets its own rate-limit bucket, so `_rate_limiter.allow()` never trips. Since the limiter runs *before* auth and is the only throttle on `DASHBOARD_API_KEY`, this enables unlimited key-guessing plus unthrottled hits on the 60-120s `/api/scan` (DoS amplification). It also lets an attacker forge any victim IP, and grows the limiter's key store unbounded.
**Fix:** Behind exactly one trusted proxy, use the **rightmost** XFF entry (the hop your proxy appended) or a fixed trusted-hop count from the right; validate the result with `ipaddress.ip_address(...)` and bucket malformed values into one key.

### H3 — Execute/paper guardrail is inverted: the only functional order path is real money
`bot.py:195` sets `dry_run = not live_execute`. So `--execute` runs with `dry_run=True` and **never places an order even on paper** — it only prints "would submit". The only path that actually submits (`dry_run=False`) is `--live-execute`, which `bot.py:382` gates to require `ALPACA_PAPER=false` — the **live** account.

**Impact:** There is no way to exercise real order submission against a paper account. A product whose disclaimer is "paper-trade 30 days first" forces users straight to live submission to test execution at all. The paper-safety wording in `--execute`'s help text does nothing.
**Fix:** Make `--execute` submit real orders to the *paper* account (require `ALPACA_PAPER=true`, `dry_run=False`); add a separate `--dry-run` for preview; keep `--live-execute` for live.

### H4 — Disqualification-filter thresholds are hardcoded; `config.yaml filter_*` ignored & inconsistent
`src/signals/filters.py:13-16` hardcodes `MIN_AVG_VOLUME`, `MIN_MARKET_CAP`, `MAX_5DAY_GAIN_PCT`, `BANKRUPTCY_Z`. The `filter_min_avg_volume` / `filter_min_market_cap` / `filter_max_5day_gain_pct` / `filter_bankruptcy_z` keys in `config.yaml` are never read. Values also disagree across the config: filter market-cap 100M vs `safety_rules.min_market_cap` 300M; volume floor 100K vs `safety_rules.min_daily_volume` 200K.

**Impact:** Editing risk thresholds in `config.yaml` silently has no effect; operators believe they raised the market-cap floor to $300M when the real gate is $100M.
**Fix:** Read from `config` (expose `FILTER_*` accessors) and reconcile to one source of truth with `safety_rules`.

### H5 — Backtest P&L is systematically optimistic
`src/backtest/engine.py`: no commission or slippage is applied to any fill (`:118-121`, `:161`, `:253`, `:261`, `:269`, `:277`), despite the documented 0.1% commission / 0.1% slippage. Daily-close stops fill at the exact `pos.stop_price` (`:250-256`) even when the close gapped far below it; targets fill at exact `t1/t2/t3`.

**Impact:** Losses understated (stop gap-through ignored), gross returns inflated (zero frictions). Reported Sharpe/CAGR/profit-factor and the `minimum_acceptance` verdict are all biased upward — the opposite of what you want on a safety gate.
**Fix:** Apply per-side commission + slippage to every fill; fill stops at `min(close_price, stop_price)` (or model gap fills).

### H6 — Altman Z-Score uses `... or 1` denominator → false "safe" classification
`src/fundamental/scorer.py:150` — `tl = fd.get("total_liabilities") or fd.get("total_debt") or 1`, then `x4 = mc / tl` (`:156`). yfinance `.info` rarely provides `totalLiab`, so `tl` frequently becomes `1`; `x4 = market_cap / 1` (billions) dominates the score.

**Impact:** Z-Score inflates to millions → always `> 2.99` → always scored "safe" (+20 in `_composite`), defeating distress detection precisely when balance-sheet data is missing.
**Fix:** Return `None` from `_altman_z` when total liabilities/total debt is unavailable (as it already does for missing `total_assets`), rather than substituting `1`.

---

## MEDIUM

- **M1 — Dead UI-token auth path.** The browser UI sends `X-Dashboard-Token` (`dashboard.py:2068`), but `_extract_supplied_token` (`security.py:96-100`) only reads `Authorization: Bearer` / `X-API-Key`; the signed token is never verified, and `issue_dashboard_ui_token` returns `""` because `SECRET_KEY` is unset in `render.yaml`. Fails closed (not a bypass) but is misleading dead code, and the remote UI can't call `/api/*` without a manual `X-API-Key`. Fix: wire up HMAC+TTL verification and set `SECRET_KEY`, or delete the token machinery.
- **M2 — CLAUDE.md describes the wrong project.** It documents a FastAPI + React/TypeScript + PostgreSQL/SQLAlchemy platform with `/api/v1/*` routes and ORM models — none of which exist. The repo is Flask + yfinance/Alpaca. Every structure/stack/API-path/model/setup command in CLAUDE.md is inaccurate; an agent following it will make wrong changes. Fix: rewrite to match the Flask signal-bot reality.
- **M3 — `generate_trade_plan` builds a LONG setup for SELL/HOLD signals.** `trade_plan.py:82-94` always computes a bullish entry/stop/targets; `scan_single_ticker` (`daily_scan.py:213`) only excludes `ERROR`/`FILTERED_OUT`, so `SELL`/`STRONG_SELL`/`HOLD` get a buy plan. Fix: branch on signal direction or refuse non-BUY.
- **M4 — `target_rr_ratios` / `target_exit_pcts` unvalidated.** `trade_plan.py:94-103` never checks equal lengths or `sum(exit_pcts)==100`, and indexes `raw_targets[1]` unconditionally (`IndexError` on short lists; positions that never fully exit on bad pcts). Fix: validate at load, fail loudly.
- **M5 — Default `pytest` hits the live network.** `tests/test_backtest.py:111,116,125` are `@pytest.mark.integration` and download live yfinance data, but `pytest.ini` has no `addopts` excluding the marker. Fix: `addopts = -m "not integration"`.
- **M6 — `requirements.txt` loosely pinned.** Only `numpy<2` and `yfinance` are bounded; `pandas/flask/gunicorn/scikit-learn/...` float. Deterministic installs rely on remembering `-c constraints.txt`. Fix: point local/CI at `requirements.lock` (as deploy does) or pin directly.
- **M7 — Piotroski composite divides by 9 but two criteria are unattainable.** `scorer.py:214` normalizes by 9, yet F3 (ROA-improving) and F7 (no dilution) can never score because prior-period fields are hardcoded `None` (`fundamental/fetcher.py:89-91`). Max achievable is 7/9 → every ticker underscored. Fix: normalize by measurable criteria or supply prior-period data.
- **M8 — `profit_factor = float("inf")`** (`engine.py:430,445`) when `gross_loss==0` → `json.dumps` emits invalid `Infinity`. Fix: cap at a finite sentinel or `None`.
- **M9 — Disk cache non-atomic / not thread-safe** under the 8-worker batch pool (`cache.py:37-41` write-then-enforce, `:21-34` unlink-on-read-error, `:65-80` eviction). Fix: temp-file + `os.replace()`, guard eviction with a lock.
- **M10 — `universe.py` network reads have no timeout/retry** and depend on fixed table/row positions (`:24` `pd.read_html`, `:47` `pd.read_csv(skiprows=9)`), bypassing the hardened `utils/http.py`. A slow Wikipedia/iShares response can hang the scan. Fix: fetch via `http.get_text` with timeout; select tables by header content.
- **M11 — Backtest same-bar signal→entry + survivorship bias** (`engine.py:368-374`, `:333`): entry fills at the same Close the signal reacts to; universe is the caller's *current* tickers. Both inflate the reported edge. Fix: enter next open; use point-in-time constituents or flag survivorship.
- **M12 — Two scheduled scan times silently dropped.** `bot.py:306-311` wires only `pre_market/market_open/post_market/overnight`; `config.yaml`'s `intraday_1: 11:00` and `intraday_2: 14:00` are never scheduled. Fix: drive the scheduler from the config dict.

---

## LOW

- **L1 — `/api/diagnose`** (`dashboard.py:3649-3656`) returns raw internal diagnostics; return a minimal status instead.
- **L2 — Telegram messages not HTML-escaped** (`telegram_alert.py:30-37`, `parse_mode: HTML`) while `email_alert.py` escapes correctly. Low reachability (values are internal + `TICKER_RE`-validated) but inconsistent; `html.escape()` for defense-in-depth.
- **L3 — Sizing divide-by-zero / negative-account edges** (`trade_plan.py:87,118`; `bot.py:125`): sub-penny `entry_price==0.00` divides by zero; a negative `--account-size` passes through. Guard `entry_price>0` and validate `--account-size>0`.
- **L4 — Scheduler fires weekends/holidays** (`bot.py:315`, `schedule.every().day`) with no market-calendar gate. Skip non-trading days.
- **L5 — Additional dead config keys** never read: `resistance_snap_discount`, `signal_thresholds`, `monitor_interval_min`, `live_position_store`, `ev_win_rate`/`ev_avg_rr` (EV uses hardcoded `WIN_RATE=0.45`/`AVG_RR=2.5` at `trade_plan.py:30-31`).
- **L6 — Bollinger/HV use sample std (ddof=1)** (`volatility.py:27,48`); canonical bands use population std (ddof=0).
- **L7 — Ichimoku `chikou_span` leaks future data** (`trend.py:97`, `shift(-26)`); unused in scoring today but latent lookahead if ever consumed.
- **L8 — Gap scanner labels a "20-day" average computed from as few as 5 bars** (`gap_scanner.py:43,57`); require ≥21 bars.
- **L9 — Single shared `requests.Session`** across all threads (`utils/http.py:29-39`, `lru_cache(maxsize=1)`); rare concurrency races under 8 workers. Use thread-local sessions.
- **L10 — `render.yaml autoDeploy: true`** auto-deploys `master` on every push for a trading system; consider disabling.
- **L11 — Backtest partial-exit that fully closes a position** mislabels exit reason/price as `UNKNOWN`/`close_price` (`engine.py:258-288`); reporting-only (P&L totals unaffected).

---

## What's correct (spot-checked, no action needed)

- **No committed secrets.** `.env` is gitignored and untracked; `.env.example` / `render.yaml` hold only placeholders. Secrets are not logged (telegram/email log only tickers/exceptions).
- **API-key comparison is constant-time** (`secrets.compare_digest`, `security.py:124`); empty tokens rejected first. **No SQL injection** in the SQLite rate limiter (parameterized). **Debug mode off** (`app.run(debug=False)`). No wildcard CORS; header-based auth so the one state-changing endpoint isn't CSRF-exposed. Ticker (`TICKER_RE`) and `account_size` bounds validated; errors are generic (no stack traces to clients).
- **No trade/kill-switch endpoints** exist in the HTTP surface — an unauthenticated caller cannot place Alpaca orders via the dashboard.
- **Indicator math is sound** where present: RSI/ATR Wilder smoothing (`ewm(com=period-1)`), MACD, Stochastic/Williams/CCI, OBV/CMF/AD, pivots (prior bar), RSI-divergence (past bars only). Backtest divide-by-zero guards for Sharpe/Sortino/Calmar/win-rate/drawdown are present. `http.retry_call` backoff is bounded.
- `portfolio.py` is genuinely read-only and routes paper/live via `TradingClient(paper=is_paper)`; `ALPACA_PAPER` defaults to `True`. `bot.py:382` correctly refuses `--live-execute` when `ALPACA_PAPER=true`.
- Offline indicator tests pass (`tests/test_technical.py`: 20/20).

---

## Recommended remediation order

1. **C1 / C4** — restore or remove the missing modules, and implement `cache.get_stale`/`get_metadata`. Nothing downstream is trustworthy while imports fail and data fallbacks crash.
2. **C3 / C2** — fix position sizing (no floor-to-1) and add a real pre-trade safety gate enforcing `safety_rules`.
3. **H1 / H2** — decouple `0.0.0.0` bind from auth (fail closed) and fix `X-Forwarded-For` handling before any public deploy.
4. **H3** — un-invert the execute/paper guardrail so paper submission is testable.
5. **H4 / H5 / H6** — wire filters to config, add backtest frictions/gap-fills, fix the Altman Z denominator.
6. **M2** — rewrite CLAUDE.md to match the real project so future automated changes are grounded in reality.
