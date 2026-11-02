"""Deterministic decision provider (default; no network)."""

from __future__ import annotations

import re
from datetime import datetime, timezone

from app.decisions.base import Decision
from app.domain.parsing import extract, looks_food_related


class RulesDecisionProvider:
    name = "rules"

    def __init__(self, max_radius: float = 15) -> None:
        self.max_radius = max_radius
        self.calls = 0

    async def decide(self, session_snapshot: dict, new_message: str) -> Decision:
        self.calls += 1
        ex = extract(new_message, now=datetime.now(timezone.utc), max_radius=self.max_radius)
        if re.search(r"\bvote\b", new_message.lower()):
            return Decision("vote", "none", "wait", 0.7, "rules")
        if not ex.empty:
            intent = "modify" if session_snapshot.get("constraint_count") else "search"
            return Decision(intent, "none", "search", 0.9, "rules")
        if looks_food_related(new_message):
            return Decision("clarify", "other", "ask_one_question", 0.5, "rules")
        return Decision("unrelated", "none", "ignore", 0.8, "rules")
