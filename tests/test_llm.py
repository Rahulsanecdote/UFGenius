"""Tests for the optional LLM layer (src/llm/).

All hermetic: the network boundary (client.chat / the openai SDK) is mocked, so
these run offline with no API key and no openai package required.
"""

from unittest.mock import MagicMock, patch

import src.utils.config as cfg
from src.llm import client, explain

_SIGNAL = {
    "ticker": "AAPL",
    "signal": "BUY",
    "confidence": "HIGH",
    "score": 71.4,
    "current_price": 189.40,
    "scores": {"technical": 72, "momentum": 65, "volume": 60,
               "sentiment": 55, "fundamental": 80, "macro": 50},
    "reasons": ["Golden Cross", "RSI rising", "Piotroski F-Score: 7/9"],
}


# ── enablement ───────────────────────────────────────────────────────────────

def test_is_enabled_false_without_key(monkeypatch):
    monkeypatch.setattr(cfg, "LLM_API_KEY", "")
    assert client.is_enabled() is False


def test_is_enabled_true_with_key(monkeypatch):
    monkeypatch.setattr(cfg, "LLM_API_KEY", "sk-test")
    assert client.is_enabled() is True


# ── graceful degradation ─────────────────────────────────────────────────────

def test_chat_returns_none_when_disabled(monkeypatch):
    monkeypatch.setattr(cfg, "LLM_API_KEY", "")
    assert client.chat([{"role": "user", "content": "hi"}]) is None


def test_explain_returns_none_when_disabled(monkeypatch):
    monkeypatch.setattr(cfg, "LLM_API_KEY", "")
    assert explain.explain_signal(_SIGNAL) is None


def test_explain_returns_none_for_empty_signal(monkeypatch):
    monkeypatch.setattr(cfg, "LLM_API_KEY", "sk-test")
    assert explain.explain_signal({}) is None


def test_chat_returns_none_when_openai_missing(monkeypatch):
    monkeypatch.setattr(cfg, "LLM_API_KEY", "sk-test")
    # Simulate the openai package not being installed.
    with patch.dict("sys.modules", {"openai": None}):
        assert client.chat([{"role": "user", "content": "hi"}]) is None


def test_chat_returns_none_on_api_error(monkeypatch):
    monkeypatch.setattr(cfg, "LLM_API_KEY", "sk-test")
    fake_openai = MagicMock()
    fake_openai.OpenAI.return_value.chat.completions.create.side_effect = RuntimeError("boom")
    with patch.dict("sys.modules", {"openai": fake_openai}):
        assert client.chat([{"role": "user", "content": "hi"}]) is None


# ── happy path (mocked SDK) ──────────────────────────────────────────────────

def _fake_openai_returning(text):
    fake = MagicMock()
    msg = MagicMock()
    msg.content = text
    fake.OpenAI.return_value.chat.completions.create.return_value.choices = [
        MagicMock(message=msg)
    ]
    return fake


def test_chat_returns_content(monkeypatch):
    monkeypatch.setattr(cfg, "LLM_API_KEY", "sk-test")
    with patch.dict("sys.modules", {"openai": _fake_openai_returning("hello world")}):
        assert client.chat([{"role": "user", "content": "hi"}]) == "hello world"


def test_explain_signal_returns_rationale(monkeypatch):
    monkeypatch.setattr(cfg, "LLM_API_KEY", "sk-test")
    rationale = "Strong fundamentals and a bullish trend drove the BUY. Not financial advice."
    with patch.object(client, "chat", return_value=rationale) as mock_chat:
        out = explain.explain_signal(_SIGNAL, risk_profile="aggressive")
    assert out == rationale
    # The prompt must carry the real factors so the model explains, not invents.
    sent = mock_chat.call_args.args[0]
    user_msg = sent[-1]["content"]
    assert "AAPL" in user_msg and "BUY" in user_msg and "Golden Cross" in user_msg
    assert "aggressive" in user_msg


# ── prompt construction ──────────────────────────────────────────────────────

def test_build_prompt_includes_all_factor_scores():
    prompt = explain.build_prompt(_SIGNAL)
    for factor in ("technical", "momentum", "volume", "sentiment", "fundamental", "macro"):
        assert factor in prompt


def test_build_prompt_handles_missing_fields():
    # Must not raise on a sparse signal dict.
    prompt = explain.build_prompt({"ticker": "MSFT"})
    assert "MSFT" in prompt
