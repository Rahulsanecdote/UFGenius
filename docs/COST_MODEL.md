# Cost model: what the measurement says

The backtest and the precursor's cost floor both read
`commission_pct + slippage_pct` from `config.yaml` — `0.001 + 0.001` per side,
so **0.400% round trip**. That number was never measured. This is the
measurement, and the decision it forces.

Raw sample: [`spread_sample_2026-09-28.json`](spread_sample_2026-09-28.json) —
29 quotes captured 2026-09-28 at 09:52 ET (~22 minutes after the open) from
Robinhood's `bid_price`/`ask_price`, with 30-day average volume for 20 of them.
Its `provenance.known_limits` block lists what the sample cannot support; read
that before quoting any figure here.

The quantity compared is the **full quoted spread as a percent of mid**, which
is the round-trip crossing cost (you pay half the spread against mid on each
side). Commission is excluded because **Alpaca is commission-free** — the
model's 0.100%/side commission leg is fictional for the configured broker, and
it is half the model.

## The model is wrong in both directions

| Band (30-day avg dollar volume) | n | median spread | worst | vs the 0.400% model |
|---|---|---|---|---|
| ≥ $500M/day | 12 | **0.0286%** | 0.2385% (LLY) | 1.7–68× **too punitive** |
| $50M–$500M/day | **0** | — | — | no observations |
| < $50M/day | 8 | **1.6155%** | 3.9216% (CHRN) | up to 10× **too generous** |

Extremes: AAPL 0.0059% (the model charges **68×** the real cost);
CHRN 3.9216% (the model charges **a tenth** of it).

So a single flat number cannot serve this system. It made the precursor's
mega-cap test meaningless *and* it would make any microcap backtest read as
free money.

## Dollar volume predicts it; price does not

Spearman rank correlation against measured spread %, n=20:

- **dollar volume: −0.697**
- **price alone: +0.079** — noise

That kills the intuitive fix. A price-tiered cost model ("penny stocks cost
more") would be worthless, and it vindicates the belief penny mode already
encodes — that price × volume, not price, is the real liquidity gate.

Dollar volume is a predictor, not a determinant, and the residuals have a
cause a cost model cannot see:

- **ADRX** — $40.0M/day and a **3.00%** spread. Its 52-week high is 2026-09-25
  and its low 2026-09-24: it listed about four days ago, so its book has no
  depth history regardless of turnover.
- **CHRN** — float 1,275,052 shares against 145.6M outstanding, i.e. **0.9%
  floated**, incorporated 2026-06-18. Same story.
- **HEPS** — the reverse: $1.4M/day yet only 0.396%. Thin turnover does not by
  itself imply a wide book.

## The decision this forces

Correcting the cost does **not** by itself rescue the precursor, and which way
it lands depends entirely on a choice the sample cannot make.

The A/B on 15 cached frames measured a **median risk/cost ratio of 0.20×**
against the floor's 2× requirement, under the 0.400% assumption. Rescaling that
same set of stops to a corrected cost:

| Liquid-band cost used | round trip | median risk/cost | floor (2×) |
|---|---|---|---|
| worst observed (LLY) | 0.2385% | **0.32×** | still refused |
| median observed | 0.0286% | **2.80×** | clears |

An **8.3× swing on one name.** LLY is a $1,186 stock with 2.4M shares/day: its
$2.83 spread is 283 ticks. AAPL's round trip is 0.0059% and LLY's is 0.2385% —
**40× apart in the same band** — which is the actual lesson. The error is not
the value of the tier; it is that there is a tier at all.

**Cost has to be per-symbol.** Two routes, and they are not interchangeable:

1. **Live path** — read the real quote at decision time. Cheap and exact.
2. **Backtest** — a quote is not in an OHLCV bar, so the spread must be
   *estimated* from bars (Corwin–Schultz or Abdi–Ranaldo both estimate the
   effective spread from high/low/close). This is the part that needs a
   deliberate choice, because CLAUDE.md's rule for the floor is that it lives
   in the evaluator precisely so the backtest and the live path cannot disagree
   about what a trade is — and a live quote paired with an estimated historical
   spread is exactly such a disagreement unless the estimator is calibrated
   against measured quotes first.

Until that is settled, nothing here changes the configured `commission_pct` /
`slippage_pct`. Lowering modelled costs makes every backtest look better, which
is the one direction a system with no demonstrated edge must not drift by
accident. The measurement is recorded so the number stops being invented; the
replacement is a separate, deliberate change.

## What is still unmeasured

- The **$50M–$500M/day band has no observations at all** — and it is where most
  of the S&P 500 lives.
- One timestamp, 22 minutes after the open. Mid-session spreads are tighter;
  the sample therefore *overstates* cost, making the "too punitive" finding
  conservative and the "too generous" finding worse than shown.
- **Market impact is not in here.** Quoted spread is the floor on friction, not
  the whole of it. A size that walks the book pays more, and the movers path
  trades exactly the names where the book is thinnest.
