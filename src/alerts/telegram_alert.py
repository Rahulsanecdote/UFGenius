"""Telegram alerts — gracefully skipped if credentials not configured."""

from html import escape

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


def send_telegram_alert(plan: dict) -> bool:
    """
    Send a Telegram alert for a trade plan.

    Returns True on success, False on failure or not configured.
    """
    bot_token = config.env("TELEGRAM_BOT_TOKEN")
    chat_id = config.env("TELEGRAM_CHAT_ID")

    if not (bot_token and chat_id):
        log.debug("Telegram not configured — skipping alert")
        return False

    ticker = plan.get("ticker", "?")
    text = _format_message(plan)
    if len(text) > _MAX_TG_LEN:
        text = text[: _MAX_TG_LEN - 20] + "\n…[truncated]"

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}

    try:
        resp = requests.post(url, json=payload, timeout=10)
        resp.raise_for_status()
        log.info(f"Telegram alert sent for {ticker}")
        return True
    except Exception as exc:
        log.warning(f"Telegram alert failed for {ticker}: {exc}")
        return False