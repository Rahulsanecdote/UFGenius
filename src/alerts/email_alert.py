"""Email digest alerts — gracefully skipped if credentials not configured."""

import smtplib
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from src.utils import config
from src.utils.logger import get_logger

log = get_logger(__name__)


def send_scan_digest(scan_result: dict) -> bool:
    """
    Send a daily scan digest email.

    Returns True on success, False on failure or not configured.
    """
    email_from = config.env("EMAIL_FROM")
    email_pass = config.env("EMAIL_PASSWORD")
    email_to   = config.env("EMAIL_TO")

    if not (email_from and email_pass and email_to):
        log.debug("Email not configured — skipping digest")
        return False

    subject = (
        f"[SignalBot] {datetime.now().strftime('%Y-%m-%d')} — "
        f"Regime: {scan_result.get('market_regime', 'N/A')} | "
        f"{len(scan_result.get('strong_buys', []))} STRONG BUY"
    )

    html = _format_html(scan_result)

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = email_from
        msg["To"]      = email_to
        msg.attach(MIMEText(html, "html"))

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(email_from, email_pass)
            server.sendmail(email_from, email_to, msg.as_string())

        log.info("Daily digest email sent")
        return True
    except Exception as e:
        log.error(f"Email digest failed: {e}")
        return False


def _format_html(scan: dict) -> str:
    regime = scan.get("market_regime", "N/A")
    vix    = scan.get("vix_level", "N/A")
    date   = scan.get("scan_date", "N/A")
    total  = scan.get("total_scanned", 0)

    rows = ""
    for category, plans in [
        ("🚀 STRONG BUY", scan.get("strong_buys", [])),
        ("📈 BUY",        scan.get("buys", [])),
        ("🔍 WATCH",      scan.get("watch_list", [])),
    ]:
        for p in plans:
            entry  = p.get("entry", {})
            stop   = p.get("stop_loss", {})
            t1     = p.get("targets", {}).get("T1", {})
            pos    = p.get("position", {})
            rows += f"""
            <tr>
                <td><b>{p.get('ticker')}</b></td>
                <td>{category}</td>
                <td>{p.get('composite_score', 0):.1f}</td>
                <td>${entry.get('price', '-')}</td>
                <td>${stop.get('price', '-')}</td>
                <td>${t1.get('price', '-')}</td>
                <td>{pos.get('shares', '-')}</td>
                <td>${pos.get('risk_dollars', '-')} ({pos.get('risk_percent', '-')}%)</td>
            </tr>"""

    return f"""
    <html><body>
    <h2>Alpaca Signal Bot — Daily Scan</h2>
    <p><b>Date:</b> {date} | <b>Regime:</b> {regime} | <b>VIX:</b> {vix} | <b>Scanned:</b> {total}</p>
    <table border="1" cellpadding="5" cellspacing="0">
        <tr>
            <th>Ticker</th><th>Signal</th><th>Score</th>
            <th>Entry</th><th>Stop</th><th>T1</th>
            <th>Shares</th><th>Risk</th>
        </tr>
        {rows}
    </table>
    <p><i>⚠️ NOT FINANCIAL ADVICE. All trading involves risk. Paper trade first.</i></p>
    </body></html>"""
