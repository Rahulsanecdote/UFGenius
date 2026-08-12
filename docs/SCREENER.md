# Screener Presets — Runbook

> A screener finds **candidates**, not edge. It turns "scroll through 100s of
> charts every morning" into a short, criteria-matched watchlist. The output is
> a *filter*, not a trade — you feed it into `--mode scan` / `--mode
> intraday-scan`, and (only after `--mode validate` proves an edge out-of-sample)
> anything about money. **Educational use only. Not financial advice.**

## The shipped presets

Config-driven (`config.yaml` `screener.presets`); add/edit freely. The three that
ship match dip-reversal / breakout day-trading:

| Preset | Finds | Criteria |
|---|---|---|
| **oversold-bounce** | Oversold names turning back up on heavy volume (the "bounce play") | price > $5, RSI(14) ≤ 40, rel-volume ≥ 2×, up on the day |
| **ma-bounce** | Pulled back between the 20- and 50-day MA, holding the 20 | price > SMA20 and < SMA50, avg vol ≥ 400k, rel-vol ≥ 1× |
| **breakout** | Strong uptrend making a new 50-day high on real volume | price > SMA20/50/200, new 50-day high, avg vol ≥ 100k, ROE ≥ 20% *(optional)* |

Criteria are checked against fields the bot computes from daily bars — price,
SMA20/50/200, RSI(14), average & relative volume, 50-day high, 1-day change,
market cap. **ROE / debt-equity are optional**: when the free data provider
doesn't supply them the criterion is *skipped* (not failed), because a missing
value must not silently reject every candidate.

## Usage

```bash
# Screen a universe → a ranked watchlist + a ready-to-use CUSTOM_WATCHLIST line
python bot.py --mode screen --preset oversold-bounce                 # scans config's scan_universe
python bot.py --mode screen --preset breakout --universe SP500

# Explain one ticker: does it pass, and if not, which criteria failed?
python bot.py --mode screen --preset ma-bounce --ticker AAPL
```

The universe scan prints matches plus a copy-paste watchlist:

```
CUSTOM_WATCHLIST="AAA,BBB,CCC"
python bot.py --mode scan --universe CUSTOM
python bot.py --mode intraday-scan --universe CUSTOM   # for intraday entries
```

## How it fits the whole loop

```
screen (find candidates)  →  scan / intraday-scan (score + time the entry)  →  validate (prove the edge OOS)  →  paper  →  (only then) live
```

The screener is step one. It is deliberately *not* a signal: passing a preset
means a name is worth *looking at*, nothing more. The bot's signal/entry logic
still has to like it, and — the part no screener video mentions — the strategy
still has to clear `--mode validate` on held-out data before any real money.
Most days, most matches won't be trades. That's the tool working.

## Caveats (read these)

- **A filter is not an edge.** "Made 130% in 2 months with these settings" is an
  unverifiable claim; screening narrows the field, it doesn't confer profit.
- **Data reliability.** yfinance is solid on price/volume/MA/RSI and market cap,
  but spotty on ROE / PEG / short-float — so presets leaning on those are
  partial. (Short-float-based "short play" screens aren't shipped for this
  reason.)
- **Pair with the penny rails.** If you point a screener at low-priced names,
  turn on penny mode (`docs/PENNY_MODE.md`) so the dollar-volume / market-cap /
  bankruptcy hard rails still apply downstream.
