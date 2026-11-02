"""Decision provider interface: bounded intent classification, never entity extraction."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

INTENTS = ("search", "modify", "vote", "unrelated", "clarify")
CONFLICTS = ("none", "cuisine", "budget", "location", "dietary", "other")
NEXT_ACTIONS = ("search", "ask_one_question", "wait", "ignore")


@dataclass(frozen=True)
class Decision:
    intent: str
    conflict_type: str
    next_action: str
    confidence: float
    source: str  # rules | jev | fallback
    error: str | None = None

    @staticmethod
    def unknown(source: str = "fallback", error: str | None = None) -> Decision:
        return Decision("clarify", "other", "wait", 0.0, source, error)


class DecisionProvider(Protocol):
    name: str

    async def decide(self, session_snapshot: dict, new_message: str) -> Decision:
        """Return validated intent, conflict_type, next_action, confidence, with unknown fallback."""
        ...
