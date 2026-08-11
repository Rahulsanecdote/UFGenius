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
- [ ] **P0.2 — Parameter-selection discipline**: coarse grid/search scored
      **only** on validation folds, with an explicit overfitting penalty
      (deflated Sharpe / trial count). Prevents curve-fitting the `config.yaml`
      knobs (`signal_weights`, thresholds, `atr_stop_multiplier`, R:R ladder).
- [ ] **P0.3 — Circuit-breaker completeness**: add a **data-staleness** breaker
      (halt new entries when quotes are stale > N seconds), a **broker-error**
      breaker (halt on repeated broker failures), and a single **global
      "halt trading" switch** surfaced in the dashboard and enforced in
      `RiskGuard`. *(Builds on existing daily/weekly loss kill-switches +
      cooldown.)*
- [ ] **P0.4 — Paper-trading scorecard**: persist every paper decision + realized
      outcome to a ledger and compute the **same validated metrics** live, so
      paper results are directly comparable to the backtest. Upgrade
      `paper_trade_days_required` from a **duration** check to a **performance**
      check.

### P1 — The real-time day-trading core *(the biggest genuine gap)*
- [ ] **P1.1 — Intraday data layer**: 1-/5-minute bars (Alpaca/Polygon) behind
      the existing provider abstraction, with intraday-aware TTL and the
      **look-ahead assertions** adopted from TradingAgents.
- [ ] **P1.2 — Continuous scan loop**: replace the 6 fixed slots with a
      short-interval (≈30–60 s) intraday scanner running the existing
      **pre-market gapper**, **unusual-volume**, and **momentum/breakout**
      scanners on **live intraday bars**, emitting candidates into a queue.
      Rate-limit- and cost-aware.
- [ ] **P1.3 — Intraday signal + entry/exit logic**: VWAP / opening-range /
      relative-volume features, **intraday ATR** stops, and entry triggers tied
      to **breakout confirmation + volume** (not a once-daily composite). Keep
      the deterministic scoring model; add an intraday variant.
- [ ] **P1.4 — Catalyst gating**: a real **earnings calendar** (upgrade the
      best-effort earnings block to calendar-backed) plus optional news /
      Polymarket catalyst tags that **bias or veto** entries — never a naked buy.

### P2 — Execution quality & observability
- [ ] **P2.1 — Execution-quality measurement**: record expected vs. realized fill
      per order → **implementation shortfall / realized slippage**, and feed the
      **measured** slippage back into the backtest cost model so simulated edge
      tracks reality.
- [ ] **P2.2 — Smart order handling**: marketable-limit / peg logic and
      partial-fill handling tuned to measured slippage (extends existing
      cancel-replace stops and partial exits to the **entry** side).
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
