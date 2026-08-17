"""Pre-market gap screener — ranked watchlist from extended-hours bars.

Answers "what is gapping, on what volume, right now, before the open" from the
extended-hours (4:00–9:30 ET) session, and ranks candidates by the factors the
measured evidence actually supports. It is a **screener, not a signal**: the
output is a research watchlist. Nothing here touches the executor, loosens a
disqualifier, or implies an entry — the standard scan → filters → RiskGuard
pipeline is unchanged, and its chaser-trap / liquidity protections still apply
to anything that later becomes a trade candidate.

Evidence base (docs/PREMARKET_SCREENER.md carries the full citation table):

* **Relative volume, computed by time-of-day**, is the best-supported ranking
  factor (Zarattini/Barbon/Aziz 2024, SSRN 4729284: the RVOL filter carried
  essentially the whole edge). The correct formula is cumulative session volume
  through the current clock time divided by the mean of prior sessions' volume
  through the *same* window — never raw volume over a full-day average, which
  understates pre-market activity by construction.
* **RVOL's sign is conditional**: above a liquidity floor it supports
  continuation; in micro-cap gappers extreme pre-market volume predicts *fade*
  (SmallCapLab n≈2,350: 5M+ pre-market shares → 71.5% fade rate). So volume
  raises rank only above the floor, and feeds the ``fade_risk`` profile below.
* **Catalyst beats gap size** (Savor 2012; PEAD literature): news-backed moves
  drift, no-news moves revert. We can only observe one catalyst source offline
  (the P1.4 earnings calendar), so catalyst is scored known/unknown — an
  unknown is neutral, never treated as proof of "no news".
* **Extreme gaps buy variance, not mean** (fill probability falls with size,
  but 100%+ small-cap gaps average −32% high-to-close): the gap-size score
  rises through a moderate band and *declines* beyond it.

Data notes: gap and session aggregates come from ``fetch_intraday(prepost=True)``
— Alpaca/Polygon intraday bars already span the extended session; the flag turns
it on for yfinance. The free-tier caveats (sparse IEX pre-market prints, delayed
yfinance extended bars) are disclosed per result in ``data_notes``.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, time as dtime, timezone
from typing import Any, Callable, Optional
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from src.data.fetcher import fetch_intraday, fetch_ohlcv, fetch_ticker_info_yfinance
from src.utils import config
from src.utils.logger import get_logger

log = get_logger(__name__)

_EASTERN = ZoneInfo("America/New_York")
_PM_START = dtime(4, 0)
_RTH_OPEN = dtime(9, 30)


# ── session utilities ─────────────────────────────────────────────────────────

def to_eastern_index(index: pd.Index) -> pd.DatetimeIndex:
    """Convert a bar index to America/New_York.

    The fetch layer normalises intraday frames to naive UTC; provider quirks can
    still hand back tz-aware indexes, so both are accepted. Naive timestamps are
    read as UTC — the one convention the pipeline guarantees.
    """
    idx = pd.DatetimeIndex(index)
    if idx.tz is None:
        idx = idx.tz_localize(timezone.utc)
    return idx.tz_convert(_EASTERN)


def premarket_bars(df: pd.DataFrame, session_date) -> pd.DataFrame:
    """Bars in the 04:00–09:30 ET window of ``session_date`` (ET calendar date).

    Slices by exchange-time wall clock, NOT by calendar date of the raw index —
    a 8:00 ET bar is 12:00/13:00 UTC, so date-slicing a UTC index misassigns
    late-evening UTC bars and DST shifts the boundary. (The existing
    ``current_session_bars`` date-slice is left untouched; this module owns its
    own session math.)
    """
    if df is None or df.empty:
        return pd.DataFrame()
    et = to_eastern_index(df.index)
    mask = (
        (et.date == session_date)
        & (et.time >= _PM_START)
        & (et.time < _RTH_OPEN)
    )
    out = df.loc[mask].copy()
    out.index = et[mask]
    return out


def cumulative_volume_through(df_pm: pd.DataFrame, cutoff: dtime) -> float:
    """Cumulative pre-market share volume through an ET wall-clock cutoff."""
    if df_pm is None or df_pm.empty or "Volume" not in df_pm.columns:
        return 0.0
    et = pd.DatetimeIndex(df_pm.index)
    return float(df_pm.loc[et.time <= cutoff, "Volume"].sum())


def time_of_day_rvol(
    frame: pd.DataFrame,
    session_date,
    cutoff: dtime,
    *,
    min_history_sessions: int,
    min_baseline_shares: float,
) -> tuple[Optional[float], str]:
    """Pre-market RVOL vs the same clock window on prior sessions.

    ``today's cumulative pre-market volume through cutoff ÷ mean of prior
    sessions' cumulative pre-market volume through the same cutoff`` — the
    formula Trade-Ideas implements and SSRN 4729284 validates, restricted to the
    pre-market session. Numerator and denominator come from the same frame, so
    a partial feed (e.g. IEX-only) still yields an internally consistent ratio.

    Returns ``(rvol, basis)`` where basis is one of:
      * ``time_of_day``      — proper same-window comparison.
      * ``thin_baseline``    — baseline below ``min_baseline_shares``; rvol is
        None because 100K over a 5K-share baseline would print "20x" while the
        name is still illiquid in absolute terms (the ratio pathology every
        scanner guide warns about).
      * ``insufficient_history`` — fewer than ``min_history_sessions`` prior
        sessions with pre-market bars; rvol is None and the caller falls back
        to absolute volume floors.
    """
    et_dates = sorted({d for d in to_eastern_index(frame.index).date})
    prior = [d for d in et_dates if d < session_date]
    baselines = []
    for d in prior:
        vol = cumulative_volume_through(premarket_bars(frame, d), cutoff)
        if vol > 0:
            baselines.append(vol)
    if len(baselines) < max(1, int(min_history_sessions)):
        return None, "insufficient_history"
    mean_base = float(np.mean(baselines))
    if mean_base < float(min_baseline_shares):
        return None, "thin_baseline"
    today = cumulative_volume_through(premarket_bars(frame, session_date), cutoff)
    return today / mean_base, "time_of_day"


# ── per-ticker snapshot ───────────────────────────────────────────────────────

@dataclass
class PremarketSnapshot:
    ticker: str
    gap_pct: Optional[float] = None
    last_price: Optional[float] = None
    prev_close: Optional[float] = None
    pm_volume: float = 0.0
    pm_dollar_volume: float = 0.0
    pm_high: Optional[float] = None
    pm_low: Optional[float] = None
    prev_day_high: Optional[float] = None
    prev_day_low: Optional[float] = None
    rvol: Optional[float] = None
    rvol_basis: str = "unavailable"
    float_shares: Optional[float] = None
    float_rotation: Optional[float] = None
    market_cap: Optional[float] = None
    adv_20d: Optional[float] = None
    # "earnings" | "news_strong" | "news_moderate" | "news_weak" | "unknown"
    catalyst: str = "unknown"
    catalyst_headline: Optional[str] = None
    catalyst_provider: Optional[str] = None
    dilution_news: bool = False
    days_to_earnings: Optional[int] = None
    flags: list[str] = field(default_factory=list)
    data_notes: list[str] = field(default_factory=list)


def _prev_regular_close(daily: pd.DataFrame, session_date) -> Optional[float]:
    """Previous session's regular close from the daily frame.

    Daily bars are the reliable source for the prior close — deriving it from
    extended bars would splice yesterday's after-hours drift into the gap.
    """
    if daily is None or daily.empty or "Close" not in daily.columns:
        return None
    idx = pd.DatetimeIndex(daily.index)
    if idx.tz is not None:
        idx = idx.tz_convert(_EASTERN)
    dates = idx.date
    prior = dates < session_date
    if not prior.any():
        return None
    val = daily["Close"].to_numpy()[prior][-1]
    return float(val) if np.isfinite(val) else None


def _prev_day_levels(daily: pd.DataFrame, session_date) -> dict[str, Optional[float]]:
    """Previous session's high/low (PDH/PDL) from the daily frame, when present.

    Daily provider bars are regular-session only, so these are the exact levels
    the practitioner canon marks. Exported as research aids (and inputs to the
    opt-in level-anchored sweep-reclaim variant); None when the daily frame
    lacks High/Low columns or has no prior row.
    """
    out: dict[str, Optional[float]] = {"high": None, "low": None}
    if daily is None or daily.empty:
        return out
    idx = pd.DatetimeIndex(daily.index)
    if idx.tz is not None:
        idx = idx.tz_convert(_EASTERN)
    prior = idx.date < session_date
    if not prior.any():
        return out
    for key, col in (("high", "High"), ("low", "Low")):
        if col in daily.columns:
            val = daily[col].to_numpy()[prior][-1]
            out[key] = float(val) if np.isfinite(val) else None
    return out


def build_snapshot(
    ticker: str,
    *,
    now_et: datetime,
    settings: dict,
    intraday: Optional[pd.DataFrame] = None,
    daily: Optional[pd.DataFrame] = None,
    info: Optional[dict] = None,
    days_to_earnings: Optional[Callable[[str], Optional[int]]] = None,
    news_fn: Optional[Callable[[str], dict]] = None,
) -> Optional[PremarketSnapshot]:
    """Compute one ticker's pre-market aggregates. None when no usable data.

    All data dependencies are injectable for offline tests; production callers
    let the defaults fetch through the provider cascade.
    """
    symbol = ticker.upper()
    session_date = now_et.date()
    cutoff = min(now_et.time(), _RTH_OPEN)

    if intraday is None:
        intraday = fetch_intraday(
            symbol,
            interval=str(settings.get("interval", "5m")),
            period=str(settings.get("period", "15d")),
            prepost=True,
        )
    pm = premarket_bars(intraday, session_date)
    if pm.empty:
        return None  # no pre-market prints — nothing to screen

    if daily is None:
        daily = fetch_ohlcv(symbol, period="3mo", interval="1d")
    prev_close = _prev_regular_close(daily, session_date)

    # Clamp the session frame to the scan's declared cutoff BEFORE deriving any
    # field. A multi-ticker scan crossing a bar boundary would otherwise let
    # later fetches carry bars past the as-of time — and last_price / gap /
    # dollar volume would read a different instant than share volume and RVOL,
    # so gates and scores would mix as-of times across tickers (Codex P1).
    pm = pm.loc[pd.DatetimeIndex(pm.index).time <= cutoff]
    if pm.empty:
        return None

    snap = PremarketSnapshot(ticker=symbol)
    snap.prev_close = prev_close
    last = pm["Close"].iloc[-1]
    snap.last_price = float(last) if np.isfinite(last) else None
    if prev_close and snap.last_price and prev_close > 0:
        snap.gap_pct = (snap.last_price - prev_close) / prev_close * 100.0

    snap.pm_volume = float(pm["Volume"].sum())
    closes = pm["Close"].to_numpy(dtype=float)
    vols = pm["Volume"].to_numpy(dtype=float)
    snap.pm_dollar_volume = float(np.nansum(closes * vols))
    if "High" in pm.columns:
        hi = float(pm["High"].max())
        snap.pm_high = hi if np.isfinite(hi) else None
    if "Low" in pm.columns:
        lo = float(pm["Low"].min())
        snap.pm_low = lo if np.isfinite(lo) else None
    pd_levels = _prev_day_levels(daily, session_date)
    snap.prev_day_high = pd_levels["high"]
    snap.prev_day_low = pd_levels["low"]

    snap.rvol, snap.rvol_basis = time_of_day_rvol(
        intraday, session_date, cutoff,
        min_history_sessions=int(settings.get("rvol_min_history_sessions", 5)),
        min_baseline_shares=float(settings.get("rvol_min_baseline_shares", 10_000)),
    )
    if snap.rvol_basis != "time_of_day":
        snap.data_notes.append(f"rvol:{snap.rvol_basis}")

    if daily is not None and not daily.empty and "Volume" in daily.columns:
        # Exclude today's (partial) bar BY DATE, not by dropping the last row:
        # pre-market most providers have no session_date row yet, so a blind
        # iloc[:-1] would discard yesterday's completed volume and shift the
        # ADV window back a day (Codex P2).
        didx = pd.DatetimeIndex(daily.index)
        if didx.tz is not None:
            didx = didx.tz_convert(_EASTERN)
        completed = daily["Volume"].loc[didx.date < session_date].tail(20)
        if len(completed) >= 5:
            snap.adv_20d = float(completed.mean())

    if info is None:
        info = fetch_ticker_info_yfinance(symbol) or {}
    try:
        mc = info.get("marketCap")
        snap.market_cap = float(mc) if mc else None
    except (TypeError, ValueError):
        snap.market_cap = None
    raw_float = info.get("floatShares")
    used_fallback = not raw_float  # 0 is as unusable as absent — fall back, but SAY so
    flt = raw_float or info.get("sharesOutstanding")
    try:
        snap.float_shares = float(flt) if flt else None
    except (TypeError, ValueError):
        snap.float_shares = None
    if snap.float_shares and snap.float_shares > 0:
        snap.float_rotation = snap.pm_volume / snap.float_shares
    if used_fallback and snap.float_shares is not None:
        snap.data_notes.append("float:shares_outstanding_fallback")

    def _default_days_to_earnings(sym: str) -> Optional[int]:
        try:
            from src.catalysts.earnings_calendar import default_calendar
            return default_calendar().days_to_earnings(sym)
        except Exception:
            return None

    resolve_earnings = days_to_earnings or _default_days_to_earnings
    snap.days_to_earnings = resolve_earnings(symbol)
    window = int(settings.get("catalyst_earnings_window_days", 1))
    if snap.days_to_earnings is not None and abs(snap.days_to_earnings) <= window:
        snap.catalyst = "earnings"

    # News catalyst feed (config `premarket.news`, absent/disabled → skipped so
    # offline runs and tests never touch the network). The earnings-calendar
    # hit takes precedence — it is the verified event; headlines refine the
    # rest. A dilution headline is NOT a catalyst: it sets a warning flag and
    # leaves the tier to the remaining classification.
    news_cfg = settings.get("news") or {}
    if news_fn is None and bool(news_cfg.get("enabled", False)):
        # Company name lets the NewsAPI fallback validate article identity —
        # short symbols ("AI", "ON") are ordinary English in full-text search.
        company = str(info.get("shortName") or info.get("longName") or "")

        def news_fn(sym: str) -> dict:
            from src.catalysts.news_feed import catalyst_news_for
            return catalyst_news_for(
                sym,
                max_age_hours=float(news_cfg.get("max_age_hours", 36)),
                company_name=company,
            )
    if news_fn is not None:
        try:
            news = news_fn(symbol) or {}
        except Exception as exc:
            log.debug(f"{symbol}: news catalyst lookup failed ({exc})")
            news = {}
        tier = str(news.get("tier") or "none")
        if tier == "dilution":
            snap.dilution_news = True
            snap.catalyst_headline = news.get("headline")
            snap.catalyst_provider = news.get("provider")
        elif tier in ("strong", "moderate", "weak"):
            if snap.catalyst != "earnings":
                snap.catalyst = f"news_{tier}"
            snap.catalyst_headline = news.get("headline")
            snap.catalyst_provider = news.get("provider")
    return snap


# ── gates, scoring, profiles ─────────────────────────────────────────────────

def passes_gates(snap: PremarketSnapshot, settings: dict) -> tuple[bool, list[str]]:
    """Mechanical liquidity/size gates. Returns (passed, failure reasons)."""
    reasons: list[str] = []
    price = snap.last_price
    if price is None or snap.gap_pct is None:
        return False, ["no_price_or_gap"]
    if abs(snap.gap_pct) < float(settings.get("min_gap_pct", 4.0)):
        reasons.append("gap_below_min")
    if price < float(settings.get("min_price", 2.0)):
        reasons.append("price_below_min")
    if price > float(settings.get("max_price", 100.0)):
        reasons.append("price_above_max")
    if snap.pm_volume < float(settings.get("min_pm_volume", 100_000)):
        reasons.append("pm_volume_below_min")
    if snap.pm_dollar_volume < float(settings.get("min_pm_dollar_volume", 1_000_000)):
        reasons.append("pm_dollar_volume_below_min")
    adv_floor = float(settings.get("min_adv_20d", 500_000))
    if snap.adv_20d is None:
        # The penny profile's ADV floor is a hard rail — an unverifiable value
        # must not pass it. The standard profile stays fail-soft: provider info
        # gaps are routine there and the floor is a screen, not a rail.
        if settings.get("profile") == "penny":
            reasons.append("adv_unavailable")
    elif snap.adv_20d < adv_floor:
        reasons.append("adv_below_min")
    cap_floor = float(settings.get("min_market_cap", 0))
    if cap_floor > 0:
        # A positive cap floor is an explicit hard rail (penny profile sets
        # $50M): unknown market cap fails closed, it doesn't skip the gate.
        if snap.market_cap is None:
            reasons.append("market_cap_unavailable")
        elif snap.market_cap < cap_floor:
            reasons.append("market_cap_below_min")
    return (not reasons), reasons


def _gap_band_score(gap_abs: float, settings: dict) -> float:
    """0–1 score rising through the moderate band, declining beyond it.

    Extreme gaps raise variance, not mean (100–150% small-cap gappers average
    −32% high-to-close), so score peaks over [peak_lo, peak_hi] and decays
    linearly to ``floor_beyond`` at ``extreme`` — never rewarding the blow-off.
    """
    lo = float(settings.get("gap_score_min", 4.0))
    peak_lo = float(settings.get("gap_score_peak_lo", 6.0))
    peak_hi = float(settings.get("gap_score_peak_hi", 15.0))
    extreme = float(settings.get("gap_score_extreme", 30.0))
    floor_beyond = float(settings.get("gap_score_floor_beyond", 0.2))
    if gap_abs <= lo:
        return 0.0
    if gap_abs < peak_lo:
        return (gap_abs - lo) / max(peak_lo - lo, 1e-9)
    if gap_abs <= peak_hi:
        return 1.0
    if gap_abs >= extreme:
        return floor_beyond
    frac = (gap_abs - peak_hi) / max(extreme - peak_hi, 1e-9)
    return 1.0 - frac * (1.0 - floor_beyond)


def _saturating(value: float, scale: float) -> float:
    """Map [0, ∞) → [0, 1) with diminishing returns; scale = the ~0.63 point."""
    if value <= 0 or scale <= 0:
        return 0.0
    return float(1.0 - np.exp(-value / scale))


def liquidity_floor_passed(snap: PremarketSnapshot, settings: dict) -> bool:
    """The evidence's continuation/fade divider: price, ADV and float floors.

    Above it, high RVOL supports continuation (SSRN 4729284); below it the same
    reading marks the fade cohort (SmallCapLab), so scoring must not reward it.
    """
    price_ok = (snap.last_price or 0) >= float(settings.get("liquidity_floor_price", 10.0))
    adv_ok = (snap.adv_20d or 0) >= float(settings.get("liquidity_floor_adv", 1_000_000))
    micro = snap.float_shares is not None and snap.float_shares < float(
        settings.get("micro_float_shares", 10_000_000)
    )
    return price_ok and adv_ok and not micro


def score_snapshot(snap: PremarketSnapshot, settings: dict) -> float:
    """Composite 0–100. Weights are config-driven and deliberately NOT a claim
    of validated alpha — see docs; the ordering encodes the evidence review's
    direction-of-support, nothing more."""
    weights = settings.get("weights") or {}
    w_rvol = float(weights.get("rvol", 0.35))
    w_gap = float(weights.get("gap_band", 0.20))
    w_dollar = float(weights.get("dollar_volume", 0.15))
    w_rot = float(weights.get("float_rotation", 0.15))
    w_cat = float(weights.get("catalyst", 0.15))
    total_w = w_rvol + w_gap + w_dollar + w_rot + w_cat
    if total_w <= 0:
        return 0.0

    liquid = liquidity_floor_passed(snap, settings)
    # RVOL: rewarded only above the liquidity floor (sign-conditional evidence).
    if snap.rvol is not None and liquid:
        rvol_score = _saturating(snap.rvol, float(settings.get("rvol_scale", 5.0)))
    else:
        rvol_score = 0.0
    gap_score = _gap_band_score(abs(snap.gap_pct or 0.0), settings)
    dollar_score = _saturating(
        snap.pm_dollar_volume, float(settings.get("dollar_volume_scale", 5_000_000))
    )
    rot_score = (
        _saturating(snap.float_rotation, float(settings.get("rotation_scale", 0.25)))
        if snap.float_rotation is not None
        else 0.0
    )
    cat_scores = settings.get("catalyst_scores") or {}
    cat_defaults = {
        "earnings": 1.0, "news_strong": 1.0, "news_moderate": 0.5,
        "news_weak": 0.0, "unknown": 0.0,
    }
    cat_score = float(cat_scores.get(snap.catalyst, cat_defaults.get(snap.catalyst, 0.0)))
    cat_score = max(0.0, min(1.0, cat_score))

    raw = (
        w_rvol * rvol_score
        + w_gap * gap_score
        + w_dollar * dollar_score
        + w_rot * rot_score
        + w_cat * cat_score
    )
    return round(100.0 * raw / total_w, 2)


def classify_profile(snap: PremarketSnapshot, settings: dict) -> str:
    """Direction-of-evidence tag: 'continuation' | 'fade_risk' | 'neutral'.

    fade_risk encodes the measured fade cohort — extreme gap in an illiquid /
    micro-float name, with huge pre-market volume as an aggravator, catalyst
    unknown. continuation requires the only combination all three literatures
    support: catalyst + liquidity floor + confirmed relative volume + moderate
    gap. Everything else is neutral — honesty over false precision.
    """
    gap_abs = abs(snap.gap_pct or 0.0)
    liquid = liquidity_floor_passed(snap, settings)
    extreme = gap_abs >= float(settings.get("fade_extreme_gap_pct", 20.0))
    strong_catalysts = set(
        settings.get("continuation_catalysts") or ["earnings", "news_strong"]
    )
    if not liquid and extreme and snap.catalyst not in strong_catalysts:
        return "fade_risk"
    if (
        liquid
        and snap.catalyst in strong_catalysts
        and snap.rvol is not None
        and snap.rvol >= float(settings.get("continuation_min_rvol", 2.0))
        and gap_abs <= float(settings.get("gap_score_extreme", 30.0))
    ):
        return "continuation"
    return "neutral"


def annotate_flags(snap: PremarketSnapshot, settings: dict) -> None:
    """Context flags — informational, never gates (screener-only scope)."""
    gap_abs = abs(snap.gap_pct or 0.0)
    if gap_abs >= float(settings.get("fade_extreme_gap_pct", 20.0)):
        snap.flags.append("extreme_gap")
    if not liquidity_floor_passed(snap, settings):
        snap.flags.append("below_liquidity_floor")
    micro = snap.float_shares is not None and snap.float_shares < float(
        settings.get("micro_float_shares", 10_000_000)
    )
    if micro:
        snap.flags.append("micro_float")
    if micro and snap.pm_volume >= float(settings.get("fade_pm_volume_shares", 5_000_000)):
        # SmallCapLab: 5M+ premarket shares in the gapper cohort → 71.5% fade.
        snap.flags.append("crowded_micro_float")
    if snap.rvol is None:
        snap.flags.append("rvol_unavailable")
    if snap.dilution_news:
        # Offerings/warrants/reverse splits: a measured bearish overhang for
        # gappers — surfaced loudly, still never a gate (screener-only scope).
        snap.flags.append("dilution_news")


def apply_penny_profile(settings: dict) -> dict:
    """Swap the screener's GATES for the penny hard rails — labels stay honest.

    Reads the ``penny:`` block (via config accessors) as the single source of
    truth: price band, market-cap floor (the sub-$50M pump zone stays cut), and
    the share-volume floor as the ADV backstop. Pre-market-specific floors are
    scaled-down defaults (a PM session is a fraction of a day), tunable via
    ``premarket.penny_overrides``. Deliberately NOT touched: the scoring
    liquidity floor, the fade_risk/crowded_micro_float tagging, and every
    disclosure — the profile changes what qualifies, never what the evidence
    says about it. Discovery is broad; the labels stay strict.
    """
    out = dict(settings)
    out["min_price"] = float(config.PENNY_MIN_PRICE)
    out["max_price"] = float(config.PENNY_MAX_PRICE)
    out["min_market_cap"] = float(config.PENNY_MIN_MARKET_CAP)
    out["min_adv_20d"] = float(config.PENNY_MIN_SHARE_VOLUME)
    # PM-session floors: defaults keep a real liquidity bar without demanding a
    # full day's dollar volume before 09:30.
    out["min_pm_volume"] = 150_000
    out["min_pm_dollar_volume"] = 300_000
    overrides = settings.get("penny_overrides") or {}
    for key, val in overrides.items():
        out[key] = val
    out["profile"] = "penny"
    return out


# ── orchestration ─────────────────────────────────────────────────────────────

def scan_premarket(
    tickers: list[str],
    *,
    now: Optional[datetime] = None,
    settings: Optional[dict] = None,
    snapshot_fn: Optional[Callable[..., Optional[PremarketSnapshot]]] = None,
    penny: Optional[bool] = None,
) -> dict[str, Any]:
    """Run the pre-market screen over ``tickers`` and return the ranked result.

    Output contract: ``candidates`` (gate-passing, score-desc, capped),
    ``near_misses`` (failed exactly one gate — the "almost qualifiers" pattern),
    counts, and ``disclosures`` restating what this is and is not.
    """
    settings = dict(settings or config.PREMARKET_SETTINGS)
    # Penny profile: explicit flag wins; otherwise follows the penny master
    # switch so a penny-mode operator gets penny-band discovery by default.
    if penny is None:
        penny = bool(config.ALLOW_PENNY_STOCKS)
    if penny:
        settings = apply_penny_profile(settings)
    # Cap the universe with DISCLOSURE, never silently: each snapshot costs up
    # to four provider calls, so an uncapped SP500 pass is thousands of network
    # round-trips — past the open for a CLI run, past the WSGI timeout for the
    # dashboard. Operators widen the cap deliberately (premarket.universe_cap).
    cap_universe = int(settings.get("universe_cap", 100))
    truncated_from = None
    if cap_universe > 0 and len(tickers) > cap_universe:
        truncated_from = len(tickers)
        tickers = list(tickers)[:cap_universe]
        log.warning(
            f"premarket-scan: universe capped at {cap_universe} of {truncated_from} "
            "tickers (premarket.universe_cap) — the alphabetical head, a subsample."
        )
    now_utc = now or datetime.now(timezone.utc)
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    now_et = now_utc.astimezone(_EASTERN)
    build = snapshot_fn or build_snapshot

    # Snapshots fan out through a bounded worker pool (the codebase's standard
    # thread-pool idiom; the provider-level semaphore still caps upstream
    # concurrency). Sequential snapshots over a full universe — each up to four
    # provider calls — would stretch a cold SP500 scan past the open and defeat
    # the staged 7:00/8:45/9:15 workflow (Codex P1). Results are collected then
    # sorted, so ordering stays deterministic regardless of completion order.
    workers = max(1, int(settings.get("workers", 8)))

    def _safe_build(ticker: str) -> Optional[PremarketSnapshot]:
        try:
            return build(ticker, now_et=now_et, settings=settings)
        except Exception as exc:
            log.debug(f"{ticker}: premarket snapshot failed ({exc})")
            return None

    snaps: list[PremarketSnapshot] = []
    if workers == 1 or len(tickers) <= 1:
        results = [_safe_build(t) for t in tickers]
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(_safe_build, tickers))
    snaps = [s for s in results if s is not None]

    candidates: list[dict[str, Any]] = []
    near_misses: list[dict[str, Any]] = []
    scanned = 0
    for snap in snaps:
        scanned += 1
        passed, reasons = passes_gates(snap, settings)
        annotate_flags(snap, settings)
        row = {
            "ticker": snap.ticker,
            "gap_pct": None if snap.gap_pct is None else round(snap.gap_pct, 2),
            "last_price": snap.last_price,
            "prev_close": snap.prev_close,
            "pm_volume": int(snap.pm_volume),
            "pm_dollar_volume": round(snap.pm_dollar_volume, 0),
            "pm_high": snap.pm_high,
            "pm_low": snap.pm_low,
            "prev_day_high": snap.prev_day_high,
            "prev_day_low": snap.prev_day_low,
            "rvol": None if snap.rvol is None else round(snap.rvol, 2),
            "rvol_basis": snap.rvol_basis,
            "float_shares": snap.float_shares,
            "float_rotation": (
                None if snap.float_rotation is None else round(snap.float_rotation, 4)
            ),
            "market_cap": snap.market_cap,
            "adv_20d": snap.adv_20d,
            "catalyst": snap.catalyst,
            "catalyst_headline": snap.catalyst_headline,
            "catalyst_provider": snap.catalyst_provider,
            "days_to_earnings": snap.days_to_earnings,
            "profile": classify_profile(snap, settings),
            "score": score_snapshot(snap, settings),
            "flags": snap.flags,
            "data_notes": snap.data_notes,
        }
        if passed:
            candidates.append(row)
        elif len(reasons) == 1:
            row["failed_gate"] = reasons[0]
            near_misses.append(row)

    candidates.sort(key=lambda r: (-r["score"], r["ticker"]))
    near_misses.sort(key=lambda r: (-r["score"], r["ticker"]))
    cap = int(settings.get("max_results", 20))
    return {
        "as_of_et": now_et.isoformat(),
        "profile_gates": "penny" if penny else "standard",
        "universe_truncated_from": truncated_from,
        "scanned_with_premarket_data": scanned,
        "candidates": candidates[:cap],
        "near_misses": near_misses[: max(5, cap // 2)],
        "disclosures": [
            "Screener output: a ranked research watchlist, not trade signals. "
            "No validated edge is claimed for this ranking; the composite "
            "weights encode the direction of published evidence only.",
            "Buying gap-ups at the open is, unconditionally, ~zero-to-negative "
            "expectancy in the academic record; profile=continuation marks the "
            "only factor combination the measured evidence supports, and "
            "profile=fade_risk marks the measured fade cohort.",
            "Catalyst detection covers the earnings calendar plus, when "
            "premarket.news is enabled, a keyword-classified headline feed "
            "(a deterministic heuristic, not verification); catalyst=unknown "
            "still does NOT mean no news exists.",
            "Free-tier data caveats apply pre-market (sparse IEX prints, "
            "delayed yfinance extended bars); see per-row data_notes.",
        ],
    }
