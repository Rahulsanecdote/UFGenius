"""Structured logger — writes to console and logs/bot.log.

Log records are REDACTED before they are emitted. Provider SDKs and `requests`
put the full request URL into their exception text, and several of our data
providers authenticate by query string, so the ordinary idiom
``log.warning(f"{symbol}: Polygon OHLCV failed ({exc})")`` writes a live API key
into logs/bot.log and into whatever the operator redirected stdout to. Observed
2026-09-28: a `--mode validate` run leaked a Polygon key into data/validate.log
on the first 429, and from there into a terminal paste.

The fix belongs HERE rather than at the ~80 call sites that interpolate an
exception, because the leak is a property of the exception text rather than of
any one call site, and a rule that has to be remembered 80 times is a rule that
will be missed on the 81st.
"""

import logging
import re
import sys
from pathlib import Path

_LOG_DIR = Path(__file__).parent.parent.parent / "logs"
_LOG_DIR.mkdir(exist_ok=True)

_FMT = "%(asctime)s [%(levelname)s] %(name)s — %(message)s"
_DATE_FMT = "%Y-%m-%d %H:%M:%S"


# key=value in a query string or a JSON-ish blob. The value runs to the first
# delimiter; `[^&\s"\'#]+` deliberately stops at `&` so only the secret is
# masked and the rest of the URL stays readable for debugging.
_SECRET_RE = re.compile(
    r"(?i)(api[-_]?key|apikey|access[-_]?token|auth[-_]?token|token|secret|"
    r"password|passwd)(\"?\s*[=:]\s*\"?)([^&\s\"\'#,}]{4,})"
)
# `Authorization: Bearer <token>` carries the value after a SPACE rather than a
# delimiter, so the key=value pattern above cannot see it. Alpaca and Anthropic
# both authenticate this way.
_BEARER_RE = re.compile(r"(?i)\b(bearer|basic)(\s+)([A-Za-z0-9\-._~+/=]{8,})")
_MASK = "***REDACTED***"
_MIN_SECRET_LEN = 12          # below this, a config value is a flag, not a key


def _configured_secrets() -> set:
    """Literal secret values from config, to catch one logged OUTSIDE a URL.

    Imported lazily: logger is imported very early and by nearly everything, so
    a module-level config import would invert that order for no benefit. Failure
    is non-fatal — pattern redaction still applies.
    """
    cached = getattr(_configured_secrets, "_cache", None)
    if cached is not None:
        return cached
    values = set()
    try:
        from src.utils import config

        for name in dir(config):
            if not any(t in name for t in ("KEY", "TOKEN", "SECRET", "PASSWORD")):
                continue
            value = getattr(config, name, None)
            if isinstance(value, str) and len(value) >= _MIN_SECRET_LEN:
                values.add(value)
    except Exception:
        pass
    _configured_secrets._cache = values
    return values


def redact(text: str) -> str:
    """Mask credentials in a string bound for a log. Never raises."""
    try:
        out = _SECRET_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{_MASK}", str(text))
        out = _BEARER_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{_MASK}", out)
        for secret in _configured_secrets():
            if secret in out:
                out = out.replace(secret, _MASK)
        return out
    except Exception:
        return str(text)


class _RedactFilter(logging.Filter):
    """Redact the fully-formatted message, then drop args so it is not re-merged."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
            cleaned = redact(message)
            if cleaned != message:
                record.msg, record.args = cleaned, ()
        except Exception:      # logging must never break the caller
            pass
        return True


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    logger.setLevel(logging.DEBUG)

    # Console handler (INFO+)
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(logging.Formatter(_FMT, _DATE_FMT))

    # File handler (DEBUG+)
    fh = logging.FileHandler(_LOG_DIR / "bot.log")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(_FMT, _DATE_FMT))

    # On the LOGGER, not the handlers: a filter on a handler is skipped by any
    # other handler a caller attaches, and this must not be opt-in.
    logger.addFilter(_RedactFilter())
    logger.addHandler(ch)
    logger.addHandler(fh)
    return logger
