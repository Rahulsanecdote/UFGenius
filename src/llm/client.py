"""Provider-agnostic chat client over any OpenAI-compatible endpoint.

One thin wrapper the rest of the codebase calls instead of talking to a vendor
SDK directly. Because NVIDIA (Nemotron), OpenAI, OpenRouter, Together, and a
local vLLM server all speak the same Chat Completions API, switching providers
is pure config — ``LLM_BASE_URL`` / ``LLM_API_KEY`` / ``LLM_MODEL`` — with no
code change.

Design mirrors the sentiment/news path: it degrades gracefully. No key set, the
``openai`` package missing, or any request error all resolve to ``None`` rather
than raising, so a caller can always treat the LLM as best-effort enrichment.
"""

from __future__ import annotations

from typing import Optional

from src.utils import config
from src.utils.logger import get_logger

log = get_logger(__name__)


def is_enabled() -> bool:
    """True when an API key is configured. When False, chat() is a no-op."""
    return bool(config.LLM_API_KEY)


def chat(
    messages: list[dict],
    *,
    max_tokens: Optional[int] = None,
    temperature: Optional[float] = None,
) -> Optional[str]:
    """Send a chat-completion request; return the reply text, or None.

    Returns None (never raises) when the LLM is disabled, the ``openai`` package
    is not installed, or the request fails for any reason — the caller decides
    how to fall back.

    Args:
        messages: OpenAI-style [{"role": "system"|"user"|"assistant", "content": str}].
        max_tokens: Response cap; defaults to config.LLM_MAX_TOKENS.
        temperature: Sampling temperature; defaults to config.LLM_TEMPERATURE.
    """
    if not is_enabled():
        log.debug("LLM disabled (LLM_API_KEY not set) — skipping chat call")
        return None

    try:
        from openai import OpenAI
    except ImportError:
        log.warning("openai package not installed — LLM features unavailable "
                    "(pip install openai)")
        return None

    try:
        client = OpenAI(
            api_key=config.LLM_API_KEY,
            base_url=config.LLM_BASE_URL,
            timeout=config.LLM_TIMEOUT_SEC,
        )
        response = client.chat.completions.create(
            model=config.LLM_MODEL,
            messages=messages,
            max_tokens=max_tokens if max_tokens is not None else config.LLM_MAX_TOKENS,
            temperature=temperature if temperature is not None else config.LLM_TEMPERATURE,
        )
        content = (response.choices[0].message.content or "").strip()
        return content or None
    except Exception as exc:  # noqa: BLE001 — best-effort enrichment, never fatal
        log.warning(f"LLM request failed ({config.LLM_MODEL} @ {config.LLM_BASE_URL}): {exc}")
        return None
