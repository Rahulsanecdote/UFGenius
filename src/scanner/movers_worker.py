"""Always-on movers worker (Phase 5).

Runs the movers pipeline continuously — with NO browser and no manual runs — so
setups are surfaced, alerted, monitored, and invalidated on their own. Meant to
run as a persistent background service (a Render Background Worker, a daemon, or
`bot.py --mode movers-worker`).

Each cycle (market-hours aware):
  • periodically RE-DISCOVERS movers and alerts on newly-qualifying setups;
  • adds qualifiers to the monitor watch set;
  • re-evaluates the watch set and invalidates setups that break down.

Composes the existing pieces (discovery, MoversAlerter, MoversMonitor) — this
is the orchestration + always-on loop, not new signal logic. Best-effort: a bad
cycle is logged and skipped, never fatal.
"""

from __future__ import annotations

import time

from src.alerts.telegram_alert import send_text_alert
from src.catalysts.catalyst_alerts import CatalystAlerter
from src.scanner.intraday_scan import is_scan_window
from src.scanner.movers import fetch_market_movers
from src.scanner.movers_alerts import MoversAlerter
from src.scanner.movers_monitor import MoversMonitor
from src.scanner.movers_state import MoversWorkerState
from src.streaming.price_stream import PriceStream
from src.utils import config
from src.utils.logger import get_logger

log = get_logger(__name__)

_MIN_INTERVAL_SEC = 15.0

# Sentinel so callers can force "no streaming" distinctly from "use the default".
_USE_DEFAULT = object()


def run_worker(
    *,
    max_cycles: int | None = None,
    sleep_fn=time.sleep,
    discover=None,
    alerter: MoversAlerter | None = None,
    monitor: MoversMonitor | None = None,
    catalyst_alerter=_USE_DEFAULT,
    scan_window=None,
    send=None,
    state: MoversWorkerState | None = None,
    stream=_USE_DEFAULT,
) -> dict:
    """Run the continuous discover → alert → monitor loop.

    Loops forever by default; ``max_cycles`` bounds it (for tests). All
    collaborators are injectable so the loop is testable without network/sleep.
    Returns a stats dict (cycles / discoveries / alerts / invalidations).

    After every cycle the worker publishes a snapshot of its live state (watch
    set, recent alerts/invalidations, heartbeat) via ``state`` (Phase 7) so the
    dashboard can show what the worker is doing without running its own scans.

    Phase 8: when ``movers.stream`` is enabled the worker keeps a ``PriceStream``
    subscribed to the live watch set, so it has a real-time tape and the published
    snapshot carries live prices + stream status. Streaming is opt-in and
    fail-open — if it can't start, the worker just keeps polling. Pass
    ``stream=None`` to force it off (tests), or inject a fake PriceStream.
    """
    discover = discover or fetch_market_movers
    alerter = alerter or MoversAlerter()
    monitor = monitor or MoversMonitor()
    if catalyst_alerter is _USE_DEFAULT:
        catalyst_alerter = CatalystAlerter() if config.CATALYST_ALERTS_ENABLED else None
    scan_window = scan_window if scan_window is not None else is_scan_window
    send = send or send_text_alert
    state = state if state is not None else MoversWorkerState.load_default()
    if stream is _USE_DEFAULT:
        stream = PriceStream() if config.MOVERS_STREAM_ENABLED else None
    # Try to open the stream up front; on any failure fall back to REST polling.
    if stream is not None and not stream.start():
        stream = None

    interval = max(_MIN_INTERVAL_SEC, float(config.MOVERS_WORKER_POLL_INTERVAL_SEC))
    rediscover_every = max(1, int(config.MOVERS_WORKER_REDISCOVER_EVERY_CYCLES))
    hours_only = bool(config.MOVERS_WORKER_MARKET_HOURS_ONLY)
    min_score = float(config.MOVERS_ALERTS_MIN_SCORE)

    stats = {"cycles": 0, "discoveries": 0, "alerts": 0, "invalidations": 0,
             "catalyst_alerts": 0}
    cycle = 0
    last_movers: list | None = None
    try:
        while max_cycles is None or cycle < max_cycles:
            cycle += 1
            stats["cycles"] = cycle
            window_open = True
            try:
                window_open = (not hours_only) or bool(scan_window())
                if window_open:
                    # The wire runs EVERY cycle, not on the rediscovery cadence:
                    # it is one request for the whole watchlist, and its whole
                    # point is to beat the price-derived path to the news.
                    if catalyst_alerter is not None:
                        news_fired = catalyst_alerter.poll(send=True)
                        stats["catalyst_alerts"] += len(news_fired)
                        state.record_alerts(news_fired)
                    # Re-discover on a slower cadence (it is the expensive step);
                    # monitor every cycle so invalidations are timely.
                    if (cycle - 1) % rediscover_every == 0:
                        movers = discover()
                        last_movers = movers
                        stats["discoveries"] += 1
                        fired = alerter.process(movers, send=True)
                        stats["alerts"] += len(fired)
                        state.record_alerts(fired)
                        watched = [m for m in movers if m.score >= min_score]
                        added = monitor.watch(watched)
                        log.info(f"worker cycle {cycle}: {len(movers)} movers, "
                                 f"{len(fired)} alerts, +{added} watched")
                    transitions = monitor.evaluate(alert=send)
                    stats["invalidations"] += len(transitions)
                    state.record_invalidations(transitions)
                    # Keep the stream subscribed to exactly the live watch set.
                    if stream is not None:
                        stream.set_symbols([s.candidate.ticker for s in monitor.active()])
                else:
                    log.debug(f"worker cycle {cycle}: outside scan window — idle")
            except Exception as exc:  # a bad cycle must not kill the worker
                log.warning(f"worker cycle {cycle} failed ({type(exc).__name__}: {exc})")

            # Publish a fresh snapshot every cycle — including idle/failed ones —
            # so the dashboard heartbeat stays current and shows the live watch
            # set, live prices, and stream status.
            try:
                state.publish(
                    cycle=cycle,
                    stats=stats,
                    watching=monitor.active(),
                    scan_window_open=window_open,
                    movers=last_movers,
                    stream_status=stream.status() if stream is not None else None,
                    stream_prices=stream.snapshot() if stream is not None else None,
                    # So the dashboard can tell "catalyst alerts are on and the
                    # wire is quiet" from "never enabled" — both read as 0.
                    features={"catalyst_alerts": catalyst_alerter is not None},
                )
            except Exception:  # publishing is best-effort — never break the loop
                pass

            if max_cycles is None or cycle < max_cycles:
                sleep_fn(interval)
    finally:
        if stream is not None:
            stream.stop()

    return stats
