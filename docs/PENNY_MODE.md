# Penny-Stock Mode — Runbook

> ⚠️ **Educational use only. Not financial advice.** Penny/low-float stocks are
> the most manipulated, least liquid, hardest-to-fill corner of the market.
> Automated strategies get picked off there. Treat everything below as a way to
> **discover and validate** candidates under hard safety rails — **not** a
> licence to auto-buy the day's biggest gainers.

Penny mode is an **opt-in, default-off** profile. Turning it on does **not**
remove protection — it swaps the standard disqualifiers for penny-specific
**hard rails**. The whole point is that most of what shows up on a "biggest
daily % gainers" screen still gets **rejected**.

---

## What the hard rails actually do

Enabled via `penny.enabled: true` (or `ALLOW_PENNY_STOCKS=true`). Every value is
config-driven (`config.yaml` `penny:` / `PENNY_*` env) — the defaults are
protective; loosen them at your own risk.

| Rail | Default | Why it exists |
|---|---|---|
| **Dollar-volume floor** | `$3,000,000`/day (price × avg 20-day vol) | The real liquidity gate. Rejects both huge-share sub-penny names **and** high-price zero-volume names that a share count misses — i.e. the ones you could never actually get filled on. |
| **Market-cap floor** | `$50,000,000` (a floor, **not** 0) | Sub-$50M caps are the pump-and-dump zone. The single biggest protection. |
| **Bankruptcy check** | **stays ON** (Altman Z ≥ `filter_bankruptcy_z`) | So a post-bankruptcy ticker (e.g. `BBBY+`) is rejected, not waved through. |
| **Price band** | `$0.50`–`$10.00` | Sub-$0.50 is the widest-spread / most-manipulated zone; the ceiling keeps the profile in its lane. |
| **Chaser-trap** | `30%` / 5 days (vs 50% standard) | Don't buy *after* the pop. |
| **Share-volume floor** | `100,000`/day (unchanged, not relaxed) | Basic liquidity hygiene. |
| **Unknown market cap** | disqualified | If the cap can't be verified, it isn't traded blind. |

### Worked example — the "Daily price jumps" screen

Run against a typical gainers list, the rails reject essentially all of it:

| Ticker | Rejected for |
|---|---|
| OFAL ($1.79, cap $1.4M) | `MICRO_CAP` |
| CSXXY ($41.88, 0 vol) | `ABOVE_PRICE_BAND`, `ILLIQUID`, `THIN_DOLLAR_VOLUME` |
| BBBY+ ($0.42, cap —) | `PENNY_STOCK`, `THIN_DOLLAR_VOLUME`, `BANKRUPTCY_RISK`, `UNKNOWN_MARKET_CAP` |
| CHOW ($0.48, cap $12M) | `PENNY_STOCK`, `MICRO_CAP` |
| A genuinely liquid $2.50 / 4M-share / $120M-cap name | **passes** (until it's up >30% in 5 days) |

---

## How to run it (scan / paper only)

1. **Enable + pick a universe.** In `config.yaml`:
   ```yaml
   scan_universe: CUSTOM
   penny:
     enabled: true
     custom_watchlist: "BOXL,HYLN,DOGZ,APYX"   # or set CUSTOM_WATCHLIST env
   ```
   (Env equivalents: `ALLOW_PENNY_STOCKS=true`, `CUSTOM_WATCHLIST="BOXL,HYLN,..."`.)

2. **Scan — no orders.** Discovery + scoring only:
   ```bash
   python bot.py --mode scan                # daily-bar scan over the watchlist
   python bot.py --mode intraday-scan       # continuous intraday candidate loop (P1.2/1.3)
   python bot.py --mode scan --ticker BOXL  # single name: score + why it passed/failed
   ```
   Both paths enforce the hard rails in penny mode: `--mode scan` runs them in
   the daily signal generator; `--mode intraday-scan` runs them as an
   **eligibility gate** in the consumer (on a daily frame + fundamentals, since
   intraday bars can't measure daily liquidity or market cap) **before** any
   entry plan is emitted, and **fails closed** — a name whose eligibility can't
   be verified is skipped, not traded blind.

3. **Size tiny.** Penny names gap and halt. Keep `account_size` realistic and
   `risk_per_trade` / `max_position_pct` small; `RiskGuard` still enforces the
   per-trade risk, single-position, cash-reserve, and loss kill-switch rules.

## Before any real money — validate first

The rails keep you out of obvious traps; they do **not** prove the strategy has
an edge on this universe. Prove it out-of-sample first — validation honors your
configured `scan_universe`, so with `scan_universe: CUSTOM` this validates the
**penny watchlist** (pass `--universe CUSTOM` explicitly to be sure):

```bash
python bot.py --mode validate --universe CUSTOM --start 2022-01-01 --end 2023-12-31
```

A `validated` verdict requires clearing the OOS Sharpe floor, the bootstrap
CIs, and a minimum OOS sample (`config.yaml` `validation:`). **If it doesn't
validate, the correct outcome is not to deploy capital** — not to loosen the
rails. Live execution (`--execute` / `--live-execute`) is a separate, explicit
step gated by `RiskGuard` and the P0.4 paper scorecard; do not flip it on for a
penny universe that hasn't cleared validation, and paper-trade it first.

> **Data caveat:** for delisted/illiquid penny names the backtest depends on the
> provider having history — yfinance drops many — so a penny validation may run
> on a survivorship-biased subset. Read the verdict with that in mind.

## Mechanics penny traders hit (not solved by config)

- **PDT rule** — under a *margin* account, FINRA's pattern-day-trader rule caps
  you at 3 day-trades / 5 business days below $25K equity. It's **broker- and
  account-type dependent** (cash accounts settle instead of counting day-trades,
  with their own settlement constraints) — check your broker and current
  [FINRA guidance](https://www.finra.org/investors/investing/investment-products/stocks/day-trading).
- **Halts & gaps** — LULD halts and overnight gaps blow through stops.
- **Data quality** — free-provider quotes on sub-$1 names are often stale; the
  P0.3 data-staleness breaker will (correctly) refuse entries built on them.
- **Borrow** — shorting most of these is hard-to-borrow or impossible.
