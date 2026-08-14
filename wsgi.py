"""WSGI entrypoint for production hosts."""

from dashboard import app

# Optionally run the always-on movers worker inside this web process so the
# dashboard's worker strip + live-streaming prices populate (they share one
# filesystem). Opt-in via RUN_WORKER_IN_PROCESS; a no-op otherwise. Done here,
# in the gunicorn entrypoint, so importing `dashboard` stays side-effect-free.
from src.scanner.worker_launcher import maybe_start_worker_thread

maybe_start_worker_thread()
