# Evaluating a forecast model: the coverage test

`--mode forecast-coverage` scores a forecast *interval* against naive baselines
on identical windows. It was written to evaluate
[Kronos](https://github.com/shiyu-coder/Kronos) (MIT, AAAI 2026, a foundation
model for candlestick data), but the harness is forecaster-agnostic.

```bash
# Baselines only — no torch, no weights, no network beyond the bar fetch.
python bot.py --mode forecast-coverage --ticker AAPL --horizon 5 --level 0.90

# With Kronos, from a local clone.
python bot.py --mode forecast-coverage --ticker AAPL --horizon 5 \
  --kronos-path /path/to/Kronos --paths 200
```

Measurement only. `src/research/` may not import the executor or the broker, and
a test asserts it.

## Why coverage, and why coverage alone is not enough

A 90% band should contain the realised close 90% of the time. A band that
contains it 60% of the time is decoration.

But coverage cannot *rank* forecasters, because it is trivial to be perfectly
calibrated and useless — widen the band until it always contains the answer. The
real question is **sharpness subject to calibration**: among forecasters with
correct coverage, the narrower band knows more. So every run reports three
numbers together, and reading any one alone is a mistake:

| | |
|---|---|
| **coverage** | did the band do what it claimed? |
| **width %** | how much did it claim, relative to price? |
| **interval score %** | the Winkler proper scoring rule that trades the two off. Lower is better. |

The interval score for a central `1 − α` interval is
`(u − l) + (2/α)(l − y)` when `y < l`, `+ (2/α)(y − u)` when `y > u`.
Widening costs width on every window; missing costs `2/α` × the shortfall, which
at the 90% level is 20×. There is no free direction, which is the point.

## The harness is tested before it is trusted

`tests/test_interval_calibration.py` runs entirely on synthetic data whose
correct answer is known in advance. A measurement tool that has only been
pointed at real data has not been tested — it has been *used*, and a bug in it
is indistinguishable from a finding.

The self-tests assert that a correctly-specified band recovers the nominal level
at 50/80/90/95%, that a band one-third too narrow is caught with nominal
*excluded* from the confidence interval, that widening to guarantee coverage
loses on interval score, that narrowing to look sharp also loses, that the
forecaster never sees a bar past the origin, and that a forecaster which
declines the hard windows is counted rather than quietly flattered.

## What it looks like, and the number that matters

Run on a Gaussian random walk (σ = 2%/bar, 6000 bars, horizon 5, level 90%,
1079 non-overlapping windows), with the true data-generating process as an
"oracle" and two deliberately broken models:

| forecaster | coverage | 95% CI | width % | score % | verdict |
|---|---|---|---|---|---|
| oracle (correct σ) | 89.5% | 0.88–0.91 | 14.74 | **18.631** | calibrated |
| overconfident (σ/3) | 40.4% | 0.38–0.43 | 4.91 | 38.774 | MISCALIBRATED |
| useless (σ×5) | 100.0% | 1.00–1.00 | 75.31 | 75.313 | MISCALIBRATED |
| naive_gaussian | 89.6% | 0.88–0.91 | 14.74 | 18.941 | calibrated |
| naive_empirical | 89.5% | 0.88–0.91 | 15.00 | **19.131** | calibrated |

The overconfident model is caught. The useless model has *perfect* coverage and
is still caught, by width and score — which is why coverage is never reported
alone.

**And then the finding that matters more than any of that.** The oracle here is
the true process — no model can do better. Its interval score is 18.631 against
naive_empirical's 19.131: an edge of **2.6%**. On a random walk, the free
baseline is within three percent of theoretically optimal.

So the bar for Kronos is not "is it calibrated" — trailing volatility is already
calibrated. The bar is: does it beat 19.131-equivalent on real bars, and by
enough to matter? If it scores anywhere near the naive baseline it has
demonstrated nothing, and the *entire* headroom available is the amount by which
real prices are more predictable than a random walk. That is the quantity nobody
publishing a price-forecasting result reports, and it is small.

## Three deliberate departures from the popular walkthrough

Verified against the repository source at `67b630e`:

1. **`top_p=1.0`, not 0.9.** Nucleus sampling at 0.9 discards the bottom tenth
   of probability mass at every token, so paths are drawn from an
   already-truncated distribution and their 5th/95th percentiles are then
   presented as the model's uncertainty. That understates the band in exactly
   the tails a risk interval exists to describe.
2. **200 paths, not 20.** At n=20 the 5th and 95th percentiles are essentially
   the sample minimum and maximum — the highest-variance statistics available —
   so band width moves substantially run to run.
3. **One batched call, not N sequential ones.** `sample_count` looks like the
   parameter for sampling many paths and is not: `model/kronos.py:467` does
   `preds = np.mean(preds, axis=1)`, so `sample_count=20` collapses to a single
   mean path and destroys the distribution. Passing the same window N times to
   `predict_batch` with `sample_count=1` yields N independent paths in one
   forward pass, which is what makes 200 affordable.

Two data bugs worth avoiding, both outside this harness: fetch **adjusted**
bars (unadjusted history makes a split look like a crash — the same artifact
`movers.suspect_change_pct` exists for), and **convert** timezones rather than
stripping them, since the model consumes hour-of-day as a covariate.

## What a pass would actually license

Nothing about direction. If the band is calibrated *and* sharper than naive, the
thing it has earned is a role in **stop distance and position sizing** — which is
where this system is weakest, since both currently come from ATR alone, and
where `docs/COST_MODEL.md` showed the binding constraint is risk-unit versus
friction. That is a range question, not a direction question.

The median path is the part least likely to survive contact with a random walk,
and a passing coverage test says nothing whatsoever about it.

## Limits

- **Daily bars.** The intraday paths here run on 1m/5m with a 28-bar warm-up and
  flat-at-close; Kronos-small has 512 context and the upstream examples are
  daily. Only the daily composite fits a multi-day horizon.
- **`naive_empirical` uses overlapping h-step returns** for its quantile
  estimate — fewer independent observations than it appears, though still
  consistent. It is the baseline, not the thing under test.
- **Non-overlapping windows cost sample size.** `stride` defaults to `horizon`
  so the coverage confidence interval means what it says; `windows_overlap` is
  reported when a caller overrides it.
- **A calibrated band is not a tradeable edge.** It has to survive costs, and
  `docs/COST_MODEL.md` measured those at 0.0286% round trip on liquid names and
  up to 3.92% on thin ones.
