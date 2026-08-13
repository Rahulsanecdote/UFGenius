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
    base = dict(FMP_KEY="k", MOVERS_SOURCES=["gainers", "losers", "most_actives"],
                MOVERS_MIN_PRICE=1.0, MOVERS_MAX_PRICE=0.0, MOVERS_MIN_CHANGE_PCT=3.0,
                MOVERS_LIMIT=40, MOVERS_INCLUDE_SHORT=True)
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
