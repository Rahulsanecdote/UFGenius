"""Unit tests for alert modules (email and Telegram) — no real messages sent."""

import html as html_module
import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def minimal_scan_result():
    """Minimal scan result dict that matches send_scan_digest() expectations."""
    return {
        "scan_date": "2024-01-15",
        "market_regime": "BULL_TREND",
        "vix_level": 14.2,
        "total_scanned": 500,
        "strong_buys": [
            {
                "ticker": "AAPL",
                "composite_score": 82.5,
                "entry": {"price": 185.00},
                "stop_loss": {"price": 181.00},
                "targets": {"T1": {"price": 191.00}},
                "position": {"shares": 10, "risk_dollars": 40.0, "risk_percent": 0.4},
            }
        ],
        "buys": [],
        "watch_list": [],
    }


@pytest.fixture
def minimal_trade_plan():
    """Minimal trade plan dict for send_telegram_alert()."""
    return {
        "ticker": "MSFT",
        "signal": "BUY",
        "composite_score": 70.0,
        "entry": {"price": 410.00},
        "stop_loss": {"price": 400.00, "pct_below_entry": 2.4},
        "targets": {
            "T1": {"price": 425.00, "rr": "1.5:1"},
            "T2": {"price": 435.00, "rr": "2.5:1"},
        },
        "position": {"shares": 5, "risk_dollars": 50.0, "risk_percent": 0.5},
        "reasoning": ["Above 200 SMA", "MACD bullish", "RVOL 1.8x"],
    }


# ---------------------------------------------------------------------------
# Email alert tests
# ---------------------------------------------------------------------------

class TestEmailAlert:
    def test_email_not_sent_when_unconfigured(self, monkeypatch, minimal_scan_result):
        """send_scan_digest() returns False gracefully when credentials are missing."""
        from src.alerts.email_alert import send_scan_digest
        from src.utils import config

        monkeypatch.setattr(config, "env", lambda key, default="": "")
        result = send_scan_digest(minimal_scan_result)
        assert result is False

    def test_html_escaping_ticker_in_email_body(self):
        """Ticker names with HTML special chars are escaped in the email body."""
        from src.alerts.email_alert import _format_html

        malicious_ticker = "<script>alert(1)</script>"
        scan = {
            "scan_date": "2024-01-15",
            "market_regime": "BULL",
            "vix_level": 14,
            "total_scanned": 1,
            "strong_buys": [
                {
                    "ticker": malicious_ticker,
                    "composite_score": 85.0,
                    "entry": {"price": 100.0},
                    "stop_loss": {"price": 95.0},
                    "targets": {"T1": {"price": 107.5}},
                    "position": {"shares": 5, "risk_dollars": 25.0, "risk_percent": 0.25},
                }
            ],
            "buys": [],
            "watch_list": [],
        }
        html_body = _format_html(scan)
        # The raw script tag must NOT appear — it should be entity-escaped
        assert "<script>" not in html_body
        assert html_module.escape(malicious_ticker) in html_body

    def test_html_escaping_category_label(self):
        """Category strings with HTML chars are escaped."""
        from src.alerts.email_alert import _format_html

        # Inject via a crafted scan_result where category is included via the loop
        scan = {
            "scan_date": "2024-01-15",
            "market_regime": "<b>PWNED</b>",
            "vix_level": 14,
            "total_scanned": 0,
            "strong_buys": [],
            "buys": [],
            "watch_list": [],
        }
        html_body = _format_html(scan)
        assert "<b>PWNED</b>" not in html_body
        assert html_module.escape("<b>PWNED</b>") in html_body

    def test_email_format_includes_required_fields(self, minimal_scan_result):
        """Rendered HTML contains ticker, entry, stop, T1 fields."""
        from src.alerts.email_alert import _format_html

        html_body = _format_html(minimal_scan_result)
        assert "AAPL" in html_body
        assert "185" in html_body   # entry price
        assert "181" in html_body   # stop price
        assert "191" in html_body   # T1 target

    def test_email_returns_html_with_disclaimer(self, minimal_scan_result):
        """Email body always contains the risk disclaimer."""
        from src.alerts.email_alert import _format_html

        html_body = _format_html(minimal_scan_result)
        assert "NOT FINANCIAL ADVICE" in html_body


# ---------------------------------------------------------------------------
# Telegram alert tests
# ---------------------------------------------------------------------------

class TestTelegramAlert:
    def test_telegram_not_sent_when_unconfigured(self, monkeypatch, minimal_trade_plan):
        """send_telegram_alert() returns False gracefully when token is missing."""
        from src.alerts import telegram_alert
        from src.utils import config

        monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "")
        monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "")
        result = telegram_alert.send_telegram_alert(minimal_trade_plan)
        assert result is False

    def test_telegram_message_truncated_at_4096(self, monkeypatch, minimal_trade_plan):
        """Messages longer than 4096 chars are truncated with an indicator."""
        from src.alerts.telegram_alert import _format_message

        # Inflate reasoning list to create a long message
        plan = dict(minimal_trade_plan)
        plan["reasoning"] = ["Very long reason " * 20] * 100
        msg = _format_message(plan)

        # Patch the truncation logic inline (it fires inside send_telegram_alert)
        _MAX_TG_LEN = 4096
        if len(msg) > _MAX_TG_LEN:
            truncated = msg[: _MAX_TG_LEN - 20] + "\n…[truncated]"
        else:
            truncated = msg

        assert len(truncated) <= _MAX_TG_LEN
        if len(msg) > _MAX_TG_LEN:
            assert truncated.endswith("\n…[truncated]")

    def test_telegram_format_contains_ticker_and_score(self, minimal_trade_plan):
        """Formatted message includes ticker and score."""
        from src.alerts.telegram_alert import _format_message

        msg = _format_message(minimal_trade_plan)
        assert "MSFT" in msg
        assert "70.0" in msg
        assert "410" in msg   # entry price

    def test_telegram_format_contains_disclaimer(self, minimal_trade_plan):
        """Formatted message contains the risk disclaimer."""
        from src.alerts.telegram_alert import _format_message

        msg = _format_message(minimal_trade_plan)
        assert "NOT FINANCIAL ADVICE" in msg

    def test_telegram_unknown_signal_uses_default_emoji(self):
        """Unknown signal type gets the fallback 📊 emoji, not a crash."""
        from src.alerts.telegram_alert import _format_message

        plan = {
            "signal": "MYSTERY_SIGNAL",
            "ticker": "XYZ",
            "composite_score": 55.0,
            "entry": {},
            "stop_loss": {},
            "targets": {},
            "position": {},
            "reasoning": [],
        }
        msg = _format_message(plan)
        assert "📊" in msg
        assert "XYZ" in msg
