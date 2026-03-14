"""Telegram bot notifications — gracefully skipped if token not configured."""

from src.utils import config
from src.utils.http import post_form
from src.utils.logger import get_logger

log = get_logger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"


def send_telegram_alert(trade_plan: dict) -> bool:
    """
    Send a formatted signal alert to Telegram.

    Returns True on success, False on failure or if not configured.
    """
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        log.debug("Telegram not configured — skipping alert")
        return False

    message = _format_message(trade_plan)

    # Telegram Bot API silently truncates messages over 4096 chars
    _MAX_TG_LEN = 4096
    if len(message) > _MAX_TG_LEN:
        message = message[: _MAX_TG_LEN - 20] + "\n…[truncated]"

    try:
        url = TELEGRAM_API.format(token=config.TELEGRAM_BOT_TOKEN)
        post_form(
            url,
            data={
                "chat_id":    config.TELEGRAM_CHAT_ID,
                "text":       message,
                "parse_mode": "HTML",
            },
        )
        log.info(f"Telegram alert sent for {trade_plan.get('ticker')}")
        return True
    except Exception as e:
        log.error(f"Telegram alert failed: {e}")
        return False


def _format_message(plan: dict) -> str:
    signal    = plan.get("signal", "?")
    ticker    = plan.get("ticker", "?")
    score     = plan.get("composite_score", 0)
    entry     = plan.get("entry", {})
    stop      = plan.get("stop_loss", {})
    targets   = plan.get("targets", {})
    position  = plan.get("position", {})
    reasons   = plan.get("reasoning", [])

    emoji = {
        "STRONG_BUY": "🚀",
        "BUY":        "📈",
        "WEAK_BUY":   "🔍",
        "SELL":       "📉",
        "STRONG_SELL":"🚨",
    }.get(signal, "📊")

    t1 = targets.get("T1", {})
    t2 = targets.get("T2", {})

    top_reasons = "\n".join(f"• {r}" for r in reasons[:4]) if reasons else "N/A"

    return f"""{emoji} <b>{signal}</b> — <b>{ticker}</b>

💯 Score: <b>{score:.1f}/100</b>
📈 Entry:    <b>${entry.get('price', '?')}</b>
🛑 Stop:     <b>${stop.get('price', '?')}</b> ({stop.get('pct_below_entry', '?')}% below)
🎯 T1:       <b>${t1.get('price', '?')}</b> ({t1.get('rr', '?')} R:R)
🎯 T2:       <b>${t2.get('price', '?')}</b> ({t2.get('rr', '?')} R:R)

💰 Shares: {position.get('shares', '?')}
⚠️ Risk: ${position.get('risk_dollars', '?')} ({position.get('risk_percent', '?')}% of account)

<b>Top Reasons:</b>
{top_reasons}

⚠️ <i>NOT FINANCIAL ADVICE. Paper trade first!</i>"""
