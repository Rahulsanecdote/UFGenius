"""
Catalyst gate (upgrade plan P1.4).

A deterministic veto for entries carrying a hard catalyst tag — a trading halt, a
fraud/SEC investigation, a going-concern/bankruptcy flag, etc. The plan's stance:
optional catalyst tags **bias or veto** an entry, but there is *never a naked buy*
into a known catalyst.

Tags come from the plan itself (``plan["catalyst_tags"]``), so any upstream source
— a news classifier, an insider/8-K feed, a prediction-market signal — can attach
them without this module taking a network dependency. The gate just decides:
**veto** if any tag is in the configured veto list, else **clear**. Case-
insensitive, and tolerant of missing/garbage tag input (fails open — no tags means
nothing to veto).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from src.utils import config


@dataclass(frozen=True)
class CatalystDecision:
    action: str  # "veto" | "clear"
    reasons: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)

    @property
    def vetoed(self) -> bool:
        return self.action == "veto"


class CatalystGate:
    """Vetoes entries whose catalyst tags intersect the configured veto set."""

    def __init__(self, veto_tags: Optional[list[str]] = None) -> None:
        source = veto_tags if veto_tags is not None else config.CATALYST_VETO_TAGS
        # Strip + upper: this is a money-path gate, so " FRAUD" from an upstream
        # feed must still match "FRAUD". Normalize both sides totally.
        self._veto_tags = {str(t).strip().upper() for t in (source or []) if str(t).strip()}

    def evaluate(self, ticker: str, tags=None) -> CatalystDecision:
        """Decide veto/clear for a ticker given its catalyst ``tags``."""
        clean: list[str] = []
        if isinstance(tags, (list, tuple, set)):
            clean = [str(t).strip().upper() for t in tags if t is not None and str(t).strip()]
        elif isinstance(tags, str):
            clean = [tags.strip().upper()] if tags.strip() else []
        hits = [t for t in clean if t in self._veto_tags]
        if hits:
            return CatalystDecision(
                action="veto",
                reasons=[f"{ticker} carries veto catalyst tag(s): {', '.join(sorted(set(hits)))}"],
                tags=clean,
            )
        return CatalystDecision(action="clear", reasons=[], tags=clean)
