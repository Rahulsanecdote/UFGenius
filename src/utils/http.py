"""HTTP helpers with retry, backoff, and sane defaults."""

from __future__ import annotations

import time
from functools import lru_cache
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from src.utils import config


def _retry_strategy() -> Retry:
    return Retry(
        total=config.REQUEST_MAX_RETRIES,
        connect=config.REQUEST_MAX_RETRIES,
        read=config.REQUEST_MAX_RETRIES,
        status=config.REQUEST_MAX_RETRIES,
        allowed_methods=frozenset({"GET", "POST"}),
        status_forcelist=(429, 500, 502, 503, 504),
        backoff_factor=config.REQUEST_BACKOFF_SEC,
        raise_on_status=False,
    )


@lru_cache(maxsize=1)
def get_retry_session() -> requests.Session:
    session = requests.Session()
    adapter = HTTPAdapter(
        max_retries=_retry_strategy(),
        pool_connections=config.REQUEST_POOL_SIZE,
        pool_maxsize=config.REQUEST_POOL_SIZE,
    )
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def get_json(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout: tuple[float, float] | None = None,
) -> dict[str, Any]:
    effective_timeout = timeout or (
        config.REQUEST_CONNECT_TIMEOUT_SEC,
        config.REQUEST_TIMEOUT_SEC,
    )
    response = get_retry_session().get(url, headers=headers, timeout=effective_timeout)
    response.raise_for_status()
    return response.json()


def get_text(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout: tuple[float, float] | None = None,
) -> str:
    effective_timeout = timeout or (
        config.REQUEST_CONNECT_TIMEOUT_SEC,
        config.REQUEST_TIMEOUT_SEC,
    )
    response = get_retry_session().get(url, headers=headers, timeout=effective_timeout)
    response.raise_for_status()
    return response.text


def post_form(
    url: str,
    data: dict[str, Any],
    *,
    headers: dict[str, str] | None = None,
    timeout: tuple[float, float] | None = None,
) -> requests.Response:
    effective_timeout = timeout or (
        config.REQUEST_CONNECT_TIMEOUT_SEC,
        config.REQUEST_TIMEOUT_SEC,
    )
    response = get_retry_session().post(url, data=data, headers=headers, timeout=effective_timeout)
    response.raise_for_status()
    return response


def retry_call(fn, *args, retries: int | None = None, backoff: float | None = None, **kwargs):
    max_retries = retries if retries is not None else config.REQUEST_MAX_RETRIES
    backoff_sec = backoff if backoff is not None else config.REQUEST_BACKOFF_SEC
    last_err = None
    for attempt in range(max_retries + 1):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # pragma: no cover - exercised via call sites
            last_err = exc
            if attempt >= max_retries:
                raise
            time.sleep(backoff_sec * (2 ** attempt))
    if last_err:
        raise last_err
    raise RuntimeError("retry_call exhausted without exception context")
