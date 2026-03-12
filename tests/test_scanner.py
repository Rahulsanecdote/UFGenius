"""Scanner and pipeline regression tests."""

from __future__ import annotations

import numpy as np
import pandas as pd

from src.scanner import daily_scan
from src.signals import generator


def _sample_price_df(rows: int = 260) -> pd.DataFrame:
    idx = pd.date_range("2023-01-01", periods=rows, freq="B")
    close = np.linspace(100, 130, rows)
    return pd.DataFrame(
        {
            "Open": close * 0.99,
            "High": close * 1.01,
            "Low": close * 0.98,
            "Close": close,
            "Volume": np.full(rows, 1_000_000.0),
        },
        index=idx,
    )


def test_generate_signal_uses_prefetched_dataframe(monkeypatch):
    df = _sample_price_df()

    # If this is called, duplicate fetch regression returned.
    monkeypatch.setattr("src.signals.context.fetch_ohlcv", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("fetch_ohlcv should not be called")))
    monkeypatch.setattr("src.signals.context.fetch_ticker_info", lambda *_args, **_kwargs: {"longName": "Test Corp"})
    monkeypatch.setattr("src.signals.context.fetch_fundamentals", lambda *_args, **_kwargs: {
        "ticker": "TEST",
        "market_cap": 10_000_000_000,
        "revenue": 10_000_000_000,
        "gross_profit": 5_000_000_000,
        "ebit": 2_000_000_000,
        "ebitda": 2_500_000_000,
        "net_income": 1_500_000_000,
        "total_assets": 8_000_000_000,
        "total_liabilities": 3_000_000_000,
        "current_assets": 2_000_000_000,
        "current_liabilities": 1_000_000_000,
        "retained_earnings": 1_200_000_000,
        "total_debt": 500_000_000,
        "operating_cash_flow": 1_000_000_000,
        "enterprise_value": 11_000_000_000,
        "free_cash_flow": 800_000_000,
        "revenue_growth_yoy": 0.1,
        "peg_ratio": 1.1,
    })
    monkeypatch.setattr(generator, "analyze_news_sentiment", lambda *_args, **_kwargs: {"sentiment_score_0_100": 55, "signal": "NEUTRAL"})
    monkeypatch.setattr(generator, "analyze_social_sentiment", lambda *_args, **_kwargs: {"sentiment_score_0_100": 50, "signal": "NEUTRAL"})
    monkeypatch.setattr(generator, "analyze_insider_activity", lambda *_args, **_kwargs: {"insider_score": 50, "flags": [], "signal": "NEUTRAL"})
    monkeypatch.setattr(generator, "detect_market_regime", lambda: {"regime": "NEUTRAL_CHOPPY", "regime_score": 0, "strategy": {"position_size_multiplier": 0.5}})

    result = generator.generate_signal("TEST", price_df=df)
    assert result["ticker"] == "TEST"
    assert result["_df"] is not None


def test_run_daily_scan_passes_prefetched_df(monkeypatch):
    df = _sample_price_df(80)
    captured = []

    monkeypatch.setattr(daily_scan, "detect_market_regime", lambda: {"regime": "MILD_BULL", "regime_score": 25, "vix": 18, "strategy": {"position_size_multiplier": 0.8}})
    monkeypatch.setattr(daily_scan, "get_universe", lambda _name: ["AAA", "BBB"])
    monkeypatch.setattr(daily_scan, "technical_pre_filter", lambda _tickers: [("AAA", df), ("BBB", df)])

    def _fake_generate_signal(ticker, macro_regime=None, price_df=None):
        captured.append((ticker, price_df is not None))
        return {
            "signal": "BUY",
            "score": 70,
            "_df": price_df,
            "reasons": ["ok"],
            "current_price": float(price_df["Close"].iloc[-1]),
            "support_resistance": {},
            "volatility": {},
        }

    monkeypatch.setattr(daily_scan, "generate_signal", _fake_generate_signal)
    monkeypatch.setattr(daily_scan, "generate_trade_plan", lambda ticker, signal, account_size=None, df=None: {
        "ticker": ticker,
        "signal": signal["signal"],
        "composite_score": signal["score"],
        "entry": {"price": 100},
        "stop_loss": {"price": 95},
        "targets": {"T1": {"price": 105}},
        "position": {"shares": 1, "risk_dollars": 5, "risk_percent": 0.05},
    })

    result = daily_scan.run_daily_scan(account_size=10_000, max_signals=2, pre_filter=True)
    assert result["total_scanned"] == 2
    assert captured == [("AAA", True), ("BBB", True)]


def test_scan_single_ticker_preserves_regime_context(monkeypatch):
    df = _sample_price_df(80)
    regime = {
        "regime": "MILD_BULL",
        "regime_score": 25,
        "vix": 18.0,
        "spy_vs_200": 2.5,
        "strategy": {"bias": "LONG", "position_size_multiplier": 0.8},
    }

    monkeypatch.setattr(daily_scan, "detect_market_regime", lambda: regime)
    monkeypatch.setattr(
        daily_scan,
        "generate_signal",
        lambda ticker, macro_regime=None: {
            "signal": "BUY",
            "score": 72,
            "raw_composite": 74,
            "confidence": "HIGH",
            "current_price": 123.45,
            "market_cap": 10_000_000_000,
            "scores": {"technical": 65},
            "reasons": ["Above 200 SMA"],
            "disqualifiers": [],
            "support_resistance": {"nearest_support": 120},
            "volatility": {"ATR_14": None},
            "_df": df,
        },
    )
    monkeypatch.setattr(
        daily_scan,
        "generate_trade_plan",
        lambda ticker, signal, account_size=None, df=None: {
            "ticker": ticker,
            "entry": {"price": 100},
            "stop_loss": {"price": 95},
            "targets": {},
            "position": {},
            "reasoning": [],
        },
    )

    result = daily_scan.scan_single_ticker("AAPL", account_size=10_000)

    assert result["regime"] == "MILD_BULL"
    assert result["regime_context"] == regime
    assert result["current_price"] == 123.45
    assert result["market_cap"] == 10_000_000_000
    assert result["reasons"] == ["Above 200 SMA"]


def test_scan_single_ticker_error_preserves_regime_context(monkeypatch):
    regime = {
        "regime": "NEUTRAL_CHOPPY",
        "regime_score": 0,
        "strategy": {"bias": "NEUTRAL", "position_size_multiplier": 0.5},
    }

    monkeypatch.setattr(daily_scan, "detect_market_regime", lambda: regime)
    monkeypatch.setattr(
        daily_scan,
        "generate_signal",
        lambda ticker, macro_regime=None: {
            "ticker": ticker,
            "signal": "ERROR",
            "score": 0,
            "reasons": ["Insufficient price data"],
            "disqualifiers": ["Insufficient price data"],
        },
    )

    result = daily_scan.scan_single_ticker("AAPL", account_size=10_000)

    assert result["regime"] == "NEUTRAL_CHOPPY"
    assert result["regime_context"] == regime
