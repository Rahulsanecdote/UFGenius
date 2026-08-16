"""Tests for the pre-market gap screener (src/scanner/premarket_scan.py).

All offline: synthetic extended-hours frames with naive-UTC indexes (the fetch
layer's contract), sliced into ET sessions by the module under test. Covers the
session math (including a DST boundary), the time-of-day RVOL formula and its
clamps, gap arithmetic, gates, the evidence-shaped scoring (RVOL earns nothing
below the liquidity floor; extreme gaps score below moderate ones), profile
tagging, and the ranked orchestration contract.
"""

from __future__ import annotations

from datetime import date, datetime, time as dtime, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from src.scanner.premarket_scan import (
    PremarketSnapshot,
    build_snapshot,
    classify_profile,
    cumulative_volume_through,
    passes_gates,
    premarket_bars,
    scan_premarket,
    score_snapshot,
    time_of_day_rvol,
    _gap_band_score,
)

ET = ZoneInfo("America/New_York")

# Baseline settings for tests: module defaults, spelled out so a config.yaml
# edit can't silently change what these tests assert.
SETTINGS = {
    "interval": "5m", "period": "15d", "max_results": 20,
    "min_gap_pct": 4.0, "min_price": 2.0, "max_price": 100.0,
    "min_pm_volume": 100_000, "min_pm_dollar_volume": 1_000_000,
    "min_adv_20d": 500_000,
    "rvol_min_history_sessions": 2, "rvol_min_baseline_shares": 10_000,
    "liquidity_floor_price": 10.0, "liquidity_floor_adv": 1_000_000,
    "micro_float_shares": 10_000_000,
    "gap_score_min": 4.0, "gap_score_peak_lo": 6.0, "gap_score_peak_hi": 15.0,
    "gap_score_extreme": 30.0, "gap_score_floor_beyond": 0.2,
    "weights": {"rvol": 0.35, "gap_band": 0.20, "dollar_volume": 0.15,
                "float_rotation": 0.15, "catalyst": 0.15},
    "rvol_scale": 5.0, "dollar_volume_scale": 5_000_000, "rotation_scale": 0.25,
    "fade_extreme_gap_pct": 20.0, "fade_pm_volume_shares": 5_000_000,
    "continuation_min_rvol": 2.0, "catalyst_earnings_window_days": 1,
}


def _utc_naive(et_dt: datetime) -> pd.Timestamp:
    """An ET wall-clock instant as the naive-UTC timestamp the fetch layer serves."""
    return pd.Timestamp(et_dt.replace(tzinfo=ET).astimezone(timezone.utc).replace(tzinfo=None))


def make_frame(rows: list[tuple[datetime, float, float]]) -> pd.DataFrame:
    """Frame from (ET wall-clock datetime, close, volume) rows, naive-UTC index."""
    idx = [_utc_naive(dt) for dt, _, _ in rows]
    closes = [c for _, c, _ in rows]
    vols = [v for _, _, v in rows]
    return pd.DataFrame(
        {"Open": closes, "High": closes, "Low": closes, "Close": closes, "Volume": vols},
        index=pd.DatetimeIndex(idx),
    )


def make_daily(closes: dict[date, float], volume: float = 2_000_000) -> pd.DataFrame:
    idx = pd.DatetimeIndex([pd.Timestamp(d) for d in sorted(closes)])
    vals = [closes[d] for d in sorted(closes)]
    return pd.DataFrame({"Close": vals, "Volume": [volume] * len(vals)}, index=idx)


# ── session math ──────────────────────────────────────────────────────────────

class TestSessionMath:
    def test_naive_utc_index_lands_on_the_right_et_session(self):
        # 8:00 ET on a summer date is 12:00 UTC — date-slicing UTC would work
        # here, but the WINTER case below is where naive date-slicing breaks.
        frame = make_frame([(datetime(2026, 7, 15, 8, 0), 10.0, 1000)])
        pm = premarket_bars(frame, date(2026, 7, 15))
        assert len(pm) == 1

    def test_dst_winter_offset_is_respected(self):
        # 4:30 ET in January is 9:30 UTC (EST, UTC-5) — a bar that naive
        # UTC-date logic could misassign if it assumed a fixed offset.
        frame = make_frame([(datetime(2026, 1, 15, 4, 30), 5.0, 500)])
        pm = premarket_bars(frame, date(2026, 1, 15))
        assert len(pm) == 1
        assert pm.index[0].hour == 4 and pm.index[0].minute == 30  # ET clock

    def test_rth_open_bar_is_excluded(self):
        frame = make_frame([
            (datetime(2026, 7, 15, 9, 25), 10.0, 100),
            (datetime(2026, 7, 15, 9, 30), 10.0, 999),   # RTH open — not premarket
        ])
        pm = premarket_bars(frame, date(2026, 7, 15))
        assert len(pm) == 1
        assert float(pm["Volume"].sum()) == 100

    def test_before_4am_excluded_and_other_days_excluded(self):
        frame = make_frame([
            (datetime(2026, 7, 15, 3, 55), 10.0, 111),   # overnight session
            (datetime(2026, 7, 14, 8, 0), 10.0, 222),    # prior day
            (datetime(2026, 7, 15, 8, 0), 10.0, 333),
        ])
        pm = premarket_bars(frame, date(2026, 7, 15))
        assert float(pm["Volume"].sum()) == 333

    def test_aware_index_also_accepted(self):
        idx = pd.DatetimeIndex([pd.Timestamp("2026-07-15 12:00", tz="UTC")])
        frame = pd.DataFrame(
            {"Open": [1.0], "High": [1.0], "Low": [1.0], "Close": [1.0], "Volume": [10]},
            index=idx,
        )
        assert len(premarket_bars(frame, date(2026, 7, 15))) == 1

    def test_cumulative_volume_respects_cutoff(self):
        pm = premarket_bars(make_frame([
            (datetime(2026, 7, 15, 7, 0), 10.0, 100),
            (datetime(2026, 7, 15, 8, 0), 10.0, 200),
            (datetime(2026, 7, 15, 9, 0), 10.0, 400),
        ]), date(2026, 7, 15))
        assert cumulative_volume_through(pm, dtime(8, 0)) == 300.0
        assert cumulative_volume_through(pm, dtime(9, 30)) == 700.0


# ── time-of-day RVOL ─────────────────────────────────────────────────────────

def _rvol_frame(today_vol: float, prior_vols: list[float]) -> pd.DataFrame:
    rows = []
    for i, v in enumerate(prior_vols):
        rows.append((datetime(2026, 7, 6 + i, 8, 0), 10.0, v))
    rows.append((datetime(2026, 7, 15, 8, 0), 10.0, today_vol))
    return make_frame(rows)


class TestTimeOfDayRvol:
    def test_ratio_vs_same_window_mean(self):
        frame = _rvol_frame(300_000, [100_000, 100_000, 100_000])
        rvol, basis = time_of_day_rvol(
            frame, date(2026, 7, 15), dtime(8, 30),
            min_history_sessions=2, min_baseline_shares=10_000,
        )
        assert basis == "time_of_day"
        assert rvol == pytest.approx(3.0)

    def test_thin_baseline_yields_none_not_a_mirage(self):
        # 100K over a 5K baseline is "20x" — the ratio pathology the guides
        # warn about. The module must decline to report it.
        frame = _rvol_frame(100_000, [5_000, 5_000, 5_000])
        rvol, basis = time_of_day_rvol(
            frame, date(2026, 7, 15), dtime(8, 30),
            min_history_sessions=2, min_baseline_shares=10_000,
        )
        assert rvol is None
        assert basis == "thin_baseline"

    def test_insufficient_history_yields_none(self):
        frame = _rvol_frame(100_000, [50_000])
        rvol, basis = time_of_day_rvol(
            frame, date(2026, 7, 15), dtime(8, 30),
            min_history_sessions=2, min_baseline_shares=10_000,
        )
        assert rvol is None
        assert basis == "insufficient_history"


# ── snapshot construction ────────────────────────────────────────────────────

def _standard_inputs(
    *,
    last: float = 10.8,
    prev_close: float = 10.0,
    pm_volume: float = 400_000,
    float_shares: float | None = 40_000_000,
    days_to_earnings: int | None = None,
):
    intraday = make_frame([
        (datetime(2026, 7, 13, 8, 0), prev_close, 120_000),
        (datetime(2026, 7, 14, 8, 0), prev_close, 120_000),
        (datetime(2026, 7, 15, 8, 0), last, pm_volume),
    ])
    daily = make_daily({date(2026, 7, 13): prev_close * 0.99, date(2026, 7, 14): prev_close})
    info = {"floatShares": float_shares, "sharesOutstanding": 50_000_000}
    if float_shares is None:
        info = {"sharesOutstanding": 50_000_000}
    return dict(
        now_et=datetime(2026, 7, 15, 8, 30, tzinfo=ET),
        settings=dict(SETTINGS),
        intraday=intraday,
        daily=daily,
        info=info,
        days_to_earnings=lambda _t: days_to_earnings,
    )


class TestBuildSnapshot:
    def test_gap_is_vs_previous_regular_close(self):
        snap = build_snapshot("GAPR", **_standard_inputs(last=10.8, prev_close=10.0))
        assert snap is not None
        assert snap.gap_pct == pytest.approx(8.0)
        assert snap.prev_close == pytest.approx(10.0)

    def test_dollar_volume_is_price_times_shares(self):
        snap = build_snapshot("GAPR", **_standard_inputs(last=10.8, pm_volume=400_000))
        assert snap.pm_dollar_volume == pytest.approx(10.8 * 400_000)

    def test_float_rotation_uses_float_shares(self):
        snap = build_snapshot("GAPR", **_standard_inputs(pm_volume=400_000, float_shares=4_000_000))
        assert snap.float_rotation == pytest.approx(0.1)

    def test_shares_outstanding_fallback_is_disclosed(self):
        snap = build_snapshot("GAPR", **_standard_inputs(float_shares=None))
        assert snap.float_shares == 50_000_000
        assert "float:shares_outstanding_fallback" in snap.data_notes

    def test_rvol_is_wired_through_build_snapshot(self):
        # Two prior sessions at 120K each vs 400K today at the 8:30 cutoff:
        # rvol = 400/120 ≈ 3.33, basis time_of_day, and NO rvol data note.
        snap = build_snapshot("RVOL", **_standard_inputs(pm_volume=400_000))
        assert snap.rvol_basis == "time_of_day"
        assert snap.rvol == pytest.approx(400_000 / 120_000)
        assert not any(n.startswith("rvol:") for n in snap.data_notes)

    def test_degraded_rvol_basis_is_noted(self):
        inputs = _standard_inputs()
        inputs["intraday"] = make_frame([
            (datetime(2026, 7, 14, 8, 0), 10.0, 120_000),   # only ONE prior session
            (datetime(2026, 7, 15, 8, 0), 10.8, 400_000),
        ])
        snap = build_snapshot("RVIH", **inputs)
        assert snap.rvol is None
        assert snap.rvol_basis == "insufficient_history"
        assert "rvol:insufficient_history" in snap.data_notes

    def test_earnings_within_window_tags_catalyst(self):
        snap = build_snapshot("GAPR", **_standard_inputs(days_to_earnings=0))
        assert snap.catalyst == "earnings"
        far = build_snapshot("GAPR", **_standard_inputs(days_to_earnings=10))
        assert far.catalyst == "unknown"

    def test_no_premarket_bars_returns_none(self):
        inputs = _standard_inputs()
        inputs["intraday"] = make_frame([(datetime(2026, 7, 14, 8, 0), 10.0, 1000)])
        assert build_snapshot("EMPT", **inputs) is None

    def test_bars_past_the_scan_cutoff_are_excluded_everywhere(self):
        # Codex P1 regression: a fetch completing after the scan's as-of time
        # can carry later bars; every field must respect the declared cutoff,
        # not just share volume — otherwise gates and scores mix as-of times.
        inputs = _standard_inputs()
        inputs["intraday"] = make_frame([
            (datetime(2026, 7, 13, 8, 0), 10.0, 120_000),
            (datetime(2026, 7, 14, 8, 0), 10.0, 120_000),
            (datetime(2026, 7, 15, 8, 0), 10.8, 400_000),
            (datetime(2026, 7, 15, 8, 45), 99.0, 5_000_000),  # after 8:30 cutoff
        ])
        snap = build_snapshot("LATE", **inputs)
        assert snap.last_price == pytest.approx(10.8)          # not 99.0
        assert snap.gap_pct == pytest.approx(8.0)
        assert snap.pm_volume == 400_000                        # not 5.4M
        assert snap.pm_dollar_volume == pytest.approx(10.8 * 400_000)

    def test_adv_keeps_yesterday_when_no_session_date_row_exists(self):
        # Codex P2 regression: pre-market the daily frame usually has no row
        # for today, so a blind iloc[:-1] would drop yesterday's completed bar.
        inputs = _standard_inputs()
        inputs["daily"] = make_daily(
            {date(2026, 7, d): 10.0 for d in range(7, 15)}, volume=1_000_000
        )
        snap = build_snapshot("ADVY", **inputs)
        assert snap.adv_20d == pytest.approx(1_000_000)  # all 8 completed bars kept

    def test_adv_drops_a_session_date_partial_row_by_date(self):
        closes = {date(2026, 7, d): 10.0 for d in range(7, 15)}
        daily = make_daily(closes, volume=1_000_000)
        partial = make_daily({date(2026, 7, 15): 10.5}, volume=37)  # today, partial
        inputs = _standard_inputs()
        inputs["daily"] = pd.concat([daily, partial])
        snap = build_snapshot("ADVP", **inputs)
        assert snap.adv_20d == pytest.approx(1_000_000)  # partial row excluded


# ── gates ────────────────────────────────────────────────────────────────────

def _snap(**over) -> PremarketSnapshot:
    base = dict(
        ticker="TCKR", gap_pct=8.0, last_price=12.0, prev_close=11.1,
        pm_volume=400_000, pm_dollar_volume=4_800_000, rvol=3.0,
        rvol_basis="time_of_day", float_shares=40_000_000,
        float_rotation=0.01, adv_20d=2_000_000, catalyst="unknown",
    )
    base.update(over)
    return PremarketSnapshot(**base)


class TestGates:
    def test_all_pass(self):
        ok, reasons = passes_gates(_snap(), SETTINGS)
        assert ok and reasons == []

    @pytest.mark.parametrize("field,value,reason", [
        ("gap_pct", 2.0, "gap_below_min"),
        ("last_price", 1.5, "price_below_min"),
        ("last_price", 150.0, "price_above_max"),
        ("pm_volume", 50_000, "pm_volume_below_min"),
        ("pm_dollar_volume", 500_000, "pm_dollar_volume_below_min"),
        ("adv_20d", 100_000, "adv_below_min"),
    ])
    def test_each_gate_fails_with_its_reason(self, field, value, reason):
        ok, reasons = passes_gates(_snap(**{field: value}), SETTINGS)
        assert not ok
        assert reason in reasons

    def test_missing_price_or_gap_is_a_hard_no(self):
        ok, reasons = passes_gates(_snap(gap_pct=None), SETTINGS)
        assert not ok and reasons == ["no_price_or_gap"]


# ── scoring shape (the evidence encoded) ─────────────────────────────────────

class TestScoringShape:
    def test_gap_band_rises_peaks_then_declines(self):
        s = SETTINGS
        assert _gap_band_score(3.0, s) == 0.0
        assert _gap_band_score(10.0, s) == 1.0
        assert _gap_band_score(25.0, s) < 1.0          # decaying past peak_hi
        assert _gap_band_score(40.0, s) == pytest.approx(0.2)  # floor at extreme
        # An extreme gap must NOT outscore a moderate one.
        assert _gap_band_score(25.0, s) < _gap_band_score(10.0, s)

    def test_rvol_earns_nothing_below_the_liquidity_floor(self):
        # Identical RVOL; the only difference is the liquidity floor. The
        # sign-conditional evidence says the illiquid one must score lower.
        liquid = _snap(last_price=15.0, adv_20d=2_000_000, rvol=5.0)
        illiquid = _snap(last_price=4.0, adv_20d=200_000, rvol=5.0)
        assert score_snapshot(liquid, SETTINGS) > score_snapshot(illiquid, SETTINGS)

    def test_catalyst_raises_score(self):
        without = _snap()
        with_cat = _snap(catalyst="earnings")
        assert score_snapshot(with_cat, SETTINGS) > score_snapshot(without, SETTINGS)

    def test_zero_weights_do_not_crash(self):
        settings = dict(SETTINGS, weights={"rvol": 0, "gap_band": 0, "dollar_volume": 0,
                                           "float_rotation": 0, "catalyst": 0})
        assert score_snapshot(_snap(), settings) == 0.0


class TestProfiles:
    def test_continuation_needs_catalyst_liquidity_and_rvol(self):
        snap = _snap(last_price=15.0, adv_20d=2_000_000, rvol=3.0, catalyst="earnings")
        assert classify_profile(snap, SETTINGS) == "continuation"

    def test_fade_risk_is_the_measured_cohort(self):
        snap = _snap(last_price=4.0, adv_20d=300_000, gap_pct=45.0,
                     float_shares=5_000_000, catalyst="unknown")
        assert classify_profile(snap, SETTINGS) == "fade_risk"

    def test_everything_else_is_neutral(self):
        assert classify_profile(_snap(), SETTINGS) == "neutral"
        # catalyst but illiquid → neutral, not continuation
        snap = _snap(last_price=4.0, adv_20d=300_000, catalyst="earnings")
        assert classify_profile(snap, SETTINGS) == "neutral"


# ── orchestration ────────────────────────────────────────────────────────────

class TestScanPremarket:
    def _fake_builder(self, table: dict):
        def build(ticker, *, now_et, settings):
            return table.get(ticker)
        return build

    def test_ranked_capped_and_deterministic(self):
        table = {
            "AAA": _snap(ticker="AAA", rvol=1.0),
            "BBB": _snap(ticker="BBB", rvol=8.0, catalyst="earnings", last_price=15.0),
            "CCC": _snap(ticker="CCC", rvol=1.0),  # identical to AAA → ticker tiebreak
        }
        out = scan_premarket(
            ["AAA", "BBB", "CCC"],
            now=datetime(2026, 7, 15, 12, 30, tzinfo=timezone.utc),
            settings=dict(SETTINGS, max_results=2),
            snapshot_fn=self._fake_builder(table),
        )
        assert [r["ticker"] for r in out["candidates"]] == ["BBB", "AAA"]
        assert out["scanned_with_premarket_data"] == 3

    def test_single_gate_failures_surface_as_near_misses(self):
        table = {"MISS": _snap(ticker="MISS", pm_volume=50_000)}
        out = scan_premarket(
            ["MISS"], now=datetime(2026, 7, 15, 12, 30, tzinfo=timezone.utc),
            settings=dict(SETTINGS), snapshot_fn=self._fake_builder(table),
        )
        assert out["candidates"] == []
        assert out["near_misses"][0]["failed_gate"] == "pm_volume_below_min"

    def test_multi_gate_failures_are_dropped_silently(self):
        table = {"BAD": _snap(ticker="BAD", pm_volume=1_000, pm_dollar_volume=5_000)}
        out = scan_premarket(
            ["BAD"], now=datetime(2026, 7, 15, 12, 30, tzinfo=timezone.utc),
            settings=dict(SETTINGS), snapshot_fn=self._fake_builder(table),
        )
        assert out["candidates"] == [] and out["near_misses"] == []

    def test_snapshot_errors_do_not_kill_the_scan(self):
        def build(ticker, *, now_et, settings):
            if ticker == "BOOM":
                raise RuntimeError("provider exploded")
            return _snap(ticker=ticker)
        out = scan_premarket(
            ["BOOM", "OKAY"], now=datetime(2026, 7, 15, 12, 30, tzinfo=timezone.utc),
            settings=dict(SETTINGS), snapshot_fn=build,
        )
        assert [r["ticker"] for r in out["candidates"]] == ["OKAY"]

    def test_parallel_fanout_matches_sequential_output(self):
        # Codex P1 regression: the bounded worker pool must not change results
        # or ordering — collected snapshots are sorted after the fan-out.
        table = {
            t: _snap(ticker=t, rvol=float(i))
            for i, t in enumerate(["AAA", "BBB", "CCC", "DDD", "EEE"], start=1)
        }
        seq = scan_premarket(
            list(table), now=datetime(2026, 7, 15, 12, 30, tzinfo=timezone.utc),
            settings=dict(SETTINGS, workers=1), snapshot_fn=self._fake_builder(table),
        )
        par = scan_premarket(
            list(table), now=datetime(2026, 7, 15, 12, 30, tzinfo=timezone.utc),
            settings=dict(SETTINGS, workers=4), snapshot_fn=self._fake_builder(table),
        )
        assert [r["ticker"] for r in par["candidates"]] == [r["ticker"] for r in seq["candidates"]]
        assert par["scanned_with_premarket_data"] == seq["scanned_with_premarket_data"] == 5

    def test_disclosures_always_present(self):
        out = scan_premarket(
            [], now=datetime(2026, 7, 15, 12, 30, tzinfo=timezone.utc),
            settings=dict(SETTINGS), snapshot_fn=self._fake_builder({}),
        )
        assert any("not trade signals" in d for d in out["disclosures"])
        assert any("zero-to-negative" in d for d in out["disclosures"])

    def test_crowded_micro_float_flag(self):
        table = {"PUMP": _snap(
            ticker="PUMP", float_shares=5_000_000, pm_volume=6_000_000,
            last_price=4.0, adv_20d=300_000, gap_pct=25.0,
            pm_dollar_volume=24_000_000,
        )}
        out = scan_premarket(
            ["PUMP"], now=datetime(2026, 7, 15, 12, 30, tzinfo=timezone.utc),
            settings=dict(SETTINGS), snapshot_fn=self._fake_builder(table),
        )
        # The measured fade cohort typically also fails a liquidity gate — this
        # fixture misses exactly one (ADV), so it surfaces as a near-miss with
        # its warning flags intact rather than as a candidate.
        row = out["near_misses"][0]
        assert row["failed_gate"] == "adv_below_min"
        assert "crowded_micro_float" in row["flags"]
        assert row["profile"] == "fade_risk"


# ── config plumbing ──────────────────────────────────────────────────────────

def test_config_settings_block_is_wired():
    from src.utils import config
    assert isinstance(config.PREMARKET_SETTINGS, dict)
    # The shipped config.yaml carries the block with the documented defaults.
    assert config.PREMARKET_SETTINGS.get("min_gap_pct") == 4.0
    assert config.PREMARKET_SETTINGS.get("weights", {}).get("rvol") == 0.35
