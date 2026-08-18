# Pre-Market Gap Screener

`--mode premarket-scan` / `GET /api/scan-premarket` — a ranked research
watchlist built from extended-hours (4:00–9:30 ET) bars, answering *"what is
gapping, on what volume, right now, before the open."*

**This is a screener, not a signal.** It emits candidates and evidence-profile
tags; it does not generate entries, touch the executor, or loosen any
disqualifier. The standard scan → filters → RiskGuard pipeline — including the
chaser-trap rule that deliberately rejects names already up >50% in 5 days —
still applies unchanged to anything that later becomes a trade candidate. No
validated edge is claimed for the ranking itself.

---

## Why the ranking looks the way it does

Every factor was chosen from a review of the published evidence (academic
papers, disclosed-methodology backtests) and the practitioner canon (gap-and-go
/ "stocks in play" scanning as taught by Warrior Trading, SMB Capital, et al.).
The evidence tiers below: **[A]** peer-reviewed, **[B]** disclosed-methodology
backtest, **[C]** practitioner convention (folklore until measured).

| Factor | Weight (default) | Direction of evidence | Key sources |
|---|---|---|---|
| Time-of-day RVOL | 0.35 | **[A/B] Strongest factor.** Opening relative volume did nearly all the work in the one rigorous day-trading backtest: RVOL<100% → −0.02R avg/trade, >100% → +0.08R; top-20-by-RVOL ORB ≈ Sharpe 2.4–2.8 (7k stocks, 2016–23). | Zarattini, Barbon & Aziz 2024, [SSRN 4729284](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4729284); Gervais et al. JF 2001 (high-volume return premium) |
| Gap size (banded) | 0.20 | **[B] Non-monotonic.** Small gaps fill (70–93%); moderate catalyst-backed gaps persist; extreme gaps buy *variance*, not mean (100–150% small-cap gappers average −32% high-to-close while being 3× likelier to squeeze). Score peaks over a moderate band and decays beyond it. | [QuantifiedStrategies gap-fill backtests](https://www.quantifiedstrategies.com/gap-fill-trading-strategies/); [SmallCapLab research](https://www.smallcaplab.com/research) (n≈2,350); Bulkowski gap studies |
| Pre-market dollar volume | 0.15 | **[C]** Mechanically sound. The honest liquidity measure — normalizes across price; a $2 stock on 100K shares is $200K of liquidity. Floors are consensus practice. | Trade-Ideas DV filter docs; scanner-vendor guides |
| Float rotation (PM vol ÷ float) | 0.15 | **[C] Supply-normalized demand.** Rotation ≥1.0 = holder base turned over; 0.25–0.5× pre-market on a small float is treated as exceptional. Finviz encodes it as a first-class filter (`sh_curvol_o100sf`). Mechanism sound; band edges unmeasured. | CenterPoint/Lightspeed float-rotation guides; Dux methodology writeups |
| Catalyst (earnings + news feed) | 0.15 | **[A] The cleanest divider in the literature** — news-backed shocks drift (PEAD ≈ +6%/60d top-vs-bottom quintile), no-news shocks revert (~9.6% of the shock given back), strongest in small caps. Coverage: the earnings calendar plus a keyword-classified headline feed (`src/catalysts/news_feed.py`, Alpaca News → yfinance → NewsAPI) tiered strong / moderate / weak / dilution; the tier score map is `catalyst_scores` in config. Unknown stays neutral — never scored as "no news". | Savor JFE 2012; Pritamani & Singal 2001; Chan JFE 2003; PEAD literature ([Quantpedia](https://quantpedia.com/strategies/post-earnings-announcement-effect)) |

### The liquidity floor: RVOL's sign is conditional

The single most important calibration in the design. High relative volume
predicts **continuation in liquid names** and **fade in micro-cap gappers** —
SmallCapLab measured 5M+ pre-market shares → **71.5% fade** vs 56.2% below, and
Barber et al. (JF 2022) measured −4.7%/20d on the most retail-herded names.
So RVOL earns score **only above** a configurable liquidity floor (price ≥ $10,
ADV ≥ 1M, non-micro float by default); below it the same reading feeds the
`fade_risk` profile instead. A screener that rewarded RVOL unconditionally
would rank the measured fade cohort at the top of the list.

### Profiles, not predictions

Each row carries `profile`:

- **`continuation`** — the *only* combination all three literatures support:
  known catalyst + liquidity floor passed + RVOL ≥ threshold + moderate gap.
- **`fade_risk`** — the measured fade cohort: extreme gap in an illiquid /
  micro-float name without a known catalyst (aggravated by crowded pre-market
  volume → `crowded_micro_float` flag).
- **`neutral`** — everything else. Honesty over false precision.

And the disclosure that matters most: **buying gap-ups at the open is,
unconditionally, ~zero-to-negative expectancy** in the academic record — the
equity premium accrues overnight and opens are systematically high (Cooper,
Cliff & Gulen 2008; Lou, Polk & Skouras JFE 2019; Bogousslavsky JFE 2021).
A high score means "most worth researching", never "buy this".

---

## The RVOL formula (and why the naive one is wrong)

```text
RVOL_tod = cumulative PM volume through clock-time t today
         ÷ mean over prior N sessions of cumulative PM volume through the same t
```

Dividing today's 8:00 AM volume by an N-day *full-day* average — the naive
formula — understates pre-market activity by construction and is the most
common home-scanner bug. Two clamps keep the ratio honest:

- **Thin baseline** (< `rvol_min_baseline_shares`): a 100K-share morning over a
  5K-share baseline computes to "20×" while the name is still illiquid in
  absolute terms; the screener reports `rvol: null, rvol_basis: thin_baseline`
  instead, and the absolute share/dollar floors do the gating.
- **Insufficient history** (< `rvol_min_history_sessions` prior sessions with
  PM bars): `rvol_basis: insufficient_history`. Numerator and denominator come
  from the same frame/feed, so a partial feed (IEX-only) still yields an
  internally consistent ratio.

## Data-source reality (read before trusting a quiet morning)

- **Alpaca (primary):** intraday bars include extended hours, but the free-tier
  `iex` feed only carries trades printed on IEX (~2–3% of consolidated volume)
  — pre-market bars on small caps are sparse or absent. The paid `sip` feed is
  the reliable extended-hours source.
- **Polygon:** aggregates span the extended session (key required).
- **yfinance (fallback):** `prepost=True` works but with known issues — 1m
  history capped at ~7 days, ~2-minute delay on the latest pre-market bars,
  and documented missing-bar windows. Fine for a morning snapshot; wrong for
  continuous polling.
- **Free Finviz cannot see pre-market at all** — extended-hours data is
  Elite-only and the free tier is 15–20 min delayed; its gap/top-gainers
  signals describe the *previous* regular session. The (default-off) Finviz
  provider is therefore a **context** source here, not a gap source: useful
  prior-day screens include `s=ta_topgainers&f=sh_avgvol_o300,sh_price_o2`
  (yesterday's momentum), `f=sh_float_u50,sh_relvol_o2,sh_curvol_o500`
  (low-float watch), `s=n_earningsbefore` (earnings before the open), and note
  `sh_curvol_o100sf` (volume >100% of float) / `sh_curvol_ousd1000` (dollar
  volume >$1M) exist as native float-rotation / dollar-volume filters.
- A missing ticker in the output means **no pre-market prints were available**
  from the configured providers — not that nothing is happening. Per-row
  `data_notes` carry the degradations that applied.

## Which universe to screen (and why `MOVERS` is the wrong one before 09:30)

The screener ranks whatever ticker list you hand it, so the universe decides
what it *can* find. There are two movers universes and they are not
interchangeable:

| `--universe` | What it contains | When it is right |
|---|---|---|
| `PREMARKET` | Movers in the **current** extended-hours session | 04:00–09:30 ET |
| `MOVERS` | Movers in the **previous regular session** | after the open |
| `WATCHLIST` | `CUSTOM_WATCHLIST` | any time, if you already know the names |

Before 09:30 the regular-session movers chain (Alpaca/Polygon/FMP gainers)
reports **yesterday**. Observed 2026-08-18 at 09:02 ET: the movers panel listed
SIC/WETO/IPST at their 2026-08-17 closing prices, and none of those names had an
extended-hours print that morning. Handing that list to the screener produced 6
usable snapshots out of 50 — the screener was working; the universe described
the wrong session. `MOVERS` and this screener never overlap usefully: during the
window the screener needs, that universe is stale; when it refreshes, the window
has closed.

`PREMARKET` (`src/scanner/premarket_movers.py`, config `premarket_movers:`) is
the discovery chain for the live extended-hours tape. Same `list`-vs-`None`
contract as the regular-session chain: an empty list is a real answer (quiet
tape) and stops the chain; `None` means the provider could not answer and the
next one is tried.

### Coverage is disclosed, because providers differ in what they can see

| Provider | Coverage | Consequence |
|---|---|---|
| `polygon` | `market_wide` | Full snapshot — a stock gapping on an 08:00 release is visible even with no prior-session activity. Needs a plan carrying the snapshot endpoint. |
| `yahoo` | `bounded_pool` | Ranks a candidate pool built from the **prior** session's gainers/losers/actives. Keyless, so it always exists — and **structurally blind** to a name that was quiet yesterday. |

The serving provider and its coverage class travel with every result
(`universe_discovery` in the API, a `universe:` note on the dashboard status
line, a `UNIVERSE:` line on the CLI). A `bounded_pool` result is not a smaller
market-wide scan — there are gappers it cannot rank at any position — so
falling back changes the meaning of the answer and says so rather than
swapping breadth silently.

Empty results are disambiguated too: outside 04:00–09:30 ET the disclosure says
the session is closed rather than leaving "0 movers" to read as a quiet tape or
a dead provider.

## Penny profile (`--penny`, or automatic with penny mode on)

For low-priced intraday trading the standard gates are calibrated against you —
the $10 liquidity floor and $1M PM dollar-volume bar exclude the whole penny
band. `--mode premarket-scan --penny` (implied when `penny.enabled` /
`ALLOW_PENNY_STOCKS` is on) swaps the **gates** for the `penny:` hard rails,
which stay the single source of truth:

- price band ← `penny.min_price`–`penny.max_price` ($0.50–$10 default)
- **market-cap floor ← `penny.min_market_cap` ($50M)** — the sub-$50M
  pump-and-dump zone stays cut, exactly as penny mode intends. A shell at a
  $1.4M cap topping the gainers list fails this rail no matter how loud the
  move is.
- ADV backstop ← `penny.min_share_volume`; PM-session floors scale down
  (150K shares / $300K by default, tunable via `premarket.penny_overrides`)

The rails **fail closed**: a ticker whose market cap or ADV the provider
can't supply surfaces as a near-miss (`market_cap_unavailable` /
`adv_unavailable`), never as a candidate — an unverifiable rail is a failed
rail. (The standard profile keeps missing ADV fail-soft; its floors are a
screen, not a hard-rail contract.)

What the profile deliberately does **not** change: the scoring liquidity floor,
the `fade_risk` / `crowded_micro_float` tagging, and every disclosure. In the
penny band, most candidates will carry `below_liquidity_floor` and many will
tag `fade_risk` — that is the measured base rate for this cohort, and the
labels are the information: they tell you which side of the 71.5%-fade
statistic each name sits on. Discovery is broad; the labels stay strict.

```bash
python bot.py --mode premarket-scan --penny --universe CUSTOM
GET /api/scan-premarket?penny=true
```

## Timing

One static early scan is structurally weak: most catalysts drop 6:00–8:00 ET,
8:30 macro prints can create or kill gappers instantly, and both RVOL and
spread readings are unrepresentative in the thin 4:00–7:00 stretch. The
practitioner-consensus cadence is staged passes — **~7:00 discovery, ~8:45
post-macro refresh, ~9:15 confirmation** — with the final ranking computed as
late as possible. Run the mode from cron at those times (the scheduler's
`pre_market` slot runs the *daily* scan, a different thing), e.g.:

```bash
python bot.py --mode premarket-scan --universe WATCHLIST
python bot.py --mode premarket-scan --universe SP500 --json
```

## Configuration

Everything lives under `premarket:` in `config.yaml` (see inline comments for
per-key provenance): hard gates (`min_gap_pct`, price band, `min_pm_volume`,
`min_pm_dollar_volume`, `min_adv_20d`), RVOL history/clamps, the liquidity
floor, gap-band knots, composite `weights`, saturation scales, and the profile
thresholds. Defaults encode the consensus/evidence values above; none of them
are validated alpha, and the weights exist to be re-examined — not believed.

## The news catalyst feed

`premarket.news` (default on, fail-soft) fetches recent headlines — Alpaca News
API (existing keys, real-time) → yfinance (keyless) → NewsAPI — and classifies
them with a **deterministic keyword taxonomy**:

| Tier | Examples | Effect |
|---|---|---|
| `strong` | earnings beat / raised guidance, FDA approval / met endpoint, M&A, contract award, analyst upgrade | Full catalyst weight; counts toward `continuation` |
| `moderate` | earnings mention, investor day, coverage initiation, conference | Half weight (default) |
| `weak` | "why is X soaring" churn, unusual-volume listicles, watchlist pieces | No weight — the no-news-pump profile that reverts |
| `dilution` | offering / registered direct / warrants / reverse split | **Not a catalyst**: sets the `dilution_news` flag (outranks every other tier — the offering IS the story) |

The earnings-calendar hit always takes precedence (it is the verified event);
the winning headline is exported per row (`catalyst_headline`) as the receipt a
human can check. Three honesty notes: the classifier is a **heuristic, not
verification** — patterns are deliberately conservative, so a missed strong
catalyst degrades a candidate rather than inflating one; provider coverage is
best-effort, so `unknown` still never means "no news"; and headline text is
untrusted input used for classification only, never executed or displayed
unescaped.

## Known limits / future work
- **No spread gate** — needs quote (bid/ask) data; the one open-source scanner
  with a spread filter gates at ≤0.30% of midpoint, and pre-market spreads are
  exactly where that matters. Add when a quotes source lands.
- **Dilution detection is headline-grade only** — the `dilution_news` flag
  catches announced offerings in the wire; an active-but-quiet S-3/ATM shelf
  still needs an EDGAR integration to see.
- **No halt/LULD awareness.**
- **Validation pathway:** the ranking's actual predictive value is an empirical
  question. The honest test is an event study through `--mode
  intraday-backtest`-style replay (rank at 9:25 → measure open-to-close by
  profile/score bucket) on real extended-hours history — until that runs, this
  is a research tool, full stop.

## Sources

Beyond the inline links above: Cooper, Cliff & Gulen 2008 (SSRN 1004081);
Lou, Polk & Skouras JFE 2019; Bogousslavsky JFE 2021 (SSRN 2869624); Aboody et
al. JFQA 2018 (SSRN 2554010); Barber, Huang, Odean & Schwarz JF 2022 (SSRN
3715077); Chan JFE 2003 (SSRN 262452); Savor JFE 2012; Gervais, Kaniel &
Mingelgrin JF 2001 (SSRN 146468); Frieder & Zittrain 2007 (spam/pump reversal);
QuantifiedStrategies gap-fill and gap-trading backtests; TradeThatSwing SPY gap
statistics; SharePlanner large-gap open-to-close data; SmallCapLab gapper
research; Trade-Ideas RV/DV filter documentation; SMB Capital RVOL and
stocks-in-play posts; Warrior Trading gap-and-go materials; CenterPoint /
Lightspeed float-rotation guides; the pyfinviz / finvizfinance /
knicola/finviz-screener code for the Finviz URL grammar; alpacahq
Momentum-Trading-Example, shner-elmo/TradingView-Screener,
adhabnr-ux/california-market-scanner (the time-of-day RVOL implementation this
module mirrors), Ssanji-san/daytrade-scanner (same-feed RVOL discipline) among
surveyed open-source scanners; yfinance issue tracker
(#356, #720, #1189, #1393, #1757, #2128) for the extended-hours caveats.
