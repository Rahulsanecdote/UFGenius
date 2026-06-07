"""Telegram alerts — gracefully skipped if credentials not configured."""

import requests

from src.utils import config
from src.utils.logger import get_logger

log = get_logger(__name__)


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
    signal = plan.get("signal", "?")
    score = plan.get("composite_score", 0)
    entry = plan.get("entry", {}).get("price", "?")
    stop = plan.get("stop_loss", {}).get("price", "?")

    text = (
        f"🚨 <b>{signal}</b> — <b>{ticker}</b> | Score: {score:.1f}/100\n"
        f"Entry: ${entry} | Stop: ${stop}\n"
        f"⚠️ Not financial advice. Paper trade first."
    )

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