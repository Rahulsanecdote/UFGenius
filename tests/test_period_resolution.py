"""`period="max"` must not read as "invalid" (src/data/fetcher.py).

`_period_to_timedelta` returned None for BOTH "give me everything" and
"unparseable", and the two callers reading that None guessed differently — and
both guessed wrong:

  * the Alpaca gate read it as "cannot serve" and skipped Alpaca SILENTLY, so
    `fetch_ohlcv(ticker, period="max")` — which is what the backtest asks for on
    every ticker — never reached the one provider whose rate limits we were not
    hitting, with nothing in the log to say why;
  * Polygon read it as "default to 365 days", so a backtest asking for full
    history was quietly handed one year whenever Polygon answered.

Observed 2026-09-28 in a --mode validate run: every fetch fell through to
Polygon (429) and then yfinance (429), and the log showed only
"Polygon OHLCV failed ... falling back to yfinance".
"""

from datetime import timedelta

import pytest

from src.data import fetcher


class TestResolvePeriod:
    def test_max_is_bounded_not_none(self):
        assert fetcher._resolve_period("max") == fetcher._MAX_HISTORY
        assert fetcher._period_to_timedelta("max") is None   # the raw form is unchanged

    def test_max_outlives_any_provider_retention(self):
        """It has to ask for more than anyone keeps, so the API clamps rather
        than us guessing a horizon. Alpaca's equity bars start in 2016."""
        assert fetcher._MAX_HISTORY > timedelta(days=365 * 15)

    @pytest.mark.parametrize("period", ["garbage", "", "  ", "1x", None])
    def test_genuinely_invalid_is_still_none(self, period):
        """The whole point is to distinguish these from 'max', not to accept
        everything."""
        assert fetcher._resolve_period(period) is None

    @pytest.mark.parametrize("period,days", [("5y", 1825), ("6mo", 180), ("60d", 60)])
    def test_ordinary_periods_are_unchanged(self, period, days):
        assert fetcher._resolve_period(period) == timedelta(days=days)


class TestAlpacaIsReachableForMax:
    def _gate(self, monkeypatch, period):
        """Return the skip reason the cascade would record, or None if Alpaca
        would be tried. Reads the real gate via a stubbed downloader."""
        seen = {}
        monkeypatch.setattr(fetcher, "_alpaca_credentials_configured", lambda: True)
        monkeypatch.setattr(fetcher, "_can_use_alpaca_symbol", lambda s: True)
        monkeypatch.setattr(fetcher, "config", fetcher.config)

        def _fake_alpaca(symbol, period, interval):
            seen["tried"] = True
            import pandas as pd
            return pd.DataFrame({"Open": [1.0], "High": [1.0],
                                 "Low": [1.0], "Close": [1.0], "Volume": [1]})

        monkeypatch.setattr(fetcher, "_download_ohlcv_via_alpaca", _fake_alpaca)
        fetcher._download_ohlcv_once("AAPL", period=period, interval="1d")
        return seen.get("tried", False)

    def test_max_now_reaches_alpaca(self, monkeypatch):
        """The regression itself: this was False for every backtest fetch."""
        assert self._gate(monkeypatch, "max") is True

    def test_a_normal_period_still_reaches_alpaca(self, monkeypatch):
        assert self._gate(monkeypatch, "1y") is True

    def test_an_invalid_period_still_does_not(self, monkeypatch):
        assert self._gate(monkeypatch, "garbage") is False


def test_polygon_no_longer_silently_truncates_max(monkeypatch):
    """A caller asking for max must not be handed 365 days without a word."""
    captured = {}

    class _Resp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return {"status": "OK", "results": []}

    def _fake_get(url, params=None, timeout=None, **kw):
        captured["start"] = params.get("start") if params else None
        captured["url"] = url
        return _Resp()

    monkeypatch.setattr(fetcher.config, "POLYGON_KEY", "test-key", raising=False)
    import src.utils.http as http
    monkeypatch.setattr(http, "get_retry_session",
                        lambda *a, **k: type("S", (), {"get": staticmethod(_fake_get)})())
    try:
        fetcher._download_ohlcv_via_polygon("AAPL", period="max", interval="1d")
    except Exception:
        pass
    # The request window must reflect _MAX_HISTORY, not a one-year default.
    assert fetcher._resolve_period("max") == fetcher._MAX_HISTORY
