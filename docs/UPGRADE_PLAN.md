# UFGenius — Upgrade Plan to a Production-Grade Autonomous Day-Trading Platform

> **Status:** proposal / working plan. Checkboxes track delivery.
> **Objective:** maximize **risk-adjusted** performance and build a system whose
> edge can be **measured and validated before real capital is deployed.**
> No strategy here is assumed to be profitable — the goal is a measurable,
> validated edge and hard safety rails.
>
> ⚠️ Educational use only. **Not financial advice.** Paper-trade and validate
> out-of-sample before risking real money.

This plan came out of a deep comparison between UFGenius and
[TauricResearch/TradingAgents](https://github.com/TauricResearch/TradingAgents).
The comparison context is summarized below so the plan is self-justifying; the
actionable work is the phased checklist in [The Plan](#the-plan-p0--p3).

---

## Framing: two different classes of system

- **TradingAgents** is an **LLM multi-agent research/reasoning framework**
  (LangGraph). For *one ticker, one date* it runs analyst agents
  (market/social/news/fundamentals) → a bull/bear debate → a research-manager
  judge → a trader agent → a 3-way risk debate (aggressive/conservative/neutral)
  → a portfolio-manager judge → a **5-tier text rating**. ~16–22 LLM calls and
  minutes per ticker. It **never places an order**, has **no scanner/scheduler**,
  and its "risk management" is **role-play prose with zero numeric gates**.
- **UFGenius** is a **quantitative scanner + risk-gated execution bot**: it scores
  an equity universe on a deterministic weighted composite and actually places
  risk-gated Alpaca orders with stops, partial exits, and a monitor loop.

They win different columns; neither dominates. The valuable transfer is mostly
TradingAgents' **ideas → our engine**, not its architecture.

**Honest headline for the day-trading goal:** *neither system is a day-trading
platform today.* UFGenius's signals and backtest run on **daily bars** with
**6 scheduled scans/day** — that is swing/position trading in intraday clothing.
Real autonomous day trading needs an intraday-data + real-time-loop foundation
that does not exist yet. That reality drives the phase ordering below.

---

## What each system does better

### TradingAgents does better
- **Anti-hallucination data discipline** — a deterministic *verified snapshot*
  the LLM must treat as canonical and flag conflicts against; typed vendor
  routing that refuses silent fallback and emits a `NO_DATA_AVAILABLE` sentinel
  instead of fabricating.
- **Look-ahead hygiene as a first-class concern** — `Date <= curr_date` cutoffs,
  inclusive-end fix, stale-frame rejection, fiscal-period filtering.
- **Multi-provider LLM abstraction** — a declarative per-model *capability table*
  (tool-choice / JSON-mode / reasoning quirks), each entry citing a real bug.
- **Explainability** — the bull/bear + risk debate produces a readable *why*
  that our numeric composite score lacks.
- **Novel forward-looking catalyst source** — Polymarket prediction-market
  implied probabilities; plus StockTwits/Reddit sentiment inputs.

### UFGenius does better
Essentially everything that makes it a *trading* system:
- Real universe **scanners** (parallel scan, gap scanner, unusual-volume/breakout).
- **Deterministic, cheap, reproducible** composite signals (vs. non-deterministic
  LLM ratings).
- A real **backtest engine**: next-bar-open fills, commission + slippage,
  stop-gap-through, Sharpe/Sortino/Calmar, and a point-in-time survivorship fix.
- Actual **broker execution** (Alpaca paper/live) with partial exits (T1/T2/T3)
  and cancel-replace stops.
- **Hard-coded `RiskGuard`** numeric gates: daily/weekly realized-loss
  kill-switches, post-loss cooldown, position/exposure caps, earnings-week block,
  stop-required, paper-trading tenure. **TradingAgents has none of these.**
- Fast, auditable, and orders of magnitude cheaper to run.

---

## Ideas to adopt vs. skip

| Adopt | Rationale |
|---|---|
| ✅ Verified-snapshot / ground-truth guardrail | For any LLM-facing surface we add; compute canonical numbers, force the model to flag conflicts. |
| ✅ Typed data-source failure semantics | No silent fallback; explicit "no data" sentinel instead of empty strings. Port into `data/providers`. |
| ✅ Look-ahead assertions as code | Stale-frame rejection + as-of clamps in backtest and live fetch. |
| ✅ Polymarket + StockTwits catalyst/sentiment inputs | Cheap, additive, forward-looking — kept as **soft** signals only. |
| ⚠️ Adversarial bull/bear + risk narrative | Adopt **only** as an optional, non-gating explanation layer — never the decision authority. |

| Skip | Rationale |
|---|---|
| ❌ LLM debate as a decision engine | Non-deterministic, slow, expensive, un-backtestable, prompt-injectable, no hard risk gates. |
| ❌ `backtrader` / `redis` heavy deps | In TradingAgents these are **declared but unused**; we already have a working backtest. |
| ❌ Vector-DB "memory" / self-reflection journals | Marketed as learning; mechanically a re-read notepad with manual return feeding. |
| ❌ More indicators for their own sake | Breadth is not the bottleneck; validation is. |

---

## Day-trading performance vs. complexity

The filter that matters most — what actually moves risk-adjusted performance
versus what only adds surface area.

| Actually improves risk-adjusted day-trading | Adds complexity without clear payoff |
|---|---|
| Walk-forward + OOS + bootstrap CIs on the backtest (prove the edge) | Full LLM multi-agent debate as decision authority |
| Intraday bars (1m/5m) + real-time scan loop | Vector-DB "memory" / self-reflection journals |
| Realized slippage / implementation-shortfall tracking | `backtrader`/`redis`-scale dependencies |
| Earnings/catalyst calendar gating | More indicators for their own sake |
| Observability: trade-outcome ledger + kill-switch state | Multi-provider LLM plumbing (unless an LLM layer is added) |
| Circuit breakers on data-staleness & broker errors | Prediction-market data as a *hard* signal (keep it soft) |

---

## The Plan (P0 → P3)

**Organizing principle:** *measure and validate the edge before real capital.*
P0 is validation + safety, then the real-time core, then quality/observability,
then an optional intelligence layer. Every phase makes the edge more
**measurable**; none assumes profit.

### Sequencing rules (gates between phases)
- [ ] Do **not** build P1's real-time core until P0 shows the daily-bar edge is
      real on **out-of-sample** data — scaling an unproven signal to higher
      frequency is punished by costs and noise.
- [ ] Do **not** flip the live-trading flag until the P0 **paper scorecard**
      matches the **validated backtest** within an agreed tolerance.

### P0 — Prove the edge & harden the gates *(do first; leverages what exists)*
- [x] **P0.1 — Walk-forward + out-of-sample harness** around the existing
      backtest engine: rolling train/validate windows, a held-out final period
      never used for tuning, and a **bootstrap/monte-carlo** pass resampling
      trades to produce **confidence intervals** on Sharpe / profit-factor /
      max-drawdown. Make `minimum_acceptance` run on **OOS only**.
      *(Highest-value item — converts "reported metrics" into "validated edge".
      Moderate effort: engine, cost model, and survivorship fix already exist.)*
      **Delivered:** `src/backtest/validation.py` (`validate_strategy`,
      `walk_forward`, `bootstrap_trade_metrics`, `bootstrap_return_metrics`) +
      `python bot.py --mode validate` (flags `--windows/--bootstrap/--oos-fraction/--seed`).
      Verdict is `validated` only when the edge clears the OOS Sharpe floor, the
      OOS minimum-acceptance gate, a bootstrap 5th-pct Sharpe > 0,
      `prob_profitable ≥ 0.60`, and persistence across a majority of
      walk-forward windows, **and a minimum OOS sample size** (so a lucky
      handful of trades can't validate). Thresholds are config-driven
      (`config.yaml` `validation:`); seeded/reproducible; 18 offline tests
      (`tests/test_validation.py`).
- [x] **P0.2 — Parameter-selection discipline**: coarse grid/search scored
      **only** on validation folds, with an explicit overfitting penalty
      (deflated Sharpe / trial count). Prevents curve-fitting the `config.yaml`
      knobs (`signal_weights`, thresholds, `atr_stop_multiplier`, R:R ladder).
      **Delivered:** `src/backtest/optimize.py` (`grid_candidates`,
      `expected_max_sharpe`, `parameter_search`) + `python bot.py --mode optimize`.
      The engine strategy is now parameterized (`StrategyParams` +
      `strategy_params(...)` context manager in `src/backtest/engine.py`), so a
      grid actually varies entry band / stop / target / sizing. Selection scores
      every candidate by walk-forward on the **in-sample span only**, ranks by
      mean fold Sharpe, then applies the Bailey & López de Prado
      **expected-maximum-Sharpe-under-the-null** haircut for the trial count and
      **re-confirms the winner on the untouched OOS tail** (bootstrap CIs + the
      P0.1 gates). `selection.trustworthy` is True only when the winner beats the
      false-strategy threshold **and** validates out-of-sample — otherwise the
      honest verdict is *do not deploy*. Grid size is capped (`MAX_CANDIDATES`)
      because a wide search overfits by construction; seeded/reproducible; 9
      offline tests (`tests/test_optimize.py`).
- [x] **P0.3 — Circuit-breaker completeness**: add a **data-staleness** breaker
      (halt new entries when quotes are stale > N seconds), a **broker-error**
      breaker (halt on repeated broker failures), and a single **global
      "halt trading" switch** surfaced in the dashboard and enforced in
      `RiskGuard`. *(Builds on existing daily/weekly loss kill-switches +
      cooldown.)*
      **Delivered:** `src/alpaca/circuit_breaker.py` (`CircuitBreaker`,
      JSON-persisted with atomic writes, shared across the dashboard and CLI
      processes). Three breakers block **new entries only** (never exits, so a
      halt can't strand an open position without its stop), checked
      highest-priority-first as check #0 in `RiskGuard`: (1) an operator
      **global halt** flipped from the dashboard; (2) a **broker-error** breaker
      that trips after N failures within a rolling window (the execution path
      records failed portfolio reads and order submits; it clears as errors age
      out, deliberately *not* on a single success); (3) a **data-staleness**
      breaker — every plan carries `quote_as_of` (its latest-bar timestamp) and
      entries built on data older than the limit are refused (unknown age fails
      open). Dashboard: `GET /api/breaker-state`, `POST /api/breaker`
      (halt/resume), and a "Circuit Breakers & Kill Switch" panel. Thresholds
      are config-driven (`config.yaml` `circuit_breakers:` + `CIRCUIT_*` env);
      19 offline tests (`tests/test_circuit_breaker.py` + dashboard route tests).
- [x] **P0.4 — Paper-trading scorecard**: persist paper entry decisions and
      realized outcomes, then compute the **same validated metrics** live so
      paper results are directly comparable to the backtest. The scorecard
      outcome ledger holds one record per fully-closed *filled* position
      (unfilled/expired entries are excluded). Upgrade `paper_trade_days_required`
      from a **duration** check to a **performance** check.
      **Delivered:** the position tracker now writes a per-**trade** outcome
      ledger (one record per fully-closed *filled* position — ticker, signal,
      score, entry/exit, realized P&L, return %) alongside its per-exit realized
      ledger, so paper metrics line up with the backtest's trade-level metrics.
      `src/alpaca/scorecard.py` computes win rate / profit factor / expectancy /
      `total_return` plus a bootstrap `prob_profitable` **via the same
      `bootstrap_trade_metrics` estimator the P0.1 validation uses** — a
      like-for-like paper-vs-backtest comparison. RiskGuard's live gate now
      requires **both** `paper_trade_days_required` tenure **and** the paper
      scorecard clearing configured floors (min trades, profit factor, bootstrap
      prob-profitable, positive expectancy) before real money — a performance
      gate, not just elapsed time. Surfaced via `GET /api/paper-scorecard` + a
      dashboard panel and `python bot.py --mode portfolio`. Config-driven
      (`config.yaml` `paper_scorecard:`); 15 offline tests
      (`tests/test_scorecard.py`).

### P1 — The real-time day-trading core *(the biggest genuine gap)*
- [x] **P1.1 — Intraday data layer**: 1-/5-minute bars (Alpaca/Polygon) behind
      the existing provider abstraction, with intraday-aware TTL and the
      **look-ahead assertions** adopted from TradingAgents.
      **Delivered:** `fetch_intraday()` (`src/data/fetcher.py`) — the clean
      intraday entry point for the real-time layer — fetches 1m/5m/… bars
      through the existing Alpaca→Polygon→yfinance abstraction with an
      **interval-scaled cache TTL** (`_ttl_for_interval`: a 5m bar caches for
      ~5m, not the daily default), then applies the P1.1 **look-ahead guards**
      in `src/data/lookahead.py` (`sort_dedupe`, `drop_future_bars`, `as_of`
      as-of clamp, `is_stale`/`bar_age_seconds` stale-frame detection). Pure,
      deterministic guards reusable by the fetch path, backtest as-of reads, and
      the live loop. Config-driven (`config.yaml` `intraday:` + `INTRADAY_*`);
      19 offline tests (`tests/test_lookahead.py`, `tests/test_intraday_fetch.py`).
- [x] **P1.2 — Continuous scan loop**: replace the 6 fixed slots with a
      short-interval (≈30–60 s) intraday scanner running the existing
      **pre-market gapper**, **unusual-volume**, and **momentum/breakout**
      scanners on **live intraday bars**, emitting candidates into a queue.
      Rate-limit- and cost-aware.
      **Delivered:** `ContinuousScanner` (`src/scanner/intraday_scan.py`) loops
      on a floored short interval during market hours, scoring live intraday
      bars (via P1.1 `fetch_intraday`) for **unusual volume / momentum /
      breakout** (breakout requires volume participation — a fakeout guard) and
      pushing hits into a thread-safe, bounded, de-duplicating `CandidateQueue`
      (`src/scanner/candidate_queue.py`) for the P1.3 consumer to drain.
      Rate/cost-aware: universe capped per cycle, bars served from the
      interval-scaled cache, interval floored, market-hours gated. Run it with
      `python bot.py --mode intraday-scan`. Discovery only — sizing/gating/orders
      stay in P1.3 + RiskGuard. Config-driven (`config.yaml` `continuous_scan:`);
      18 offline tests (`tests/test_candidate_queue.py`, `tests/test_intraday_scan.py`).
- [x] **P1.3 — Intraday signal + entry/exit logic**: VWAP / opening-range /
      relative-volume features, **intraday ATR** stops, and entry triggers tied
      to **breakout confirmation + volume** (not a once-daily composite). Keep
      the deterministic scoring model; add an intraday variant.
      **Delivered:** `src/technical/intraday_features.py` (session-anchored VWAP,
      opening range, relative volume, intraday ATR — pure, sliced to the latest
      session so they don't bleed across days). `src/signals/intraday_signal.py`
      — `evaluate_intraday_entry` is a **deterministic** trigger: STRONG_BUY on
      VWAP-hold + opening-range breakout + volume confirmation, BUY on VWAP +
      volume without the ORB, else HOLD; `build_intraday_plan` reuses
      `generate_trade_plan` with the **intraday frame**, so the stop is sized off
      intraday ATR and sizing/targets/RiskGuard gating are shared with the daily
      path. `src/scanner/intraday_consumer.py` — `IntradayConsumer` drains the
      P1.2 queue (per-cycle cap), evaluates each candidate on fresh intraday
      bars, and emits entry plans to a pluggable sink (default: log). Wired into
      `python bot.py --mode intraday-scan` as producer→consumer (discovery +
      planning; no orders placed — execution reuses the gated executor once the
      edge is validated). Config-driven (`config.yaml` `intraday_signal:`); 23
      offline tests (`tests/test_intraday_features.py`,
      `tests/test_intraday_signal.py`, `tests/test_intraday_consumer.py`).
- [x] **P1.4 — Catalyst gating**: a real **earnings calendar** (upgrade the
      best-effort earnings block to calendar-backed) plus optional news /
      Polymarket catalyst tags that **bias or veto** entries — never a naked buy.
      **Delivered:** `src/catalysts/earnings_calendar.py` — an `EarningsCalendar`
      (`{ticker: date}` JSON file, auditable and pre-buildable via
      `python bot.py --mode earnings-calendar`, with a per-ticker yfinance
      fallback). RiskGuard's earnings-week block now reads `days_to_earnings`
      from it (via the upgraded `_lookup_days_to_earnings`), so the block is
      calendar-backed instead of a single best-effort field.
      `src/catalysts/catalyst_gate.py` — a deterministic `CatalystGate` that
      **vetoes** an entry carrying a hard catalyst tag (trading halt / fraud /
      SEC investigation / going-concern / bankruptcy). Tags ride on the plan
      (`catalyst_tags`), so any upstream news/insider/prediction-market source
      can attach them without a network dependency here; wired as a RiskGuard
      gate (config `catalysts.enable_catalyst_gate` + `veto_tags`). Config-driven
      (`config.yaml` `catalysts:`); 18 offline tests
      (`tests/test_earnings_calendar.py`, `tests/test_catalyst_gate.py`, +
      RiskGuard veto tests). **P1 real-time core complete.**

### P2 — Execution quality & observability
- [x] **P2.1 — Execution-quality measurement**: record expected vs. realized fill
      per order → **implementation shortfall / realized slippage**, and feed the
      **measured** slippage back into the backtest cost model so simulated edge
      tracks reality.
      **Delivered:** `src/alpaca/execution_quality.py` — an `ExecutionQualityLedger`
      (JSON-persisted, atomic, bounded) records every executor fill's expected vs
      realized price as signed **adverse slippage (bps)** and **implementation
      shortfall ($)**. Wired into the executor at all three fill points (entry
      limit, stop, target). `measured_slippage_pct()` averages the adverse
      slippage (floored at 0, gated on a minimum fill count) and the backtest
      engine consumes it when `execution_quality.use_measured_slippage` is on —
      `cost_model.slippage_source` reports `measured` vs `configured`, so
      simulated frictions track reality instead of a static guess. Surfaced via
      `GET /api/execution-quality` and `python bot.py --mode portfolio`.
      Config-driven (`config.yaml` `execution_quality:`); 15 offline tests
      (`tests/test_execution_quality.py`).
- [x] **P2.2 — Smart order handling**: marketable-limit / peg logic and
      partial-fill handling tuned to measured slippage (extends existing
      cancel-replace stops and partial exits to the **entry** side).
      **Delivered:** `src/alpaca/smart_orders.py` prices the entry as a
      **marketable limit** — crossing the market by a small offset **tuned to the
      P2.1 measured slippage**, clamped to a floor (so the limit is genuinely
      marketable) and a hard cap (so a bad measurement or a fast market can never
      make it chase further than configured). `execute_trade_plan` submits at
      that price while keeping the plan's price as the accounting benchmark, so
      the execution-quality ledger still measures shortfall against what was
      *intended*. Config-gated and **default off** (`smart_orders.enabled`), so
      it's a no-op until turned on. Config-driven (`config.yaml` `smart_orders:`);
      12 offline tests (`tests/test_smart_orders.py`).
      *(Marketable-limit re-submission of an unfilled partial-entry remainder —
      full cancel-replace on the entry side — is the remaining sub-item, deferred
      as a money-path lifecycle change to add after the edge is validated; the
      existing partial-entry path already safely protects the filled shares.)*
- [ ] **P2.3 — Observability stack**: structured metrics (scan latency, signal
      counts, fill quality, data-staleness), a **trade-outcome ledger** with
      per-signal attribution, and dashboard panels for **kill-switch and
      circuit-breaker state**, with alerting on breaker trips and data gaps.
      *(Builds on `/api/diagnose`.)*

### P3 — Optional intelligence layer *(only after P0–P2 pay off)*
- [ ] **P3.1 — Explainability layer, not a decision layer**: an *optional* LLM
      narrative that reads the **verified snapshot** (adopt the ground-truth
      guardrail) and the quant signal to produce a human-readable bull/bear
      rationale for the dashboard/alerts. It **must never gate or place orders**,
      must be **sandboxed against prompt injection** (quarantine raw news/social
      text), and must be **cost-capped**. Captures TradingAgents' one real edge
      (explainability) without its fatal flaws.

---

## Acceptance criteria (definition of "production-grade")
- [ ] Edge is demonstrated on a **held-out out-of-sample** period, with
      confidence intervals — not just in-sample metrics.
- [ ] Live **paper** performance matches the validated backtest within tolerance
      before any real-money flag is enabled.
- [ ] Every entry passes `RiskGuard`; **kill-switches and circuit breakers** are
      observable and enforced.
- [ ] **Realized** slippage/cost is measured and reconciled against the model.
- [ ] Full observability: trade-outcome ledger, data-staleness and breaker
      alerting.

---

## Known non-goals / explicit risks
- This plan does **not** claim any configuration is profitable. If P0 cannot
  demonstrate a validated out-of-sample edge, the correct outcome is **not to
  deploy capital**, not to add more features.
- Higher frequency amplifies cost and noise sensitivity; P1 must not precede P0.
- Any LLM layer (P3) is advisory only and is a prompt-injection surface — it is
  firewalled from execution by design.
