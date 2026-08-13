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
