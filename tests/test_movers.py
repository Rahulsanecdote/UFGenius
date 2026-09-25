"""Tests for the MOVERS discovery source (src/scanner/movers.py).

Hermetic: the FMP HTTP boundary is mocked, so no key or network is needed.
"""

from unittest.mock import MagicMock, patch

import src.utils.config as cfg
from src.scanner import movers as mv

# Per-endpoint fake FMP payloads (fields match /stable/biggest-* & most-actives).
_FAKE = {
    "biggest-gainers": [
        {"symbol": "BULL", "price": 12.0, "name": "Bull Co", "changesPercentage": 30.0},
        {"symbol": "PENNY", "price": 0.40, "name": "Penny Co", "changesPercentage": 80.0},
        {"symbol": "TINY", "price": 5.0, "name": "Tiny Co", "changesPercentage": 1.0},
    ],
    "biggest-losers": [
        {"symbol": "BEAR", "price": 20.0, "name": "Bear Co", "changesPercentage": -18.0},
    ],
    "most-actives": [
        {"symbol": "BULL", "price": 12.0, "name": "Bull Co", "changesPercentage": 30.0},
        {"symbol": "ACTV", "price": 50.0, "name": "Active Co", "changesPercentage": 6.0},
    ],
}


def _mock_session():
    """Session whose .get(url, ...) returns the right fake list per endpoint."""
    def _get(url, **kwargs):
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        endpoint = url.rstrip("/").rsplit("/", 1)[-1]
        resp.json.return_value = _FAKE.get(endpoint, [])
        return resp
    session = MagicMock()
    session.get.side_effect = _get
    return session


def _patched(**cfgover):
    """Context managers: real FMP key + default movers config (overridable)."""
    # Enrichment OFF for the discovery tests → hermetic (no intraday fetch); the
    # enrichment path has its own dedicated tests below.
    base = dict(FMP_KEY="k", MOVERS_SOURCES=["gainers", "losers", "most_actives"],
                MOVERS_PROVIDERS=["fmp"],
                MOVERS_MIN_PRICE=1.0, MOVERS_MAX_PRICE=0.0, MOVERS_MIN_CHANGE_PCT=3.0,
                MOVERS_LIMIT=40, MOVERS_INCLUDE_SHORT=True, MOVERS_ENRICH_INTRADAY=False)
    base.update(cfgover)
    patches = [patch.object(cfg, k, v) for k, v in base.items()]
    patches.append(patch("src.scanner.movers_providers.get_retry_session", return_value=_mock_session()))
    return patches


def _run(**cfgover):
    ps = _patched(**cfgover)
    for p in ps:
        p.start()
    try:
        return mv.fetch_market_movers()
    finally:
        for p in ps:
            p.stop()


def test_discovers_and_ranks_by_score():
    out = _run()
    tickers = [c.ticker for c in out]
    # BULL (gainer + most-active, +30%) should outrank ACTV (+6%).
    assert "BULL" in tickers and "BEAR" in tickers and "ACTV" in tickers
    assert out == sorted(out, key=lambda c: c.score, reverse=True)
    assert tickers[0] == "BULL"


def test_multi_source_corroboration_boost():
    out = {c.ticker: c for c in _run()}
    # BULL appears in gainers + most_actives → 2 sources, corroboration bonus.
    assert set(out["BULL"].sources) == {"gainers", "most_actives"}
    assert out["BULL"].score > mv._score(30.0, 1)  # more than a single-source +30%


def test_penny_filtered_by_min_price():
    tickers = [c.ticker for c in _run()]
    assert "PENNY" not in tickers          # $0.40 < min_price 1.0
    # ...but survives if the floor is dropped.
    tickers2 = [c.ticker for c in _run(MOVERS_MIN_PRICE=0.0)]
    assert "PENNY" in tickers2


def test_min_change_pct_filters_small_moves():
    tickers = [c.ticker for c in _run()]
    assert "TINY" not in tickers           # +1% < min_change_pct 3.0


def test_direction_long_and_short():
    out = {c.ticker: c for c in _run()}
    assert out["BULL"].direction == "long"
    assert out["BEAR"].direction == "short"


def test_short_setups_excluded_when_disabled():
    tickers = [c.ticker for c in _run(MOVERS_INCLUDE_SHORT=False)]
    assert "BEAR" not in tickers
    assert "BULL" in tickers


def test_limit_caps_results():
    out = _run(MOVERS_LIMIT=1)
    assert len(out) == 1 and out[0].ticker == "BULL"


def test_no_key_returns_empty():
    with patch.object(cfg, "FMP_KEY", ""):
        assert mv.fetch_market_movers() == []
        assert mv.get_movers_universe() == []


def test_graceful_on_request_error():
    session = MagicMock()
    session.get.side_effect = RuntimeError("network down")
    with patch.object(cfg, "FMP_KEY", "k"), \
         patch.object(cfg, "MOVERS_PROVIDERS", ["fmp"]), \
         patch.object(cfg, "MOVERS_HALTS_ENABLED", False), \
         patch("src.scanner.movers_providers.get_retry_session", return_value=session):
        assert mv.fetch_market_movers() == []   # never raises


def test_get_movers_universe_returns_symbols():
    ps = _patched()
    for p in ps:
        p.start()
    try:
        syms = mv.get_movers_universe()
    finally:
        for p in ps:
            p.stop()
    assert "BULL" in syms and all(isinstance(s, str) for s in syms)


# ── Phase 2: intraday enrichment / early-momentum ranking ────────────────────

def test_enriched_score_rewards_volume_and_alignment_over_raw_move():
    # A modest +6% move on heavy volume, strong aligned momentum, above VWAP...
    hot = mv._enriched_score("long", change_pct=6.0, rel_volume=5.0,
                             momentum_pct=3.0, vwap_pct=2.0, is_breakout=True)
    # ...beats a huge +40% move with no volume, fading momentum, below VWAP.
    cold = mv._enriched_score("long", change_pct=40.0, rel_volume=0.5,
                              momentum_pct=-2.0, vwap_pct=-3.0, is_breakout=False)
    assert hot > cold


def test_enriched_score_direction_aware_for_shorts():
    # For a SHORT, negative momentum + below VWAP is "aligned" and scores well.
    aligned = mv._enriched_score("short", change_pct=-10.0, rel_volume=3.0,
                                 momentum_pct=-4.0, vwap_pct=-3.0, is_breakout=False)
    counter = mv._enriched_score("short", change_pct=-10.0, rel_volume=3.0,
                                 momentum_pct=4.0, vwap_pct=3.0, is_breakout=False)
    assert aligned > counter


def _enrich_patches(metrics, vwap_val):
    """Patch the intraday helpers _enrich_candidate imports (lazily, inside it)."""
    import pandas as pd
    fake_df = pd.DataFrame({"Close": [1, 2], "High": [1, 2],
                            "Low": [1, 2], "Volume": [1, 2]})
    return [
        patch("src.data.fetcher.fetch_intraday", return_value=fake_df),
        patch("src.scanner.intraday_scan.score_intraday_frame", return_value=metrics),
        patch("src.technical.intraday_features.vwap", return_value=vwap_val),
    ]


def test_enrich_candidate_attaches_metrics_and_rescores():
    c = mv.MoverCandidate(ticker="X", price=10.0, change_pct=6.0,
                          direction="long", sources=["gainers"], base_score=20.0, score=20.0)
    metrics = {"last_price": 10.2, "rel_volume": 5.0, "momentum_pct": 3.0, "is_breakout": True}
    ps = _enrich_patches(metrics, vwap_val=10.0)  # last 10.2 vs vwap 10 → +2% above
    for p in ps:
        p.start()
    try:
        mv._enrich_candidate(c)
    finally:
        for p in ps:
            p.stop()
    assert c.enriched is True
    assert c.rel_volume == 5.0 and c.momentum_pct == 3.0 and c.is_breakout is True
    assert c.vwap_pct == 2.0
    assert c.score != c.base_score   # re-scored on intraday signals


def test_enrich_candidate_keeps_base_score_when_no_intraday_data():
    c = mv.MoverCandidate(ticker="X", price=10.0, change_pct=6.0,
                          direction="long", sources=["gainers"], base_score=20.0, score=20.0)
    ps = _enrich_patches(metrics=None, vwap_val=None)  # too few bars → None
    for p in ps:
        p.start()
    try:
        mv._enrich_candidate(c)
    finally:
        for p in ps:
            p.stop()
    assert c.enriched is False and c.score == 20.0   # unchanged


# ── corporate-action guard (reverse-split artifacts) ─────────────────────────
#
# The FMP lists report an UNADJUSTED quote change, so on a reverse-split
# effective date the mechanical price multiple is published as a move: AiRWA
# (YYAI) 1-for-20 on 2026-08-17 surfaced as "+1668%" against a real ~+23%.

from datetime import datetime, timedelta

import pandas as pd
import pytest

_SPLIT_FAKE = {
    "gainers": [
        {"symbol": "SPLT", "price": 1.13, "name": "Split Co", "changesPercentage": 1668.4},
        {"symbol": "REAL", "price": 12.0, "name": "Real Co", "changesPercentage": 30.0},
    ],
    "losers": [],
    "most_actives": [],
}


def _run_split(verified, payload=None, **cfgover):
    """Discovery over a payload holding one extreme (>= suspect) move.

    ``verified`` is what the split-adjusted recomputation returns (None = bars
    unavailable); ``payload`` overrides the default reverse-split fixture.
    Returns (candidates by ticker, tickers that were verified).
    """
    feed = _SPLIT_FAKE if payload is None else payload
    base = dict(FMP_KEY="k", MOVERS_SOURCES=["gainers", "losers", "most_actives"],
                MOVERS_PROVIDERS=["fmp"],
                MOVERS_MIN_PRICE=1.0, MOVERS_MAX_PRICE=0.0, MOVERS_MIN_CHANGE_PCT=3.0,
                MOVERS_LIMIT=40, MOVERS_INCLUDE_SHORT=True, MOVERS_ENRICH_INTRADAY=False,
                MOVERS_SUSPECT_CHANGE_PCT=300.0, MOVERS_SUSPECT_AGREEMENT_PCT=25.0)
    base.update(cfgover)
    patches = [patch.object(cfg, k, v) for k, v in base.items()]
    patches.append(patch.object(mv, "_fetch_source", lambda s: feed.get(s, [])))
    calls = []

    def _verify(ticker, price):
        calls.append((ticker, price))
        return verified

    patches.append(patch.object(mv, "_verified_change_pct", _verify))
    for p in patches:
        p.start()
    try:
        return {c.ticker: c for c in mv.fetch_market_movers()}, calls
    finally:
        for p in patches:
            p.stop()


def test_split_artifact_change_is_replaced_with_the_verified_value():
    out, calls = _run_split(-11.6)
    # Only the implausible one costs a fetch, and it is measured against the
    # feed's CURRENT quote, not a stale pair of closes.
    assert calls == [("SPLT", 1.13)]
    assert out["SPLT"].change_pct == pytest.approx(-11.6)
    assert out["SPLT"].change_verified is True
    assert out["SPLT"].direction == "short"        # direction follows the real sign
    # A plausible mover is untouched.
    assert out["REAL"].change_pct == 30.0
    assert out["REAL"].change_verified is False


def test_unverifiable_extreme_move_is_dropped():
    # Fail closed: an extreme claim we cannot check is not published.
    out, _ = _run_split(None)
    assert "SPLT" not in out
    assert "REAL" in out


# ── the third branch: extreme AND confirmed ──────────────────────────────────
#
# "Implausible" is not the same as "false". The guard used to drop on
# `abs(verified) >= suspect` even when the recomputation AGREED with the feed,
# so a real 442% move was indistinguishable from a reverse-split artifact.

_DAIC_FAKE = {
    # CID HoldCo, 2026-08-22: $0.426 → $2.31 on ~6M shares. Genuine.
    "gainers": [{"symbol": "DAIC", "price": 2.31, "name": "CID HoldCo",
                 "changesPercentage": 442.25}],
    "losers": [],
    "most_actives": [],
}


def test_extreme_move_confirmed_by_our_own_bars_is_kept():
    # Our split-adjusted bars independently reproduce the feed's number. That is
    # corroboration — a reverse split would have separated the two by the split
    # ratio, not matched them.
    out, calls = _run_split(442.25, payload=_DAIC_FAKE)
    assert calls == [("DAIC", 2.31)]
    assert "DAIC" in out
    assert out["DAIC"].change_pct == pytest.approx(442.25)
    assert out["DAIC"].change_verified is True
    assert out["DAIC"].direction == "long"


def test_confirmation_tolerates_a_small_basis_difference():
    # The two operands measure the same quote against different previous
    # closes, so an ordinary session leaves a little daylight between them.
    out, _ = _run_split(400.0, payload=_DAIC_FAKE)          # ~9.6% apart
    assert out["DAIC"].change_pct == pytest.approx(400.0)   # ours, not the feed's
    assert out["DAIC"].change_verified is True


def test_extreme_move_is_dropped_when_the_bars_disagree_and_are_also_extreme():
    # Feed says +1668%, our bars say +400%: two different implausible moves
    # means our bars have not picked the corporate action up either, so nothing
    # was verified and neither number can be published.
    out, _ = _run_split(400.0)
    assert "SPLT" not in out


def test_zero_agreement_tolerance_demands_an_exact_match():
    # Documented behaviour of the strictest setting: anything short of identical
    # counts as disagreement, restoring the old drop-every-extreme-move stance.
    out, _ = _run_split(442.0, payload=_DAIC_FAKE, MOVERS_SUSPECT_AGREEMENT_PCT=0.0)
    assert "DAIC" not in out


# ── withheld candidates are disclosed, not silently dropped ──────────────────

def test_unverifiable_move_is_disclosed_with_the_feed_s_claim():
    out, _ = _run_split(None, payload=_DAIC_FAKE)
    assert "DAIC" not in out                       # still not published
    (w,) = mv.last_withheld()                      # ...but no longer invisible
    assert w["ticker"] == "DAIC"
    assert w["reason"] == "unverifiable"
    assert w["reported_change_pct"] == pytest.approx(442.25)
    assert w["recomputed_change_pct"] is None      # there was nothing to compare
    assert w["sources"] == ["gainers"]


def test_conflicting_recomputation_is_disclosed_with_both_numbers():
    out, _ = _run_split(400.0)                     # vs the fixture's +1668.4
    assert "SPLT" not in out
    (w,) = mv.last_withheld()
    assert w["reason"] == "conflicting"
    assert w["reported_change_pct"] == pytest.approx(1668.4)
    assert w["recomputed_change_pct"] == pytest.approx(400.0)


def test_withheld_is_per_run_and_does_not_accumulate():
    _run_split(None, payload=_DAIC_FAKE)
    assert mv.last_withheld()
    _run_split(442.25, payload=_DAIC_FAKE)         # confirmed — nothing held back
    assert mv.last_withheld() == []


def test_withheld_disclosure_is_bounded():
    # A run withholding more than the cap has a systemic problem the first few
    # entries already show; the list must not grow without limit.
    rows = [{"symbol": f"X{i}", "price": 5.0, "changesPercentage": 900.0}
            for i in range(mv._MAX_WITHHELD + 5)]
    _run_split(None, payload={"gainers": rows, "losers": [], "most_actives": []})
    assert len(mv.last_withheld()) == mv._MAX_WITHHELD


def test_withheld_entries_are_copies():
    # last_source_health() list-copies its values; the dicts inside need copying
    # too or a caller's edit reaches back into this thread's state.
    _run_split(None, payload=_DAIC_FAKE)
    mv.last_source_health()["withheld"][0]["ticker"] = "MUTATED"
    mv.last_withheld()[0]["reason"] = "MUTATED"
    fresh = mv.last_withheld()[0]
    assert fresh["ticker"] == "DAIC"
    assert fresh["reason"] == "unverifiable"


@pytest.mark.parametrize("feed,verified,tol,expected", [
    (442.25, 442.25, 0.25, True),        # identical
    (442.25, 400.0, 0.25, True),         # 9.6% apart — same move, different basis
    (500.0, 300.0, 0.25, False),         # 40% apart — not the same move
    (1668.4, 23.0, 0.25, False),         # YYAI 1-for-20: separated by the ratio
    (442.0, -442.0, 0.25, False),        # opposite signs can never agree
    (0.0, 0.0, 0.25, True),              # both flat — nothing to disagree about
    (float("nan"), 10.0, 0.25, False),   # non-finite never agrees
    (float("inf"), 10.0, 0.25, False),
    (400.0, 10.0, 1.0, True),            # tol 1.0 ≈ "sharing a sign is enough"
])
def test_changes_agree(feed, verified, tol, expected):
    assert mv._changes_agree(feed, verified, tol) is expected


def test_corrected_move_below_the_floor_stops_being_a_mover():
    out, _ = _run_split(0.4)                       # < min_change_pct 3.0
    assert "SPLT" not in out


def test_guard_disabled_passes_the_raw_feed_value_through():
    out, calls = _run_split(-11.6, MOVERS_SUSPECT_CHANGE_PCT=0.0)
    assert calls == []                             # no verification attempted
    assert out["SPLT"].change_pct == pytest.approx(1668.4)
    assert out["SPLT"].change_verified is False


def _daily(closes, last_day_offset=1):
    """Daily frame whose final bar is `last_day_offset` days before today (ET)."""
    end = datetime.now(mv._EASTERN).date() - timedelta(days=last_day_offset)
    idx = pd.DatetimeIndex([pd.Timestamp(end) - pd.Timedelta(days=i)
                            for i in range(len(closes) - 1, -1, -1)])
    return pd.DataFrame({"Close": list(closes)}, index=idx)


def test_verified_change_pct_measures_the_quote_against_the_adjusted_prev_close():
    # YYAI shape: split-adjusted prior close 1.13, live quote 1.39 → +23%,
    # which is what a split-aware quote source reports (NOT the -11.6% that
    # the prior completed session would give).
    with patch("src.data.fetcher.fetch_ohlcv", return_value=_daily([1.278, 1.13])):
        assert mv._verified_change_pct("YYAI", 1.39) == pytest.approx(23.01, abs=0.01)


def test_verified_change_pct_skips_todays_own_bar():
    # Once the session has printed a bar, the reference is still the PREVIOUS
    # close — otherwise every candidate measures ~0% against itself.
    frame = _daily([1.13, 1.39], last_day_offset=0)   # final bar is today
    with patch("src.data.fetcher.fetch_ohlcv", return_value=frame):
        assert mv._verified_change_pct("YYAI", 1.39) == pytest.approx(23.01, abs=0.01)


@pytest.mark.parametrize("frame,price", [
    (pd.DataFrame(), 1.0),                    # no data at all
    (_daily([1.0]), 0.0),                     # unusable quote
    (_daily([0.0]), 1.0),                     # zero prior close — undefined
])
def test_verified_change_pct_returns_none_when_unusable(frame, price):
    with patch("src.data.fetcher.fetch_ohlcv", return_value=frame):
        assert mv._verified_change_pct("X", price) is None


def test_verified_change_pct_returns_none_when_only_todays_bar_exists():
    with patch("src.data.fetcher.fetch_ohlcv", return_value=_daily([1.0], last_day_offset=0)):
        assert mv._verified_change_pct("X", 1.0) is None


def test_verified_change_pct_never_raises():
    with patch("src.data.fetcher.fetch_ohlcv", side_effect=RuntimeError("provider down")):
        assert mv._verified_change_pct("X", 1.0) is None


def test_two_implausible_numbers_that_agree_are_treated_as_corroboration():
    """The accepted residual risk, pinned so a change to it is deliberate.

    If our own provider ALSO had not adjusted for the split, our recomputation
    would reproduce the feed's artifact exactly and the agreement would be
    spurious. The one documented instance runs the other way — on YYAI's
    effective date our bars were adjusted (+23%) while the feed was not
    (+1668%) — and the alternative (drop every extreme move) makes the scanner
    blind to precisely the names it exists to surface. Operators who want the
    old stance set ``suspect_agreement_pct: 0``.
    """
    out, _ = _run_split(1500.0)                # vs the feed's +1668.4 — 10% apart
    assert out["SPLT"].change_pct == pytest.approx(1500.0)
    assert out["SPLT"].change_verified is True


def test_merge_keeps_the_price_from_the_row_that_won_the_change():
    """Price and change must come from the SAME source row.

    The endpoints can carry different snapshots, and the verification measures
    the kept change's quote against our previous close — a price left over from
    the losing row would silently corrupt that recomputation (CodeRabbit).
    """
    payloads = {
        # Same ticker in two lists: the most-actives row carries the larger
        # move AND its own (different) price.
        "gainers": [{"symbol": "DUP", "price": 10.0, "changesPercentage": 20.0}],
        "most_actives": [{"symbol": "DUP", "price": 11.5, "changesPercentage": 38.0}],
        "losers": [],
    }
    base = dict(FMP_KEY="k", MOVERS_SOURCES=["gainers", "losers", "most_actives"],
                MOVERS_PROVIDERS=["fmp"],
                MOVERS_MIN_PRICE=1.0, MOVERS_MAX_PRICE=0.0, MOVERS_MIN_CHANGE_PCT=3.0,
                MOVERS_LIMIT=40, MOVERS_INCLUDE_SHORT=True, MOVERS_ENRICH_INTRADAY=False,
                MOVERS_SUSPECT_CHANGE_PCT=300.0)
    ps = [patch.object(cfg, k, v) for k, v in base.items()]
    ps.append(patch.object(mv, "_fetch_source", lambda s: payloads.get(s, [])))
    for p in ps:
        p.start()
    try:
        out = {c.ticker: c for c in mv.fetch_market_movers()}
    finally:
        for p in ps:
            p.stop()
    assert out["DUP"].change_pct == 38.0
    assert out["DUP"].price == 11.5          # not the 10.0 from the first row

# ── discovery-source health (observability) ──────────────────────────────────
#
# Every fetcher fails soft to [], so without an explicit signal a dead key or an
# exhausted quota is indistinguishable from a quiet market — the dashboard was
# rendering both as "nothing qualified".

def _run_with(session_factory, **cfgover):
    base = dict(FMP_KEY="k", MOVERS_SOURCES=["gainers"], MOVERS_PROVIDERS=["fmp"],
                MOVERS_ENRICH_INTRADAY=False, MOVERS_HALTS_ENABLED=False)
    base.update(cfgover)
    ps = [patch.object(cfg, k, v) for k, v in base.items()]
    ps.append(patch("src.scanner.movers_providers.get_retry_session", session_factory))
    for p in ps:
        p.start()
    try:
        return mv.fetch_market_movers()
    finally:
        for p in ps:
            p.stop()


def _json_session(payload, status_ok=True):
    def _factory():
        resp = MagicMock()
        resp.raise_for_status.return_value = None if status_ok else RuntimeError("http")
        resp.json.return_value = payload
        session = MagicMock()
        session.get.return_value = resp
        return session
    return _factory


def test_exception_is_recorded_as_a_source_failure():
    def _boom():
        raise RuntimeError("quota exceeded")
    assert _run_with(_boom) == []
    health = mv.last_source_health()
    assert health["failed"] == ["gainers: no_provider_answered"]
    assert health["succeeded"] == []
    assert health["attempted"] == ["gainers"]


def test_non_list_payload_is_a_failure_not_an_empty_market():
    # FMP answers an exhausted quota with HTTP 200 and a JSON *object*, so
    # nothing raises. Treating that as [] is exactly what made a dead quota
    # look like a quiet market.
    assert _run_with(_json_session({"Error Message": "Limit Reach..."})) == []
    health = mv.last_source_health()
    assert health["failed"] == ["gainers: no_provider_answered"]
    assert health["succeeded"] == []


def test_missing_key_is_recorded_as_a_failure():
    assert _run_with(_json_session([]), FMP_KEY="") == []
    assert mv.last_source_health()["failed"] == ["gainers: no_provider_configured"]


def test_healthy_empty_source_is_a_success_not_a_failure():
    # A genuinely quiet market: the source answered, it just had nothing.
    assert _run_with(_json_session([])) == []
    health = mv.last_source_health()
    assert health["succeeded"] == ["gainers"]
    assert health["failed"] == []


def test_health_resets_between_runs():
    def _boom():
        raise RuntimeError("down")
    _run_with(_boom)
    assert mv.last_source_health()["failed"]        # failed run recorded
    _run_with(_json_session([]))
    assert mv.last_source_health()["failed"] == []  # healthy run cleared it


def test_health_is_isolated_between_concurrent_runs():
    """On Render the in-process worker and a dashboard request share a process,
    so two discovery runs genuinely overlap. Module-global health let one run
    clobber the other's (CodeRabbit)."""
    import threading

    results = {}
    barrier = threading.Barrier(2)

    def _healthy():
        _run_with(_json_session([]))
        barrier.wait()                       # both mid-flight before reading
        results["healthy"] = mv.last_source_health()

    def _failing():
        def _boom():
            raise RuntimeError("down")
        _run_with(_boom)
        barrier.wait()
        results["failing"] = mv.last_source_health()

    threads = [threading.Thread(target=_healthy), threading.Thread(target=_failing)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results["healthy"]["failed"] == []
    assert results["failing"]["failed"] == ["gainers: no_provider_answered"]


# ── bar freshness (feeds MoversAlerter's require_fresh_session gate) ─────────


def _dated_frame(stamps):
    import pandas as pd
    n = len(stamps)
    return pd.DataFrame(
        {"Close": list(range(1, n + 1)), "High": list(range(1, n + 1)),
         "Low": list(range(1, n + 1)), "Volume": [100] * n},
        index=pd.DatetimeIndex(stamps),
    )


def test_enrich_records_the_last_bar_timestamp():
    """The gate is only as good as this: no timestamp, nothing ever alerts."""
    from datetime import datetime

    import pandas as pd

    c = mv.MoverCandidate(ticker="X", price=10.0, change_pct=6.0,
                          direction="long", sources=["gainers"], base_score=20.0, score=20.0)
    df = _dated_frame([datetime(2026, 9, 25, 13, 40), datetime(2026, 9, 25, 13, 45)])
    metrics = {"last_price": 10.2, "rel_volume": 5.0, "momentum_pct": 3.0, "is_breakout": True}
    ps = [
        patch("src.data.fetcher.fetch_intraday", return_value=df),
        patch("src.scanner.intraday_scan.score_intraday_frame", return_value=metrics),
        patch("src.technical.intraday_features.vwap", return_value=10.0),
    ]
    for p in ps:
        p.start()
    try:
        mv._enrich_candidate(c)
    finally:
        for p in ps:
            p.stop()
    assert c.bars_as_of == datetime(2026, 9, 25, 13, 45)      # LAST bar, not first
    assert c.as_dict()["bars_as_of"] == "2026-09-25T13:45:00"
    assert isinstance(pd.DatetimeIndex(df.index), pd.DatetimeIndex)


def test_last_bar_time_normalises_to_naive_utc():
    """fetch_intraday's convention is naive UTC; a tz-aware frame must match it."""
    from datetime import datetime, timezone

    aware = _dated_frame([datetime(2026, 9, 25, 13, 45, tzinfo=timezone.utc)])
    assert mv._last_bar_time(aware) == datetime(2026, 9, 25, 13, 45)


def test_last_bar_time_is_none_on_an_unusable_index():
    """A non-datetime index must not raise — it reads as unknown, and the
    alerter treats unknown as stale rather than as current."""
    import pandas as pd

    assert mv._last_bar_time(pd.DataFrame({"Close": []})) is None
