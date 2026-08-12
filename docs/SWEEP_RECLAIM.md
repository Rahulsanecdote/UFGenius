# Sweep-Reclaim Reversal Entry — Runbook

> A **liquidity-grab reversal** entry: buy the moment a fake breakdown fails.
> It is the counterpart to the momentum breakout — instead of buying strength,
> it buys the reclaim after price dips below an obvious low, grabs the stops
> resting there, and snaps back above. **Default OFF. A timing hypothesis, not a
> proven edge — validate out-of-sample before real money. Not financial advice.**

## The setup

The pattern this fires on, on the **current session's** intraday bars:

1. **Swing low** — the lowest low over the `lookback_bars` (default **15**) that
   *precede* the recent window. This is the visible level where stop-loss orders
   cluster ("liquidity below the low").
2. **Sweep** — within the last `reclaim_window_bars` (default **2**) bars, price
   dips *below* that swing low. A wick pierces it and grabs the resting stops.
3. **Reclaim** — the current bar closes back *above* the swept level. The
   breakdown failed; the sellers who pushed it under are now trapped.
4. **Volume** — the reclaim carries above-average participation
   (`min_rel_volume`, default **1.5×** the session average).

| Confirmed | Signal |
|---|---|
| sweep + reclaim + volume | `STRONG_BUY` |
| sweep + reclaim, thin volume (and `require_volume: false`) | `BUY` |
| anything less, or an over-extended reclaim | `HOLD` (no entry) |

**Over-extension guard.** If the reclaim close is already more than
`max_reclaim_extension_pct` (default **5%**) above the swept level, it's `HOLD` —
entering there means chasing, with the swept-low stop now too far below to give a
sane reward:risk.

## The stop is the whole point

The stop sits just **below the swept wick** (`stop_buffer_pct` below the sweep
low, default 0.1%). The logic is exact: the setup's premise is that the low was
swept and *rejected*; if price falls back through that low, the premise is wrong
and you're out. That swept-low stop — not a generic ATR distance — drives the
position size and the R-multiple targets, through the same
`generate_trade_plan` → RiskGuard money-path every other entry uses. Nothing here
sizes or places an order directly.

## Enabling it

Off by default. Turn it on in `config.yaml`:

```yaml
sweep_reclaim:
  enabled: true                # the consumer only evaluates this path when true
  lookback_bars: 15
  reclaim_window_bars: 2
  min_rel_volume: 1.5
  require_volume: false        # true = no entry at all without volume confirmation
  stop_buffer_pct: 0.001
  max_reclaim_extension_pct: 0.05
  min_session_bars: 20
```

or via env (`SWEEP_RECLAIM_ENABLED=true`, `SWEEP_LOOKBACK_BARS=15`, …). Every knob
has a `SWEEP_*` override in `src/utils/config.py`.

## How it fits the pipeline

```text
intraday-scan (produce candidates) → consumer drains the queue →
   breakout entry?  → plan
   else, if sweep_reclaim.enabled → sweep-reclaim reversal entry? → plan
        → sink (log by default; execution reuses the gated execute_trade_plan path)
```

The **producer** (`ContinuousScanner`) enqueues a structural `sweep` candidate
whenever a sweep + reclaim is present on a scanned frame (a strict superset of the
graded entries, so a thin-volume `BUY` or a sub-threshold reclaim still reaches
the consumer). The **consumer** (`IntradayConsumer`) evaluates the momentum
**breakout** first; only when that doesn't fire *and* `sweep_reclaim.enabled` is
true does it run the full reversal grading. A breakout (momentum) and a
sweep-reclaim (reversal) are distinct setups, so at most one plan is emitted per
ticker per drain. Both the producer's `sweep` candidate and the consumer's
reversal path are gated by the same flag — with `enabled: false` the intraday
path is byte-for-byte unchanged.

## Caveats (read these)

- **A pattern is not an edge.** The stop-hunt-reversal is a real market
  microstructure phenomenon, but "it looks clean on the chart" is not a backtest.
- **There is no automated validation for this entry yet.** `--mode validate`
  backtests the **daily composite** strategy (`src/backtest/engine.py`); it does
  **not** run `evaluate_sweep_reclaim`, so a passing `validate` verdict says
  nothing about this timing hypothesis. Treat the sweep-reclaim entry as
  **unvalidated**: keep it on `scan`/paper, and judge it on realized paper trades
  via the paper scorecard (`/api/paper-scorecard`) and per-signal attribution
  (`/api/attribution`) before ever pointing real money at it. An intraday
  backtest harness is a known gap (shared by every intraday entry, incl. the
  breakout).
- **Reversal ≠ falling knife protection.** The reclaim confirmation is what
  separates this from catching a knife — but a low that gets swept can simply keep
  going. The tight swept-low stop is what bounds that; respect it.
- **Bar size matters.** Defaults are tuned for the intraday scan's bar size
  (`continuous_scan.interval`, default 5m). On a much finer/coarser interval,
  re-tune `lookback_bars` / `reclaim_window_bars` so the swing low is meaningful.
- **Pair with penny rails.** Pointed at low-priced names, keep penny mode on
  (`docs/PENNY_MODE.md`) — the consumer's dollar-volume / market-cap / bankruptcy
  hard-rail eligibility gate still runs upstream of every intraday plan.
