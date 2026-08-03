"""Tests for src/utils/http.py — retry_call and session helpers."""

import pytest
import requests

from src.utils.http import get_retry_session, retry_call


def test_retry_call_returns_on_first_try():
    result = retry_call(lambda: 42)
    assert result == 42


def test_retry_call_succeeds_after_retries():
    attempts = {"count": 0}

    def flaky():
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise ConnectionError("transient")
        return "ok"

    result = retry_call(flaky, retries=3, backoff=0)
    assert result == "ok"
    assert attempts["count"] == 3


def test_retry_call_raises_after_exhausting_retries():
    def always_fails():
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        retry_call(always_fails, retries=2, backoff=0)


def test_retry_call_zero_retries_raises_immediately():
    calls = {"n": 0}

    def fails():
        calls["n"] += 1
        raise RuntimeError("instant")

    with pytest.raises(RuntimeError):
        retry_call(fails, retries=0, backoff=0)

    assert calls["n"] == 1


def test_get_retry_session_returns_requests_session():
    session = get_retry_session()
    assert isinstance(session, requests.Session)


def test_get_retry_session_is_cached():
    s1 = get_retry_session()
    s2 = get_retry_session()
    assert s1 is s2
