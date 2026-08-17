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
                MOVERS_MIN_PRICE=1.0, MOVERS_MAX_PRICE=0.0, MOVERS_MIN_CHANGE_PCT=3.0,
                MOVERS_LIMIT=40, MOVERS_INCLUDE_SHORT=True, MOVERS_ENRICH_INTRADAY=False)
    base.update(cfgover)
    patches = [patch.object(cfg, k, v) for k, v in base.items()]
    patches.append(patch("src.scanner.movers.get_retry_session", return_value=_mock_session()))
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
         patch("src.scanner.movers.get_retry_session", return_value=session):
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


def _run_split(verified, **cfgover):
    """Discovery over a payload holding one reverse-split artifact.

    ``verified`` is what the split-adjusted recomputation returns (None = bars
    unavailable). Returns (candidates by ticker, tickers that were verified).
    """
    base = dict(FMP_KEY="k", MOVERS_SOURCES=["gainers", "losers", "most_actives"],
                MOVERS_MIN_PRICE=1.0, MOVERS_MAX_PRICE=0.0, MOVERS_MIN_CHANGE_PCT=3.0,
                MOVERS_LIMIT=40, MOVERS_INCLUDE_SHORT=True, MOVERS_ENRICH_INTRADAY=False,
                MOVERS_SUSPECT_CHANGE_PCT=300.0)
    base.update(cfgover)
    patches = [patch.object(cfg, k, v) for k, v in base.items()]
    patches.append(patch.object(mv, "_fetch_source", lambda s: _SPLIT_FAKE.get(s, [])))
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


def test_recomputation_that_is_also_implausible_fails_closed():
    # Provider split-adjustment can lag the effective date; if our own number
    # is still absurd we have verified nothing.
    out, _ = _run_split(1500.0)
    assert "SPLT" not in out


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
    base = dict(FMP_KEY="k", MOVERS_SOURCES=["gainers"], MOVERS_ENRICH_INTRADAY=False,
                MOVERS_HALTS_ENABLED=False)
    base.update(cfgover)
    ps = [patch.object(cfg, k, v) for k, v in base.items()]
    ps.append(patch("src.scanner.movers.get_retry_session", session_factory))
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
    assert health["failed"] == ["gainers: RuntimeError"]
    assert health["succeeded"] == []
    assert health["attempted"] == ["gainers"]


def test_non_list_payload_is_a_failure_not_an_empty_market():
    # FMP answers an exhausted quota with HTTP 200 and a JSON *object*, so
    # nothing raises. Treating that as [] is exactly what made a dead quota
    # look like a quiet market.
    assert _run_with(_json_session({"Error Message": "Limit Reach..."})) == []
    health = mv.last_source_health()
    assert health["failed"] == ["gainers: unexpected_payload"]
    assert health["succeeded"] == []


def test_missing_key_is_recorded_as_a_failure():
    assert _run_with(_json_session([]), FMP_KEY="") == []
    assert mv.last_source_health()["failed"] == ["gainers: no_api_key"]


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
    assert results["failing"]["failed"] == ["gainers: RuntimeError"]
