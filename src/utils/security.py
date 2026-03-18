"""Shared dashboard security middleware (auth + rate limiting)."""

from __future__ import annotations

import secrets
import sqlite3
import threading
import time
from collections import defaultdict, deque
from pathlib import Path

from flask import Request

from src.utils import config
from src.utils.logger import get_logger

log = get_logger(__name__)


class InMemoryRateLimiter:
    def __init__(self, limit_per_minute: int):
        self.limit = max(1, int(limit_per_minute))
        self.window_sec = 60.0
        self._buckets: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.time()
        with self._lock:
            bucket = self._buckets[key]
            while bucket and now - bucket[0] > self.window_sec:
                bucket.popleft()
            if len(bucket) >= self.limit:
                return False
            bucket.append(now)
            return True


class SQLiteRateLimiter:
    def __init__(self, db_path: str, limit_per_minute: int):
        self.limit = max(1, int(limit_per_minute))
        self.window_sec = 60
        self.db_path = db_path
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=5, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _init_db(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS rate_limit_events (
                    client_key TEXT NOT NULL,
                    ts INTEGER NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_rate_limit_client_ts ON rate_limit_events(client_key, ts)"
            )

    def allow(self, key: str) -> bool:
        now = int(time.time())
        cutoff = now - self.window_sec
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM rate_limit_events WHERE ts < ?", (cutoff,))
            count = conn.execute(
                "SELECT COUNT(*) FROM rate_limit_events WHERE client_key = ? AND ts >= ?",
                (key, cutoff),
            ).fetchone()[0]
            if count >= self.limit:
                conn.execute("COMMIT")
                return False
            conn.execute(
                "INSERT INTO rate_limit_events(client_key, ts) VALUES (?, ?)",
                (key, now),
            )
            conn.execute("COMMIT")
        return True


def resolve_client_ip(request: Request) -> str:
    if config.DASHBOARD_TRUST_PROXY:
        xff = request.headers.get("X-Forwarded-For", "").strip()
        if xff:
            return xff.split(",")[0].strip()
    return request.remote_addr or "unknown"


def _extract_supplied_token(request: Request) -> str:
    bearer = request.headers.get("Authorization", "").strip()
    if bearer.lower().startswith("bearer "):
        return bearer[7:].strip()
    return request.headers.get("X-API-Key", "").strip()


def _configured_tokens() -> list[str]:
    raw_multi = (config.DASHBOARD_API_KEYS or "").strip()
    tokens: list[str] = []
    if raw_multi:
        tokens.extend([k.strip() for k in raw_multi.split(",") if k.strip()])
    if config.DASHBOARD_API_KEY:
        tokens.append(config.DASHBOARD_API_KEY.strip())
    seen = set()
    unique_tokens = []
    for token in tokens:
        if token and token not in seen:
            seen.add(token)
            unique_tokens.append(token)
    return unique_tokens


def is_authorized_request(request: Request) -> bool:
    supplied = _extract_supplied_token(request)
    if not supplied:
        return False
    for token in _configured_tokens():
        if secrets.compare_digest(supplied, token):
            return True
    return False


def has_auth_config() -> bool:
    return len(_configured_tokens()) > 0


def build_rate_limiter():
    if (config.DASHBOARD_RATE_LIMIT_BACKEND or "").strip().lower() == "memory":
        log.warning("Using in-memory rate limiter backend")
        return InMemoryRateLimiter(config.DASHBOARD_RATE_LIMIT_PER_MIN)
    return SQLiteRateLimiter(
        db_path=config.DASHBOARD_RATE_LIMIT_DB_PATH,
        limit_per_minute=config.DASHBOARD_RATE_LIMIT_PER_MIN,
    )
