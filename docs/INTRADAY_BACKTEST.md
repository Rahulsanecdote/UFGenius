# Intraday Backtest Harness — Runbook

> The out-of-sample check the **intraday entries** previously lacked. `--mode
> validate` backtests the *daily composite* only; this harness replays the
> deterministic intraday entries — the opening-range **breakout** and the
> **sweep-reclaim** reversal — bar-by-bar on historical intraday bars, with no
> look-ahead and day-trading discipline. **Educational use only. Not financial
> advice. A green verdict here is necessary, not sufficient.**

## What it does

For each ticker it replays one entry over historical intraday bars:

1. **Decision (no look-ahead)** — at each bar T the evaluator sees only bars up
   to and including T. Same bars in, same decision out.
2. **Fill** — a signal on bar T fills at bar **T+1's open**, and only when T+1 is
   in the **same session**. A signal on a session's last bar is dropped — it
   can't be entered and exited the same day.
3. **Manage intrabar** — from the fill bar on, each bar is checked
   **stop-first**: if the bar's low pierced the stop, the stop is assumed to fill
   first (at `min(open, stop)`, so a gap-through fills worse); otherwise targets
   are taken by the bar's high (T1/T2/T3 partials, same geometry as the live plan).
4. **Flat by session end** — anything still open at the session's last bar is
   closed at that close. **No overnight holds** — this is day-trading.

Stop geometry matches the live plans: the breakout stops an ATR-multiple below
entry; the sweep-reclaim stops just below the swept wick (an absolute level). A
degenerate reclaim whose stop sits at/above the fill is skipped — the same guard
`build_sweep_reclaim_plan` applies.

## Usage

```bash
# Breakout entry, default interval (5m), default universe
python bot.py --mode intraday-backtest --entry breakout

# Sweep-reclaim reversal on one ticker, 1-minute bars, a date range
python bot.py --mode intraday-backtest --entry sweep_reclaim --ticker AAPL --interval 1m \
    --start 2024-01-02 --end 2024-02-29

python bot.py --mode intraday-backtest --entry breakout --universe SP500 --json
```

`--entry` is `breakout` or `sweep_reclaim`; `--interval` defaults to
`INTRADAY_DEFAULT_INTERVAL`. Knobs live under `config.yaml` `intraday_backtest:`
(and `INTRADAY_BACKTEST_*` env overrides): `max_lookback_bars` (trailing window
handed to the evaluator each step — must exceed one session + the ATR warmup),
`min_trades` (significance floor), `min_profit_factor`, `max_drawdown_pct`.

## Reading the output

Trade-based metrics come first because they are what validate an *entry*:

| Metric | Meaning |
|---|---|
| **Expectancy (R)** | Average R per trade. **The headline** — positive after costs is the bar to clear. |
| **Profit Factor** | Gross-win R ÷ gross-loss R. > 1 means winners outweigh losers. |
| **Win Rate / Avg Win-Loss (R)** | A low win rate with big winners can still be a strong edge (and vice-versa). |
| **Avg Hold (bars)** | How long trades last — a sanity check that it's really intraday. |
| **Total Return / Max Drawdown** | From a fixed-fractional-risk equity curve (risk `RISK_PER_TRADE` per trade, compounded). |
| **Exit Breakdown** | Counts by STOP / T1 / T2 / T3 / SESSION_CLOSE. |

**Costs dominate tight stops.** With 0.1% commission + 0.1% slippage per side, a
stop on a ~$100 name with a ~$2 risk fills near **−1.2R**, not −1R — the round
trip costs ~0.2R. That is real, and it's why a raw "looks profitable" chart read
is not a backtest. The acceptance check refuses to bless a run with fewer than
`min_trades` trades (default 30) — a handful of trades is noise, not an edge.

## Caveats (read these — they're in `bias_disclosures` too)

- **INTRABAR_ORDERING.** Within one bar the stop is assumed to fill before any
  target. Real sequencing is unknown at bar granularity; this is the
  conservative choice, and finer bars (`--interval 1m`) reduce the uncertainty.
- **NO_CONCURRENCY.** Each trade is sized independently by fixed-fractional risk;
  interaction between simultaneously-open positions is not modeled. This isolates
  the *entry's* edge, which is the point — portfolio construction is a separate
  question (see the daily engine / risk engine).
- **DATA.** Intraday history is short and provider-dependent (yfinance caps 1m at
  ~7 days and 5m at ~60 days), and the tested tickers are the supplied list — so
  survivorship applies and the window may not span multiple regimes. Treat a
  60-day 5m result as suggestive, not conclusive.
- **SESSION_FLAT.** Positions close at the session's last bar; a strategy meant
  to hold overnight is not represented (by design).

## Where it fits

```text
screen → intraday-scan (find/time entries) → intraday-backtest (prove the entry OOS) → paper → (only then) live
```

This is the gate that was missing between the intraday entries and paper trading.
It does not replace paper: a strategy that clears the harness still has to survive
realized paper trades (`/api/paper-scorecard`, `/api/attribution`) before real
money — the harness can't model everything (queue position, partial fills, halts).
