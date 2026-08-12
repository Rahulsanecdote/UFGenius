"""Tests for the intraday backtest harness (src/backtest/intraday_engine.py).

Engine MECHANICS (next-open fill, intrabar stop/target, session-flat, one-position,
R math) are tested deterministically with an injected strategy so the outcome is
fully controlled. Two INTEGRATION tests then drive the real breakout / sweep
evaluators end-to-end to prove they wire up.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.backtest import intraday_engine
from src.backtest.intraday_engine import (
    IntradayTrade,
    _compute_intraday_metrics,
    backtest_intraday,
    simulate_intraday_ticker,
)


# ── frame builders ────────────────────────────────────────────────────────────
def _bars(closes, opens=None, highs=None, lows=None, vols=None, date="2023-06-01",
          start="13:30"):
    n = len(closes)
    idx = pd.date_range(f"{date} {start}", periods=n, freq="5min")  # naive UTC
    opens = opens if opens is not None else list(closes)
    highs = highs if highs is not None else [max(o, c) + 0.2 for o, c in zip(opens, closes)]
    lows = lows if lows is not None else [min(o, c) - 0.2 for o, c in zip(opens, closes)]
    vols = vols if vols is not None else [1000] * n
    return pd.DataFrame(
        {"Open": opens, "High": highs, "Low": lows, "Close": closes, "Volume": vols},
        index=idx,
    )


def _threshold_strategy(window):
    """Fire (stop 2.0 below entry) the first time a bar closes >= 100."""
    if float(window["Close"].iloc[-1]) >= 100.0:
        return {"stop_abs": None, "stop_distance": 2.0, "signal": "TEST"}
    return None


@pytest.fixture
def inject_test_strategy(monkeypatch):
    monkeypatch.setitem(intraday_engine._STRATEGIES, "test", _threshold_strategy)


# ── mechanics: next-open fill + session flat ──────────────────────────────────
def test_next_open_fill_and_session_flat(inject_test_strategy):
    closes = [98, 98, 98, 98, 98, 101, 102, 102, 102, 102, 102, 102.5]
    opens = [98, 98, 98, 98, 98, 98, 101.5, 102, 102, 102, 102, 102]
    highs = [98.2] * 6 + [103] * 6
    lows = [97.8] * 6 + [100.5] * 6           # never reach stop 99.5
    df = _bars(closes, opens, highs, lows)
    trades = simulate_intraday_ticker(df, "test", ticker="AAA")

    assert len(trades) == 1                    # one position; overlapping signals don't re-enter
    t = trades[0]
    assert t.entry_ts == df.index[6]           # signal fired at bar 5 → fill at bar 6's OPEN
    assert t.entry_ref == pytest.approx(101.5)
    assert t.entry_fill > 101.5                # slippage makes the buy worse
    assert t.stop == pytest.approx(99.5)       # 101.5 - 2.0
    assert t.exit_reason == "SESSION_CLOSE"
    assert t.exit_ts == df.index[-1]           # flat at the session's last bar


def test_stop_exit_is_about_minus_one_r(inject_test_strategy):
    closes = [98, 98, 98, 98, 98, 101, 102, 102, 102, 102, 102, 102.5]
    opens = [98, 98, 98, 98, 98, 98, 101.5, 102, 102, 102, 102, 102]
    highs = [98.2] * 6 + [103] * 6
    lows = [97.8] * 6 + [100.5, 99.0, 100.5, 100.5, 100.5, 100.5]   # bar 7 pierces stop
    df = _bars(closes, opens, highs, lows)
    t = simulate_intraday_ticker(df, "test", ticker="AAA")[0]
    assert t.exit_reason == "STOP"
    assert t.exit_ts == df.index[7]
    # Stop at 99.5, entry ref 101.5 → nominal -1R, but with 0.1% commission +
    # 0.1% slippage per side on a ~$100 notional / $2 risk, the round trip costs
    # ~0.2R — so a real stop fills at ~ -1.2R. The harness surfacing this cost
    # drag on tight intraday stops is the whole point.
    assert -1.5 < t.r_multiple < -1.0


def test_target_exit_positive_r(inject_test_strategy):
    closes = [98, 98, 98, 98, 98, 101, 102, 102, 102, 102, 102, 102.5]
    opens = [98, 98, 98, 98, 98, 98, 101.5, 102, 102, 102, 102, 102]
    highs = [98.2] * 6 + [103, 110, 103, 103, 103, 103]   # bar 7 spikes through T3 (109.5)
    lows = [97.8] * 6 + [100.5, 101.0, 100.5, 100.5, 100.5, 100.5]
    df = _bars(closes, opens, highs, lows)
    t = simulate_intraday_ticker(df, "test", ticker="AAA")[0]
    assert t.exit_reason == "T3"
    # Blended R across T1/T2/T3 = 0.3*1.5 + 0.4*2.5 + 0.3*4.0 = 2.65 (minus costs).
    assert t.r_multiple > 2.0


def test_degenerate_stop_at_or_above_entry_is_skipped(monkeypatch):
    monkeypatch.setitem(
        intraday_engine._STRATEGIES, "badstop",
        lambda w: {"stop_abs": 999.0, "stop_distance": None, "signal": "X"}
        if float(w["Close"].iloc[-1]) >= 100 else None,
    )
    df = _bars([98, 98, 98, 101, 102, 102])
    assert simulate_intraday_ticker(df, "badstop", ticker="AAA") == []


# ── overnight discipline / session boundaries ─────────────────────────────────
def test_signal_on_session_last_bar_is_dropped(inject_test_strategy):
    # Session A's LAST bar signals; the fill would be session B's open → dropped.
    a = _bars([98, 98, 98, 98, 98, 101], date="2023-06-01")
    b = _bars([98, 98, 98, 98, 98, 98], date="2023-06-02")
    df = pd.concat([a, b])
    assert simulate_intraday_ticker(df, "test", ticker="AAA") == []


def test_trade_never_holds_overnight(inject_test_strategy):
    # Signal mid-session A → must be flat by session A's close, never into B.
    a = _bars([98, 98, 98, 101, 102, 102], date="2023-06-01")
    b = _bars([98, 98, 98, 98, 98, 98], date="2023-06-02")
    df = pd.concat([a, b])
    trades = simulate_intraday_ticker(df, "test", ticker="AAA")
    assert len(trades) == 1
    t = trades[0]
    assert t.exit_reason == "SESSION_CLOSE"
    assert t.entry_ts.date() == t.exit_ts.date() == pd.Timestamp("2023-06-01").date()


# ── backtest_intraday orchestration + metrics ─────────────────────────────────
def test_backtest_intraday_aggregates_and_flags_small_sample(inject_test_strategy):
    frame = _bars(
        [98, 98, 98, 98, 98, 101, 102, 102, 102, 102, 102, 102.5],
        opens=[98, 98, 98, 98, 98, 98, 101.5, 102, 102, 102, 102, 102],
        highs=[98.2] * 6 + [103] * 6, lows=[97.8] * 6 + [100.5] * 6,
    )
    frames = {"AAA": frame, "BBB": frame}
    res = backtest_intraday(
        ["AAA", "BBB"], entry="test",
        fetch=lambda s, interval=None: frames[s].copy(),
    )
    assert res["total_trades"] == 2
    assert res["tickers_tested"] == 2
    assert "expectancy_r" in res and "profit_factor" in res
    # 2 trades < the 30 min-sample floor → the verdict must say so, not "pass".
    assert "INSUFFICIENT SAMPLE" in res["minimum_acceptance"]["verdict"]


def test_backtest_intraday_unknown_entry_errors():
    res = backtest_intraday(["AAA"], entry="does-not-exist", fetch=lambda s, interval=None: _bars([1, 2, 3]))
    assert "error" in res


def test_backtest_intraday_no_trades_note():
    res = backtest_intraday(["AAA"], entry="breakout", fetch=lambda s, interval=None: pd.DataFrame())
    assert res["total_trades"] == 0 and "note" in res


def test_metrics_math_direct():
    ts = pd.Timestamp("2023-06-01 14:00")
    def mk(r, i):
        return IntradayTrade(
            ticker="X", entry_ts=ts + pd.Timedelta(minutes=5 * i),
            exit_ts=ts + pd.Timedelta(minutes=5 * i + 5), entry_ref=100.0,
            entry_fill=100.0, stop=98.0, r_multiple=r, pct_return=r * 0.02,
            exit_reason="T3" if r > 0 else "STOP", hold_bars=2, signal="TEST",
        )
    trades = [mk(2.0, 0), mk(2.0, 1), mk(2.0, 2), mk(-1.0, 3), mk(-1.0, 4)]
    m = _compute_intraday_metrics(
        trades, initial_capital=10_000, entry="test", interval="5m", tickers_tested=1,
    )
    assert m["win_rate_pct"] == pytest.approx(60.0)
    assert m["expectancy_r"] == pytest.approx(0.8)      # (3*2 + 2*-1)/5
    assert m["profit_factor"] == pytest.approx(3.0)     # 6 / 2
    assert m["exit_breakdown"] == {"T3": 3, "STOP": 2}


# ── integration: the real evaluators wire up ──────────────────────────────────
def test_integration_real_breakout_produces_a_trade():
    # 6-bar opening range near 100 (OR high ~100.5), then a clean breakout on
    # rising price + volume, with enough bars for the intraday ATR (needs >=15).
    base = [100.0] * 6                                   # opening range
    mid = [100.2, 100.3, 100.2, 100.3, 100.2, 100.3, 100.2, 100.3]  # coil below OR high
    run = [101.0, 102.0, 103.0, 104.0, 105.0, 106.0]    # breakout leg
    closes = base + mid + run
    n = len(closes)
    highs = [c + 0.3 for c in closes]
    lows = [c - 0.3 for c in closes]
    vols = [1000] * (n - len(run)) + [4000] * len(run)  # participation on the breakout
    df = _bars(closes, highs=highs, lows=lows, vols=vols)
    trades = simulate_intraday_ticker(df, "breakout", ticker="AAA")
    assert len(trades) >= 1
    assert trades[0].signal in {"BUY", "STRONG_BUY"}


def test_integration_real_sweep_reclaim_produces_a_trade():
    # 20 base bars establish a swing low at 9.80; bar 20 sweeps to 9.60, bar 21
    # reclaims (close 9.95 > 9.80); bars 22+ give the fill + management room.
    base_closes = [10.0] * 20
    base_lows = [9.85] * 20
    base_lows[8] = 9.80                                  # the swing low
    closes = base_closes + [9.70, 9.95, 10.0, 10.0, 10.0, 10.0]
    lows = base_lows + [9.60, 9.90, 9.9, 9.9, 9.9, 9.9]
    highs = [c + 0.05 for c in closes]
    opens = list(closes)
    opens[22] = 9.96                                     # next-open fill reference
    vols = [500] * 20 + [500, 2000, 500, 500, 500, 500]  # volume on the reclaim
    df = _bars(closes, opens=opens, highs=highs, lows=lows, vols=vols)
    trades = simulate_intraday_ticker(df, "sweep_reclaim", ticker="AAA")
    assert len(trades) >= 1
    assert trades[0].entry_ts.date() == trades[0].exit_ts.date()   # intraday only
