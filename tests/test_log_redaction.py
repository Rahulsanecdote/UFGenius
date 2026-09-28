"""Credentials must not reach a log record (src/utils/logger.py).

Observed 2026-09-28: a `--mode validate` run wrote a live Polygon API key into
data/validate.log on the first HTTP 429, because `requests` puts the whole
request URL into its exception text and several providers authenticate by query
string. The redaction lives on the logger rather than at the ~80 sites that
interpolate an exception, since the leak is a property of the exception text,
not of any one call site.
"""

import logging

import pytest

from src.utils.logger import get_logger, redact

# Shaped like the real leak, with a fake key.
_POLYGON_ERR = (
    "ALB: Polygon OHLCV failed (429 Client Error: Too Many Requests for url: "
    "https://api.polygon.io/v2/aggs/ticker/ALB/range/1/day/2025-09-28/2026-09-28"
    "?adjusted=true&sort=asc&limit=50000&apiKey=FAKEKEY1234567890abcdefGHIJ), "
    "falling back to yfinance"
)
_FAKE = "FAKEKEY1234567890abcdefGHIJ"


class TestRedact:
    def test_the_observed_leak(self):
        out = redact(_POLYGON_ERR)
        assert _FAKE not in out
        assert "***REDACTED***" in out

    def test_the_rest_of_the_url_survives(self):
        """Masking the whole string would destroy the diagnostic value that made
        someone log the exception in the first place."""
        out = redact(_POLYGON_ERR)
        assert "429" in out and "api.polygon.io" in out and "ticker/ALB" in out
        assert "adjusted=true" in out          # stops at `&`, not the whole query

    @pytest.mark.parametrize("raw,secret", [
        ("...&apikey=ABCD1234EFGH5678", "ABCD1234EFGH5678"),
        ("...?api_key=ABCD1234EFGH5678", "ABCD1234EFGH5678"),
        ("...&token=ABCD1234EFGH5678", "ABCD1234EFGH5678"),
        ('{"secret": "ABCD1234EFGH5678"}', "ABCD1234EFGH5678"),
        ("Authorization: Bearer sk-ant-abcdefgh12345678", "sk-ant-abcdefgh12345678"),
        ("Authorization: Basic dXNlcjpwYXNzd29yZA==", "dXNlcjpwYXNzd29yZA=="),
    ])
    def test_credential_shapes(self, raw, secret):
        assert secret not in redact(raw)

    def test_ordinary_messages_are_untouched(self):
        msg = "movers: 42 candidates after filters (min_price=1.0, min_change_pct=3.0)"
        assert redact(msg) == msg

    def test_never_raises(self):
        for junk in (None, 12345, object(), b"bytes"):
            redact(junk)          # must not raise


class TestFilterIsAttached:
    def test_a_leaky_log_call_is_scrubbed_end_to_end(self, caplog):
        """The real test: not the helper, but whether an ordinary log call that
        interpolates a provider exception can still emit the key."""
        log = get_logger("test_redaction_e2e")
        with caplog.at_level(logging.WARNING, logger="test_redaction_e2e"):
            log.warning(_POLYGON_ERR)
        emitted = " ".join(r.getMessage() for r in caplog.records)
        assert _FAKE not in emitted
        assert "***REDACTED***" in emitted

    def test_it_survives_printf_style_args(self, caplog):
        """`log.warning("%s failed (%s)", sym, exc)` formats at emit time, so a
        filter that only inspected record.msg would miss the key entirely."""
        log = get_logger("test_redaction_args")
        with caplog.at_level(logging.WARNING, logger="test_redaction_args"):
            log.warning("%s failed (%s)", "ALB", _POLYGON_ERR)
        emitted = " ".join(r.getMessage() for r in caplog.records)
        assert _FAKE not in emitted

    def test_the_filter_is_on_the_logger_not_a_handler(self):
        """A handler filter is bypassed by any handler a caller adds later."""
        log = get_logger("test_redaction_placement")
        assert any(type(f).__name__ == "_RedactFilter" for f in log.filters)
