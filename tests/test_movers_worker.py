"""Tests for the Phase 5 always-on movers worker (movers_worker.py).

Hermetic: all collaborators (discover / alerter / monitor / scan_window / sleep)
are injected, so the loop is exercised without network or real sleeps.
"""

from unittest.mock import patch

import src.utils.config as cfg
from src.scanner.movers import MoverCandidate
from src.scanner.movers_monitor import WatchState
from src.scanner import movers_worker as mw


def _cfg(**over):
    base = dict(MOVERS_WORKER_POLL_INTERVAL_SEC=60.0, MOVERS_WORKER_REDISCOVER_EVERY_CYCLES=5,
                MOVERS_WORKER_MARKET_HOURS_ONLY=True, MOVERS_ALERTS_MIN_SCORE=70.0)
    base.update(over)
    return [patch.object(cfg, k, v) for k, v in base.items()]


class _Alerter:
    def __init__(self, fired=1):
        self.calls = 0
        self._fired = fired
    def process(self, candidates, *, now=None, send=True):
        self.calls += 1
        return [{"ticker": c.ticker} for c in candidates][: self._fired]


class _Monitor:
    def __init__(self, invalidate=0):
        self.watch_calls = 0
        self.eval_calls = 0
        self._inval = invalidate
        self._active = []
    def watch(self, candidates):
        self.watch_calls += 1
        # mirror the real monitor: active() yields WatchState (with .candidate)
        self._active = [WatchState(candidate=c, entry_score=c.score) for c in candidates]
        return len(candidates)
    def evaluate(self, *, now=None, enrich=None, alert=None):
        self.eval_calls += 1
        return [{"ticker": "X"}] * self._inval
    def active(self):
        return self._active


class _State:
    """Captures worker snapshot publishes so the loop can be asserted without I/O."""
    def __init__(self):
        self.publishes = []
        self.alerts = []
        self.invalidations = []
    def record_alerts(self, fired, now=None):
        self.alerts.append(list(fired))
    def record_invalidations(self, transitions, now=None):
        self.invalidations.append(list(transitions))
    def publish(self, **kw):
        self.publishes.append(kw)


class _Stream:
    """Fake PriceStream: records lifecycle + subscription calls, no network."""
    def __init__(self, start_ok=True):
        self.started = False
        self.stopped = False
        self.symbol_calls = []
        self._ok = start_ok
    def start(self, symbols=None):
        self.started = True
        return self._ok
    def set_symbols(self, syms):
        self.symbol_calls.append(list(syms))
    def status(self):
        return {"live": True, "subscribed_count": len(self.symbol_calls[-1]) if self.symbol_calls else 0}
    def snapshot(self):
        return {"NBIS": {"price": 12.3, "age_seconds": 0.4, "fresh": True}}
    def stop(self):
        self.stopped = True


def _cand(ticker="NBIS", score=89.0):
    return MoverCandidate(ticker=ticker, price=10.0, change_pct=20.0,
                          direction="long", sources=["gainers"], score=score, enriched=True)


def _run(**kw):
    sleeps = []
    defaults = dict(max_cycles=6, sleep_fn=lambda s: sleeps.append(s),
                    discover=lambda: [_cand()], alerter=_Alerter(), monitor=_Monitor(),
                    scan_window=lambda: True, send=lambda *a, **k: True, state=_State())
    defaults.update(kw)
    stats = mw.run_worker(**defaults)
    return stats, sleeps, defaults


def test_rediscovers_on_cadence_monitors_every_cycle():
    with_ctx = _cfg(MOVERS_WORKER_REDISCOVER_EVERY_CYCLES=3)
    for p in with_ctx:
        p.start()
    try:
        stats, sleeps, d = _run(max_cycles=6, monitor=_Monitor(), alerter=_Alerter())
    finally:
        for p in with_ctx:
            p.stop()
    # 6 cycles, rediscover every 3 → cycles 1 and 4 discover (2 discoveries).
    assert stats["cycles"] == 6
    assert stats["discoveries"] == 2
    assert d["monitor"].eval_calls == 6          # monitored every cycle
    assert d["monitor"].watch_calls == 2         # watched on each discovery
    assert len(sleeps) == 5                       # sleeps between, not after the last


def test_market_hours_gate_skips_when_closed():
    ps = _cfg()
    for p in ps:
        p.start()
    try:
        stats, _, d = _run(scan_window=lambda: False)
    finally:
        for p in ps:
            p.stop()
    assert stats["discoveries"] == 0 and stats["alerts"] == 0
    assert d["monitor"].eval_calls == 0


def test_market_hours_only_false_scans_regardless():
    ps = _cfg(MOVERS_WORKER_MARKET_HOURS_ONLY=False)
    for p in ps:
        p.start()
    try:
        stats, _, _ = _run(max_cycles=1, scan_window=lambda: False)
    finally:
        for p in ps:
            p.stop()
    assert stats["discoveries"] == 1   # ran even though "closed"


def test_alerts_and_invalidations_counted():
    ps = _cfg(MOVERS_WORKER_REDISCOVER_EVERY_CYCLES=1)
    for p in ps:
        p.start()
    try:
        stats, _, _ = _run(max_cycles=3, discover=lambda: [_cand("A"), _cand("B")],
                           alerter=_Alerter(fired=2), monitor=_Monitor(invalidate=1))
    finally:
        for p in ps:
            p.stop()
    assert stats["alerts"] == 6          # 2 per cycle × 3 cycles (rediscover every 1)
    assert stats["invalidations"] == 3   # 1 per cycle × 3


def test_only_high_score_candidates_watched():
    ps = _cfg(MOVERS_WORKER_REDISCOVER_EVERY_CYCLES=1)
    for p in ps:
        p.start()
    try:
        mon = _Monitor()
        # one above threshold (89), one below (40) → only 1 watched
        _run(max_cycles=1, discover=lambda: [_cand("HI", 89.0), _cand("LO", 40.0)], monitor=mon)
        # capture what watch received
        watched_counts = []
        mon2 = _Monitor()
        orig = mon2.watch
        def spy(cands):
            watched_counts.append(len(cands)); return orig(cands)
        mon2.watch = spy
        _run(max_cycles=1, discover=lambda: [_cand("HI", 89.0), _cand("LO", 40.0)], monitor=mon2)
    finally:
        for p in ps:
            p.stop()
    assert watched_counts == [1]         # only the >=70 candidate


def test_publishes_snapshot_every_cycle_including_idle():
    # Phase 7: state snapshot every cycle so the heartbeat stays fresh even
    # when the market is closed and no discovery runs.
    ps = _cfg(MOVERS_WORKER_MARKET_HOURS_ONLY=True)
    for p in ps:
        p.start()
    try:
        st = _State()
        _run(max_cycles=4, scan_window=lambda: False, state=st)
    finally:
        for p in ps:
            p.stop()
    assert len(st.publishes) == 4                         # one publish per cycle
    assert [p["cycle"] for p in st.publishes] == [1, 2, 3, 4]
    assert all(p["scan_window_open"] is False for p in st.publishes)


def test_publish_records_alerts_and_invalidations():
    ps = _cfg(MOVERS_WORKER_REDISCOVER_EVERY_CYCLES=1)
    for p in ps:
        p.start()
    try:
        st = _State()
        _run(max_cycles=2, discover=lambda: [_cand("A"), _cand("B")],
             alerter=_Alerter(fired=2), monitor=_Monitor(invalidate=1), state=st)
    finally:
        for p in ps:
            p.stop()
    assert sum(len(a) for a in st.alerts) == 4            # 2 fired × 2 cycles
    assert sum(len(i) for i in st.invalidations) == 2     # 1 × 2 cycles
    # the live watch set is handed to publish each cycle
    assert st.publishes[-1]["watching"] is not None


def test_worker_drives_injected_stream():
    # Phase 8: worker starts the stream, keeps it subscribed to the live watch
    # set each in-window cycle, feeds prices+status into publish, and stops it.
    ps = _cfg(MOVERS_WORKER_REDISCOVER_EVERY_CYCLES=1)
    for p in ps:
        p.start()
    try:
        st, strm = _State(), _Stream()
        _run(max_cycles=2, discover=lambda: [_cand("NBIS")],
             monitor=_Monitor(), state=st, stream=strm)
    finally:
        for p in ps:
            p.stop()
    assert strm.started is True and strm.stopped is True
    assert strm.symbol_calls and strm.symbol_calls[0] == ["NBIS"]   # active watch set
    last = st.publishes[-1]
    assert last["stream_status"] is not None
    assert last["stream_prices"]["NBIS"]["price"] == 12.3


def test_worker_falls_back_when_stream_wont_start():
    # start() returning False → worker drops the stream and keeps polling.
    ps = _cfg(MOVERS_WORKER_REDISCOVER_EVERY_CYCLES=1)
    for p in ps:
        p.start()
    try:
        st, strm = _State(), _Stream(start_ok=False)
        stats, _, _ = _run(max_cycles=2, discover=lambda: [_cand("NBIS")],
                           monitor=_Monitor(), state=st, stream=strm)
    finally:
        for p in ps:
            p.stop()
    assert strm.started is True
    assert strm.symbol_calls == []                    # never subscribed (dropped)
    assert stats["cycles"] == 2                        # loop still ran
    assert st.publishes[-1]["stream_status"] is None   # no streaming block


def test_bad_cycle_does_not_crash_the_loop():
    ps = _cfg(MOVERS_WORKER_REDISCOVER_EVERY_CYCLES=1)
    for p in ps:
        p.start()
    try:
        def boom():
            raise RuntimeError("discover failed")
        stats, _, _ = _run(max_cycles=3, discover=boom)
    finally:
        for p in ps:
            p.stop()
    assert stats["cycles"] == 3          # kept looping despite the error
    assert stats["discoveries"] == 0


# ── catalyst alerts run on the wire cadence, not the discovery cadence ───────

class _CatalystAlerter:
    def __init__(self, fired=1):
        self.calls = 0
        self._fired = fired
    def poll(self, *, now=None, send=True, fetch=None):
        self.calls += 1
        return [{"ticker": "NEWS", "direction": "long", "sent": True}] * self._fired


def test_catalyst_alerter_polls_every_cycle_not_the_rediscovery_cadence():
    # The whole point of the wire path is that it is cheap enough to run every
    # cycle — polling it on the 5-cycle discovery cadence would throw the
    # latency advantage away.
    ctx = _cfg(MOVERS_WORKER_REDISCOVER_EVERY_CYCLES=3)
    for p in ctx:
        p.start()
    try:
        news = _CatalystAlerter()
        stats, _, _ = _run(max_cycles=6, catalyst_alerter=news)
    finally:
        for p in ctx:
            p.stop()
    assert news.calls == 6                     # every cycle...
    assert stats["discoveries"] == 2           # ...while discovery stayed on cadence
    assert stats["catalyst_alerts"] == 6


def test_catalyst_alerter_is_optional():
    ctx = _cfg()
    for p in ctx:
        p.start()
    try:
        stats, _, _ = _run(max_cycles=2, catalyst_alerter=None)
    finally:
        for p in ctx:
            p.stop()
    assert stats["catalyst_alerts"] == 0


def test_catalyst_alerts_are_skipped_outside_the_scan_window():
    ctx = _cfg(MOVERS_WORKER_MARKET_HOURS_ONLY=True)
    for p in ctx:
        p.start()
    try:
        news = _CatalystAlerter()
        _run(max_cycles=3, scan_window=lambda: False, catalyst_alerter=news)
    finally:
        for p in ctx:
            p.stop()
    assert news.calls == 0


def test_published_features_reflect_whether_catalyst_alerts_run():
    # The dashboard reads this flag to tell "enabled and the wire was quiet"
    # from "never switched on" — both publish catalyst_alerts: 0.
    ctx = _cfg()
    for p in ctx:
        p.start()
    try:
        _, _, on = _run(max_cycles=1, catalyst_alerter=_CatalystAlerter())
        _, _, off = _run(max_cycles=1, catalyst_alerter=None)
    finally:
        for p in ctx:
            p.stop()
    assert on["state"].publishes[-1]["features"] == {"catalyst_alerts": True}
    assert off["state"].publishes[-1]["features"] == {"catalyst_alerts": False}
