"""Telegram alerts — gracefully skipped if credentials not configured."""

from html import escape
from typing import Optional

import requests

from src.utils import config
from src.utils.logger import get_logger

log = get_logger(__name__)

_MAX_TG_LEN = 4096  # Telegram hard limit on message length

# Signal → emoji. Unknown signals fall back to 📊 (never crash).
_SIGNAL_EMOJI = {
    "STRONG_BUY": "🚀",
    "BUY": "📈",
    "WEAK_BUY": "📈",
    "HOLD": "➖",
    "WEAK_SELL": "📉",
    "SELL": "📉",
    "STRONG_SELL": "🔻",
}


def _format_message(plan: dict) -> str:
    """Build the HTML-formatted Telegram message for a trade plan.

    All interpolated free-text values are HTML-escaped (parse_mode=HTML), so a
    ticker/reason containing <, >, or & cannot break or inject markup (audit L2).
    """
    signal = str(plan.get("signal", "?"))
    ticker = str(plan.get("ticker", "?"))
    score = plan.get("composite_score", 0) or 0
    entry = plan.get("entry", {}).get("price", "?")
    stop = plan.get("stop_loss", {}).get("price", "?")
    emoji = _SIGNAL_EMOJI.get(signal, "📊")

    lines = [
        f"{emoji} <b>{escape(signal)}</b> — <b>{escape(ticker)}</b> | Score: {float(score):.1f}/100",
        f"Entry: ${entry} | Stop: ${stop}",
    ]

    targets = plan.get("targets", {}) or {}
    for label in ("T1", "T2", "T3"):
        t = targets.get(label)
        if t:
            lines.append(f"{label}: ${t.get('price', '?')} ({escape(str(t.get('rr', '')))})")

    reasons = [r for r in (plan.get("reasoning", []) or []) if r]
    for r in reasons[:6]:
        lines.append(f"• {escape(str(r))}")

    lines.append("⚠️ NOT FINANCIAL ADVICE. Paper trade first.")
    return "\n".join(lines)


def _credential_shape(value: str) -> dict:
    """Describe a credential WITHOUT revealing it.

    Enough to spot the mistakes that actually happen on a hosting dashboard —
    an empty value, a truncated paste, a trailing newline from copy/paste — and
    nothing an attacker could use. The value itself never appears in the output,
    the logs, or an exception.
    """
    raw = str(value or "")
    stripped = raw.strip()
    return {
        "present": bool(stripped),
        "length": len(stripped),
        "has_surrounding_whitespace": raw != stripped,
    }


def diagnose_telegram(send_test: bool = True) -> dict:
    """Check the Telegram credentials THIS PROCESS actually holds.

    Testing by pasting a URL into a browser tests a URL you typed; this tests
    what the deployment is configured with, which is the thing that decides
    whether an alert is delivered. The two differ exactly when it matters — a
    truncated paste or a trailing newline in the hosting dashboard is invisible
    to a hand-typed test.

    Reports Telegram's OWN ``description`` for failures (e.g. "chat not found",
    "Unauthorized"), which is the actionable part and never contains the token.
    The token is described only by shape. Never raises.
    """
    bot_token = config.env("TELEGRAM_BOT_TOKEN")
    chat_id = config.env("TELEGRAM_CHAT_ID")
    out: dict = {
        "token": _credential_shape(bot_token),
        "chat_id": _credential_shape(chat_id),
        "configured": bool(bot_token.strip() and chat_id.strip()),
    }
    # A well-formed token is "<digits>:<secret>". Getting this wrong is the
    # documented cause of a 404 from the API (as opposed to a 401 for a
    # well-formed but unknown token), so it is worth naming precisely.
    token = bot_token.strip()
    out["token"]["looks_well_formed"] = bool(
        ":" in token and token.split(":", 1)[0].isdigit())

    if not out["configured"]:
        missing = [k for k in ("token", "chat_id") if not out[k]["present"]]
        out["status"] = "not_configured"
        out["detail"] = (f"Missing: {', '.join(missing)}. Alerts are formatted "
                         f"and logged but never delivered.")
        return out

    def _call(method: str, payload: Optional[dict] = None) -> dict:
        url = f"https://api.telegram.org/bot{token}/{method}"
        try:
            # Explicit (connect, read) rather than a scalar, matching
            # utils/http: this runs inside an HTTP request, and a host that
            # blackholes the connection instead of refusing it must not be able
            # to hold the worker open until gunicorn kills it — a killed worker
            # reaches the browser as "Failed to fetch", which looks like a
            # network fault rather than an unreachable Telegram.
            resp = requests.post(
                url, json=payload or {},
                timeout=(config.REQUEST_CONNECT_TIMEOUT_SEC,
                         config.REQUEST_TIMEOUT_SEC))
            body = resp.json() if resp.content else {}
        except Exception as exc:
            # Never interpolate the exception text: a requests error can embed
            # the request URL, which carries the token.
            return {"ok": False, "error": f"{type(exc).__name__} contacting Telegram"}
        if isinstance(body, dict) and body.get("ok"):
            return {"ok": True, "result": body.get("result")}
        description = (body or {}).get("description") if isinstance(body, dict) else None
        return {"ok": False, "http_status": resp.status_code,
                "error": str(description or f"HTTP {resp.status_code}")}

    # getMe isolates the token from the chat id: if this fails the chat id is
    # irrelevant, and saying so stops the operator debugging the wrong half.
    me = _call("getMe")
    if not me["ok"]:
        out["status"] = "bad_token"
        out["detail"] = f"getMe failed: {me['error']}"
        return out
    out["bot_username"] = (me.get("result") or {}).get("username")

    if not send_test:
        out["status"] = "token_ok"
        out["detail"] = f"Token valid (@{out['bot_username']}); no test message sent."
        return out

    sent = _call("sendMessage", {
        "chat_id": chat_id.strip(),
        "text": "✅ UFGenius alert test — delivery is working.",
    })
    if sent["ok"]:
        out["status"] = "ok"
        out["detail"] = f"Test message delivered to chat via @{out['bot_username']}."
    else:
        out["status"] = "bad_chat_id"
        out["detail"] = (f"Token is valid (@{out['bot_username']}) but sending "
                         f"failed: {sent['error']}")
    return out


def send_telegram_message(text: str, *, context: str = "message") -> bool:
    """Send a pre-formatted HTML Telegram message.

    Shared transport for both trade-plan alerts and the P2.3 operational alerts
    (breaker trips, data gaps). Returns True on success, False on failure or when
    Telegram is not configured. Never raises.
    """
    bot_token = config.env("TELEGRAM_BOT_TOKEN")
    chat_id = config.env("TELEGRAM_CHAT_ID")

    if not (bot_token and chat_id):
        log.debug("Telegram not configured — skipping alert")
        return False

    if len(text) > _MAX_TG_LEN:
        text = text[: _MAX_TG_LEN - 20] + "\n…[truncated]"

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}

    try:
        resp = requests.post(url, json=payload,
                             timeout=(config.REQUEST_CONNECT_TIMEOUT_SEC,
                                      config.REQUEST_TIMEOUT_SEC))
        resp.raise_for_status()
        log.info(f"Telegram alert sent ({context})")
        return True
    except Exception as exc:
        # Log only the exception TYPE, never the exception text: a requests
        # error can embed the request URL, which carries TELEGRAM_BOT_TOKEN.
        log.warning(f"Telegram alert failed ({context}): {type(exc).__name__}")
        return False


def send_telegram_alert(plan: dict) -> bool:
    """
    Send a Telegram alert for a trade plan.

    Returns True on success, False on failure or not configured.
    """
    ticker = plan.get("ticker", "?")
    return send_telegram_message(_format_message(plan), context=f"plan {ticker}")


def send_text_alert(text: str, *, context: str = "alert") -> bool:
    """Send a plain-text operational alert (HTML-escaped) via Telegram."""
    return send_telegram_message(escape(str(text)), context=context)