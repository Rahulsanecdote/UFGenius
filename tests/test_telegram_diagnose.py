"""Tests for the Telegram delivery self-test (src/alerts/telegram_alert.py).

Offline: the HTTP boundary is stubbed. Two things are pinned — that each
failure mode is named precisely enough to act on (bad token vs bad chat id vs
not configured), and that the credentials never appear in the output, because a
diagnostic that leaks a bot token is worse than no diagnostic.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from src.alerts.telegram_alert import _credential_shape, diagnose_telegram

_TOKEN = "123456789:AAHsuperSecretTokenValueDoNotLeak"
_CHAT = "987654321"


class _Resp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.content = json.dumps(payload).encode()

    def json(self):
        return self._payload


@pytest.fixture
def _creds(monkeypatch):
    env = {"TELEGRAM_BOT_TOKEN": _TOKEN, "TELEGRAM_CHAT_ID": _CHAT}
    monkeypatch.setattr("src.alerts.telegram_alert.config.env",
                        lambda key, default="": env.get(key, default))


class TestCredentialShape:
    def test_describes_without_revealing(self):
        shape = _credential_shape(_TOKEN)
        assert shape["present"] is True
        assert shape["length"] == len(_TOKEN)
        assert _TOKEN not in json.dumps(shape)

    def test_flags_surrounding_whitespace(self):
        # The copy/paste failure a hosting dashboard makes invisible.
        assert _credential_shape(f"  {_TOKEN}\n")["has_surrounding_whitespace"] is True
        assert _credential_shape(_TOKEN)["has_surrounding_whitespace"] is False

    def test_blank_is_absent(self):
        assert _credential_shape("   ")["present"] is False
        assert _credential_shape(None)["present"] is False


class TestDiagnose:
    def test_missing_credentials_named(self, monkeypatch):
        monkeypatch.setattr("src.alerts.telegram_alert.config.env",
                            lambda key, default="": _TOKEN
                            if key == "TELEGRAM_BOT_TOKEN" else "")
        out = diagnose_telegram()
        assert out["status"] == "not_configured"
        assert "chat_id" in out["detail"]

    def test_happy_path(self, _creds):
        with patch("src.alerts.telegram_alert.requests.post") as post:
            post.side_effect = [
                _Resp({"ok": True, "result": {"username": "UFGeniusBot"}}),
                _Resp({"ok": True, "result": {"message_id": 42}}),
            ]
            out = diagnose_telegram()
        assert out["status"] == "ok"
        assert out["bot_username"] == "UFGeniusBot"

    def test_bad_token_reported_before_chat_id(self, _creds):
        # getMe first: if the token is wrong the chat id is irrelevant, and
        # saying so stops the operator debugging the wrong half.
        with patch("src.alerts.telegram_alert.requests.post") as post:
            post.return_value = _Resp({"ok": False, "description": "Unauthorized"}, 401)
            out = diagnose_telegram()
        assert out["status"] == "bad_token"
        assert "Unauthorized" in out["detail"]
        assert post.call_count == 1        # never tried to send

    def test_bad_chat_id_distinguished_from_bad_token(self, _creds):
        with patch("src.alerts.telegram_alert.requests.post") as post:
            post.side_effect = [
                _Resp({"ok": True, "result": {"username": "UFGeniusBot"}}),
                _Resp({"ok": False, "description": "Bad Request: chat not found"}, 400),
            ]
            out = diagnose_telegram()
        assert out["status"] == "bad_chat_id"
        assert "chat not found" in out["detail"]

    def test_malformed_token_shape_is_flagged(self, monkeypatch):
        # A token that is not "<digits>:<secret>" is what produces a 404 from
        # the API (vs 401 for a well-formed but unknown one).
        monkeypatch.setattr("src.alerts.telegram_alert.config.env",
                            lambda key, default="": "not-a-real-token"
                            if key == "TELEGRAM_BOT_TOKEN" else _CHAT)
        with patch("src.alerts.telegram_alert.requests.post") as post:
            post.return_value = _Resp({"ok": False, "description": "Not Found"}, 404)
            out = diagnose_telegram()
        assert out["token"]["looks_well_formed"] is False

    def test_network_failure_reports_type_only(self, _creds):
        # A requests exception can embed the request URL, which carries the
        # token — so only the exception TYPE may be surfaced.
        with patch("src.alerts.telegram_alert.requests.post") as post:
            post.side_effect = RuntimeError(f"failed connecting to bot{_TOKEN}/getMe")
            out = diagnose_telegram()
        assert out["status"] == "bad_token"
        assert "RuntimeError" in out["detail"]
        assert _TOKEN not in json.dumps(out)

    def test_send_test_false_stops_after_token_check(self, _creds):
        with patch("src.alerts.telegram_alert.requests.post") as post:
            post.return_value = _Resp({"ok": True, "result": {"username": "B"}})
            out = diagnose_telegram(send_test=False)
        assert out["status"] == "token_ok"
        assert post.call_count == 1

    @pytest.mark.parametrize("scenario", ["ok", "bad_token", "bad_chat_id"])
    def test_credentials_never_appear_in_output(self, _creds, scenario):
        responses = {
            "ok": [_Resp({"ok": True, "result": {"username": "B"}}),
                   _Resp({"ok": True, "result": {}})],
            "bad_token": [_Resp({"ok": False, "description": "Unauthorized"}, 401)],
            "bad_chat_id": [_Resp({"ok": True, "result": {"username": "B"}}),
                            _Resp({"ok": False, "description": "chat not found"}, 400)],
        }[scenario]
        with patch("src.alerts.telegram_alert.requests.post") as post:
            post.side_effect = responses
            out = diagnose_telegram()
        blob = json.dumps(out)
        assert _TOKEN not in blob
        assert _TOKEN.split(":", 1)[1] not in blob    # not even the secret half
