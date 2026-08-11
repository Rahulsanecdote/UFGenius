"""Plain-English rationale for a generated signal, via the optional LLM layer.

Takes the signal dict that ``signals.generator.generate_signal`` already
produces and asks the model to narrate *why* — using only the factors the
pipeline computed. The model explains an existing, deterministic decision; it
never makes the call, invents numbers, or gives investment advice.

Returns None when the LLM is disabled or the request fails, so callers can fall
back to the templated ``reasons`` list the pipeline already provides.
"""

from __future__ import annotations

from typing import Optional

from src.llm import client
from src.utils.logger import get_logger

log = get_logger(__name__)

_SYSTEM_PROMPT = (
    "You are a financial-data explainer for an educational stock-signal tool. "
    "Explain, in plain English, WHY the model produced the given signal, using "
    "ONLY the factor scores and reasons provided. Rules: do not give investment "
    "advice or tell the user to buy/sell/hold; do not invent numbers, prices, or "
    "facts beyond what is given; 2-3 sentences, neutral and factual. Always end "
    "with: 'Not financial advice.'"
)


def _format_scores(scores: dict) -> str:
    if not isinstance(scores, dict) or not scores:
        return "unavailable"
    order = ["technical", "momentum", "volume", "sentiment", "fundamental", "macro"]
    parts = [f"{k}={scores[k]}" for k in order if k in scores]
    # Include any extra keys we didn't anticipate, so the model sees everything.
    parts += [f"{k}={v}" for k, v in scores.items() if k not in order]
    return ", ".join(parts) if parts else "unavailable"


def build_prompt(signal: dict, *, risk_profile: str = "standard") -> str:
    """Compose the user prompt from a signal dict. Public for testing/inspection.

    Accepts either the raw ``generate_signal`` dict (``score`` / ``reasons`` /
    ``current_price``) or the augmented trade plan the scanner returns
    (``composite_score`` / ``reasoning``, price under ``entry.price``), so the
    dashboard can hand its result object straight through.
    """
    reasons = signal.get("reasons") or signal.get("reasoning") or []
    reason_lines = "\n".join(f"- {r}" for r in reasons[:10]) or "- (none provided)"
    score = signal.get("score", signal.get("composite_score", "?"))
    price = signal.get("current_price") or (signal.get("entry") or {}).get("price", "?")
    return (
        f"Ticker: {signal.get('ticker', '?')}\n"
        f"Signal: {signal.get('signal', '?')}  "
        f"(confidence: {signal.get('confidence', '?')})\n"
        f"Composite score: {score} / 100\n"
        f"Current price: {price}\n"
        f"Factor scores (0-100): {_format_scores(signal.get('scores', {}))}\n"
        f"Reader risk profile: {risk_profile}\n"
        f"Contributing reasons:\n{reason_lines}\n\n"
        "Explain why this signal was produced."
    )


def explain_signal(signal: dict, *, risk_profile: str = "standard") -> Optional[str]:
    """Return a short natural-language rationale for a signal, or None.

    None means the LLM is disabled or unavailable — the caller should fall back
    to the pipeline's own ``reasons`` list.
    """
    if not isinstance(signal, dict) or not signal.get("ticker"):
        return None
    if not client.is_enabled():
        return None

    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": build_prompt(signal, risk_profile=risk_profile)},
    ]
    return client.chat(messages)
