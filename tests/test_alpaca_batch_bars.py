"""Multi-symbol Alpaca bars for the scan's batch fetch.

The S&P scan fanned out one request per ticker — 503 — against Alpaca's 200
requests/minute. The paper trader's first scheduled scan logged 46 HTTP 429s in
its first minute; each fell through to yfinance, itself rate-limited, so some
names were scanned on no data. `/v2/stocks/bars?symbols=…` answers a chunk of
symbols in one paged response. These tests pin that the batch path only ever
REMOVES requests: what it cannot answer still goes through the old chain.
"""

from __future__ import annotations

import pandas as pd
import pytest

import src.utils.config as cfg
from src.data import fetcher


def _bar(day: int, close: float = 100.0) -> dict:
    return {"t": f"2026-09-{day:02d}T04:00:00Z", "o": close, "h": close + 1,
            "l": close - 1, "c": close, "v": 1000 + day}


class _Resp:
    def __init__(self, body, status=200):
        self._body, self.status_code = body, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._body


class _Alpaca:
    """Pages a multi-symbol response like Alpaca does: by symbol, then time."""

    def __init__(self, data: dict[str, list], page_size: int = 3, fail_chunk_with=None):
        self.data, self.page_size, self.fail_chunk_with = data, page_size, fail_chunk_with
        self.calls: list[dict] = []

    def get(self, url, headers=None, params=None, timeout=None):
        assert url.endswith("/v2/stocks/bars"), url
        self.calls.append(dict(params))
        syms = params["symbols"].split(",")
        if self.fail_chunk_with and self.fail_chunk_with in syms and params.get("page_token"):
            return _Resp({}, status=429)          # fails part-way through the chunk
        flat = [(s, b) for s in syms for b in self.data.get(s, [])]
        start = int(params.get("page_token") or 0)
        page = flat[start:start + self.page_size]
        bars: dict[str, list] = {}
        for s, b in page:
            bars.setdefault(s, []).append(b)
        nxt = start + self.page_size
        return _Resp({"bars": bars, "next_page_token": str(nxt) if nxt < len(flat) else None})


@pytest.fixture
def alpaca(monkeypatch):
    def _install(data, **kw):
        fake = _Alpaca(data, **kw)
        monkeypatch.setattr(cfg, "ALPACA_API_KEY", "k")
        monkeypatch.setattr(cfg, "ALPACA_SECRET_KEY", "s")
        monkeypatch.setattr(fetcher, "get_retry_session", lambda: fake)
        return fake
    return _install


def test_pages_are_merged_per_symbol(alpaca):
    fake = alpaca({"AAPL": [_bar(d) for d in range(1, 6)], "MSFT": [_bar(d) for d in range(1, 4)]})
    out = fetcher._download_ohlcv_batch_via_alpaca(["AAPL", "MSFT"], period="1y", interval="1d")
    assert len(out["AAPL"]) == 5 and len(out["MSFT"]) == 3      # AAPL spanned two pages
    assert len(fake.calls) == 3                                  # 8 bars / 3 per page
    assert list(out["AAPL"].columns) == ["Open", "High", "Low", "Close", "Volume"]


def test_the_frame_matches_the_per_symbol_path():
    bars = [_bar(d) for d in range(1, 4)]
    single = fetcher._alpaca_bars_to_frame(bars)
    assert single.index.tz is None and single["Close"].tolist() == [100.0] * 3


def test_share_classes_are_sent_with_a_dot_and_returned_with_a_hyphen(alpaca):
    fake = alpaca({"BRK.B": [_bar(1)]})
    out = fetcher._download_ohlcv_batch_via_alpaca(["BRK-B"], period="1y", interval="1d")
    assert "BRK.B" in fake.calls[0]["symbols"]
    assert list(out) == ["BRK-B"]


def test_the_single_symbol_path_maps_share_classes_too(monkeypatch):
    seen = []

    class _S:
        def get(self, url, **kw):
            seen.append(url)
            return _Resp({"bars": [_bar(1)]})

    monkeypatch.setattr(cfg, "ALPACA_API_KEY", "k")
    monkeypatch.setattr(cfg, "ALPACA_SECRET_KEY", "s")
    monkeypatch.setattr(fetcher, "get_retry_session", lambda: _S())
    fetcher._download_ohlcv_via_alpaca("BRK-B", period="1y", interval="1d")
    assert seen[0].endswith("/v2/stocks/BRK.B/bars")


def test_symbols_are_chunked(alpaca, monkeypatch):
    monkeypatch.setattr(fetcher, "_ALPACA_BATCH_SYMBOLS", 2)
    fake = alpaca({s: [_bar(1)] for s in "ABCDE"}, page_size=100)
    out = fetcher._download_ohlcv_batch_via_alpaca(list("ABCDE"), period="1y", interval="1d")
    assert sorted(out) == list("ABCDE")
    assert [c["symbols"] for c in fake.calls] == ["A,B", "C,D", "E"]


def test_a_chunk_failing_part_way_is_discarded_whole(alpaca, monkeypatch):
    """Pages are ordered by symbol: a symbol cut off mid-history must not pass
    as complete, so the whole chunk falls back."""
    monkeypatch.setattr(fetcher, "_ALPACA_BATCH_SYMBOLS", 2)
    alpaca({"A": [_bar(d) for d in range(1, 5)], "B": [_bar(1)], "C": [_bar(1)]},
           page_size=3, fail_chunk_with="A")
    out = fetcher._download_ohlcv_batch_via_alpaca(["A", "B", "C"], period="1y", interval="1d")
    assert "A" not in out and "B" not in out
    assert "C" in out


@pytest.mark.parametrize("why", ["no_keys", "index", "bad_period"])
def test_nothing_is_requested_when_alpaca_cannot_serve(alpaca, monkeypatch, why):
    fake = alpaca({"AAPL": [_bar(1)]})
    if why == "no_keys":
        monkeypatch.setattr(cfg, "ALPACA_API_KEY", "")
        args = (["AAPL"], "1y")
    elif why == "index":
        args = (["^VIX"], "1y")
    else:
        args = (["AAPL"], "banana")
    assert fetcher._download_ohlcv_batch_via_alpaca(args[0], period=args[1], interval="1d") == {}
    assert fake.calls == []


def test_fetch_ohlcv_batch_only_falls_back_for_what_the_batch_missed(alpaca, monkeypatch):
    alpaca({"AAPL": [_bar(1)], "MSFT": [_bar(1)]})
    per_symbol = []
    monkeypatch.setattr(fetcher, "fetch_ohlcv",
                        lambda t, **kw: per_symbol.append(t) or pd.DataFrame())
    out = fetcher.fetch_ohlcv_batch(["AAPL", "MSFT", "ZZZZ"], period="1y", use_cache=False)
    assert per_symbol == ["ZZZZ"]
    assert not out["AAPL"].empty and not out["MSFT"].empty


def test_batch_results_are_cached_under_the_per_symbol_key(alpaca, monkeypatch):
    alpaca({"AAPL": [_bar(1)]})
    written = {}
    monkeypatch.setattr(fetcher.cache, "get", lambda key: None)
    monkeypatch.setattr(fetcher.cache, "set", lambda key, value, ttl=None: written.update({key: value}))
    monkeypatch.setattr(fetcher, "fetch_ohlcv", lambda t, **kw: pd.DataFrame())
    fetcher.fetch_ohlcv_batch(["AAPL"], period="1y", interval="1d")
    assert "ohlcv:AAPL:1y:1d" in written     # what fetch_ohlcv reads next


def test_the_scan_universe_takes_a_handful_of_requests_not_503(alpaca):
    syms = [f"S{i:03d}" for i in range(503)]
    fake = alpaca({s: [_bar(d) for d in range(1, 21)] for s in syms}, page_size=10_000)
    out = fetcher._download_ohlcv_batch_via_alpaca(syms, period="1y", interval="1d")
    assert len(out) == 503
    assert len(fake.calls) == 6               # ceil(503 / 100) chunks, one page each
