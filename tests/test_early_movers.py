import pandas as pd
from src.scanner import early_movers
from src.utils import config


def _frame(n=30, spike=True):
    close = [10.0] * (n - 1) + [10.04]
    vol = [1000.0] * (n - 3) + ([5000.0] * 3 if spike else [1000.0] * 3)
    return pd.DataFrame({"open": close, "high": [10.05] * n, "low": [9.95] * n,
                         "close": close, "volume": vol})


def test_fires_on_accel_pressing(monkeypatch):
    monkeypatch.setattr(config, "EARLY_ALERTS_ENABLED", True)
    r = early_movers.evaluate_early(_frame(), change_pct=2.0)
    assert r["fire"] and r["rvol_accel"] >= 2.5 and "session_high" in r["pressing"]


def test_excludes_extended_and_quiet(monkeypatch):
    monkeypatch.setattr(config, "EARLY_ALERTS_ENABLED", True)
    assert early_movers.evaluate_early(_frame(), change_pct=25.0) is None
    assert not early_movers.evaluate_early(_frame(spike=False), change_pct=2.0)["fire"]


def test_off_by_default():
    assert early_movers.evaluate_early(_frame(), change_pct=2.0) is None


# ── watchlist path, stream bars, worker wiring ───────────────────────────────

import asyncio
from datetime import datetime, timezone

import pytest

from src.scanner.early_movers import (BarBuffer, WatchlistScanner, load_watchlist,
                                      prior_day_levels, split_session)
from src.streaming.price_stream import PriceStream
from src.scanner import movers_worker as mw
from tests.test_price_stream import FakeStream
from tests.test_movers_worker import _Alerter, _Monitor, _State

NOW = datetime(2026, 10, 15, 15, 0, tzinfo=timezone.utc)   # 11:00 ET


def _intraday(n=40, spike=True, start="2026-10-15 13:00"):
    """1m bars, naive UTC: 09:00-09:30 ET premarket then RTH (quiet then spike)."""
    idx = pd.date_range(start, periods=n, freq="1min")
    close = [10.0] * (n - 1) + [10.04]
    vol = [1000.0] * (n - 3) + ([5000.0] * 3 if spike else [1000.0] * 3)
    high = [10.05] * n
    return pd.DataFrame({"open": close, "high": high, "low": [9.95] * n,
                         "close": close, "volume": vol}, index=idx)


def _daily():
    idx = pd.to_datetime(["2026-10-13 04:00", "2026-10-14 04:00", "2026-10-15 04:00"])
    return pd.DataFrame({"open": [9.7] * 3, "high": [9.9, 10.06, 11.0],
                         "low": [9.5] * 3, "close": [9.7, 9.8, 10.9],
                         "volume": [1e6] * 3}, index=idx)


@pytest.fixture
def early_on(monkeypatch):
    monkeypatch.setattr(config, "EARLY_ALERTS_ENABLED", True)
    monkeypatch.setattr(config, "EARLY_WATCHLIST", ["abc", "XYZ", "abc"])
    monkeypatch.setattr(config, "EARLY_WATCHLIST_FILE", "")
    monkeypatch.setattr(config, "EARLY_WATCHLIST_MAX", 100)


def test_prior_day_skips_todays_partial_bar():
    assert prior_day_levels(_daily(), NOW) == (9.8, 10.06)


def test_split_session_extracts_premarket_high():
    df = _intraday(start="2026-10-15 13:20")          # 09:20 ET onward
    df.iloc[2, df.columns.get_loc("high")] = 10.5     # premarket print
    session, pm = split_session(df, NOW)
    assert pm == 10.5 and session.index.min() >= pd.Timestamp("2026-10-15 13:30")


def test_watchlist_load_dedup_file_and_cap(early_on, monkeypatch, tmp_path):
    f = tmp_path / "wl.txt"
    f.write_text("DEF  # comment\nghi,XYZ\n")
    monkeypatch.setattr(config, "EARLY_WATCHLIST_FILE", str(f))
    assert load_watchlist() == ["ABC", "XYZ", "DEF", "GHI"]
    monkeypatch.setattr(config, "EARLY_WATCHLIST_MAX", 2)
    assert load_watchlist() == ["ABC", "XYZ"]


def test_watchlist_scan_batches_and_fires(early_on):
    calls = []
    def bars(syms):
        calls.append(list(syms))
        return {"ABC": _intraday(start="2026-10-15 13:30"),
                "XYZ": _intraday(spike=False, start="2026-10-15 13:30")}
    sc = WatchlistScanner(fetch_bars=bars, fetch_daily=lambda s: {k: _daily() for k in s})
    hits, sessions = sc.scan(now=NOW)
    assert calls == [["ABC", "XYZ"]]                 # one batched request
    assert [c.ticker for c in hits] == ["ABC"]
    c = hits[0]
    assert c.early["fire"] and "prior_day_high" in c.early["levels"]
    assert c.enriched and c.bars_as_of is not None and c.sources == ["early_watchlist"]
    assert 2.0 < c.change_pct < 3.0 and set(sessions) == {"ABC", "XYZ"}


def test_watchlist_scan_excludes_extended(early_on):
    big = _intraday(start="2026-10-15 13:30") * 1.0
    big[["open", "high", "low", "close"]] *= 1.5     # +53% vs prior close
    sc = WatchlistScanner(["ABC"], fetch_bars=lambda s: {"ABC": big},
                          fetch_daily=lambda s: {"ABC": _daily()})
    assert sc.scan(now=NOW)[0] == []


def test_early_hit_passes_alerter_below_min_score(early_on, monkeypatch):
    from src.scanner.movers_alerts import MoversAlerter
    monkeypatch.setattr(config, "MOVERS_ALERTS_ENABLED", True)
    monkeypatch.setattr(config, "MOVERS_ALERTS_MIN_SCORE", 99.0)
    sc = WatchlistScanner(["ABC"], fetch_bars=lambda s: {"ABC": _intraday(start="2026-10-15 13:30")},
                          fetch_daily=lambda s: {"ABC": _daily()})
    hits, _ = sc.scan(now=NOW)
    a = MoversAlerter()
    fired = a.process(hits, now=NOW, send=False)
    assert len(fired) == 1 and "EARLY" in fired[0]["message"]
    assert a.process(hits, now=NOW, send=False) == []          # dedup still applies
    hits[0].is_halted = True
    assert MoversAlerter().process(hits, now=NOW, send=False) == []   # halt applies


class FakeBar:
    def __init__(self, symbol, ts, close, volume):
        self.symbol, self.timestamp = symbol, ts
        self.open = self.low = close - 0.05
        self.high, self.close, self.volume = 10.05, close, volume


class FakeBarStream(FakeStream):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.bar_handler = None
        self.bar_subscribed = set()
    def subscribe_bars(self, handler, *syms):
        self.bar_handler = handler
        self.bar_subscribed.update(syms)
    def unsubscribe_bars(self, *syms):
        for s in syms:
            self.bar_subscribed.discard(s)


@pytest.fixture
def stream_on(monkeypatch):
    monkeypatch.setattr(config, "MOVERS_STREAM_ENABLED", True)
    monkeypatch.setattr(config, "MOVERS_STREAM_MAX_SYMBOLS", 3)


def test_stream_bars_share_socket_and_cap(stream_on):
    fake = FakeBarStream()
    ps = PriceStream(api_key="k", secret_key="s", client_factory=lambda *a, **k: fake)
    assert ps.start(["AAA"])
    ps.set_bar_symbols(["AAA", "BBB", "CCC", "DDD"])
    assert fake.bar_subscribed == {"AAA", "BBB", "CCC"}       # total cap 3, one client
    ps.set_bar_symbols(["AAA"])
    assert fake.bar_subscribed == {"AAA"}
    asyncio.run(fake.bar_handler(FakeBar("AAA", NOW, 10.0, 100)))
    assert ps.drain_bars()[0]["symbol"] == "AAA" and ps.drain_bars() == []
    ps.stop()


def test_bar_buffer_fires_on_bar_close(early_on):
    buf = BarBuffer()
    seed = _intraday(spike=False, start="2026-10-15 13:30").iloc[:-1]
    buf.seed("ABC", seed)
    ctx = lambda s: {"prior_close": 9.8, "prior_high": 10.06}
    base = seed.index[-1]
    bars = [{"symbol": "ABC", "timestamp": base + pd.Timedelta(minutes=i + 1),
             "open": 10.0, "high": 10.05, "low": 9.95,
             "close": 10.04 if i == 2 else 10.0, "volume": 5000.0} for i in range(3)]
    hits = buf.on_bars(bars, ctx, now=NOW)
    assert [c.sources for c in hits][-1] == ["early_stream"]
    assert buf.on_bars(bars[-1:], ctx, now=NOW) == []          # duplicate bar ignored


def test_worker_runs_watchlist_every_cycle_and_drains_stream(early_on, monkeypatch):
    monkeypatch.setattr(config, "MOVERS_WORKER_REDISCOVER_EVERY_CYCLES", 8)
    monkeypatch.setattr(config, "EARLY_WATCHLIST_EVERY_CYCLES", 1)
    monkeypatch.setattr(config, "EARLY_STREAM_BARS", True)

    class Scanner:
        symbols = ["ABC"]
        scans = 0
        def scan(self, now=None):
            Scanner.scans += 1
            return [], {"ABC": _intraday(spike=False, start="2026-10-15 13:30")}
        def context(self, s):
            return {"prior_close": 9.8}

    class Stream:
        def __init__(self):
            self.bar_syms = []
            self.drains = 0
        def start(self, symbols=None): return True
        def set_symbols(self, s): pass
        def set_bar_symbols(self, s): self.bar_syms.append(list(s))
        def drain_bars(self):
            self.drains += 1
            return []
        def status(self): return {}
        def snapshot(self): return {}
        def stop(self): pass

    st = Stream()
    discovers = []
    stats = mw.run_worker(max_cycles=3, sleep_fn=lambda s: None,
                          discover=lambda: discovers.append(1) or [],
                          alerter=_Alerter(), monitor=_Monitor(), catalyst_alerter=None,
                          scan_window=lambda: True, state=_State(), stream=st,
                          outcome_ledger=None, early_scanner=Scanner())
    assert Scanner.scans == 3 and len(discovers) == 1          # independent of rediscovery
    assert st.drains == 3 and st.bar_syms[-1] == ["ABC"]
    assert stats["early_alerts"] == 0


def test_worker_early_off_by_default():
    assert not config.EARLY_ALERTS_ENABLED and config.EARLY_WATCHLIST == []
