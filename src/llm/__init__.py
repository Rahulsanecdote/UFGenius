"""Optional LLM layer — provider-agnostic, OpenAI-compatible.

Entirely opt-in: every entry point returns a neutral no-op (``None``) when
``LLM_API_KEY`` is unset, so the rest of the bot runs unchanged without it.
"""
