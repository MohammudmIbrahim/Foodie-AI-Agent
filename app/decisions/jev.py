"""Jev structured-decision adapter.

Two hosted providers document different request schemas; they are NOT mixed. JEV_SCHEMA selects:

- `typesafe_systemone`: POST https://api.typesafe.ai/v1/systemone
  body {"model", "state", "questions"}; models e.g. jev-latest / jev-1.13.0.
- `jevtypesafe_decide`: POST https://jevtypesafe.org/api/v1/decide
  body {"state", "questions"}.

Both: `Authorization: Bearer <key>`; `questions` maps ids to {"type": "choice", "instructions",
"criteria": {option: description}}; response {"model", "answers": {id: {"type", "choice",
"confidence", "probabilities"}}, "usage": {"input_tokens", "output_tokens"}}.

Jev only classifies (intent / conflict / next action). It never extracts entities, ranks
restaurants, decides votes or relaxes safety rules. One batched call per decision, no retries,
hard per-session and per-day call caps, and minimal redacted context (never history or phone
numbers).
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from datetime import date

import httpx

from app.decisions.base import CONFLICTS, INTENTS, NEXT_ACTIONS, Decision
from app.security.redaction import redact

log = logging.getLogger("foodie.jev")


class JevAuthError(RuntimeError):
    """401/403: configuration problem. Fail closed: stop calling Jev until fixed."""


QUESTIONS: dict[str, dict] = {
    "intent": {
        "type": "choice",
        "instructions": "Classify what the new group-chat message wants from a restaurant-planning assistant.",
        "criteria": {
            "search": "Asks to find or start looking for places to eat.",
            "modify": "Changes or adds a requirement (cuisine, budget, distance, area, time, diet).",
            "vote": "Expresses a choice between already-listed options.",
            "unrelated": "Not about the current food plan.",
            "clarify": "About the food plan but too vague to act on.",
        },
    },
    "conflict": {
        "type": "choice",
        "instructions": "Does the new message conflict with the current plan? Pick the main conflict.",
        "criteria": {
            "none": "No conflict.",
            "cuisine": "Wants a different cuisine than others.",
            "budget": "Different price expectation.",
            "location": "Different area or distance.",
            "dietary": "Dietary or allergy requirement conflicts with current options.",
            "other": "Some other conflict.",
        },
    },
    "next_action": {
        "type": "choice",
        "instructions": "What should the assistant do next?",
        "criteria": {
            "search": "Run or rerun the restaurant search.",
            "ask_one_question": "Ask one short clarifying question.",
            "wait": "Wait for more messages.",
            "ignore": "Ignore this message.",
        },
    },
}
_ENUMS = {"intent": INTENTS, "conflict": CONFLICTS, "next_action": NEXT_ACTIONS}


class JevDecisionProvider:
    name = "jev"

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        schema: str,
        base_url: str,
        endpoint: str,
        api_key: str,
        model: str = "",
        timeout: float = 4.0,
        min_confidence: float = 0.6,
        max_calls_per_session: int = 4,
        max_calls_per_day: int = 200,
        metrics: object | None = None,
    ) -> None:
        if schema not in ("typesafe_systemone", "jevtypesafe_decide"):
            raise ValueError("unknown Jev schema")
        if not api_key:
            raise ValueError("Jev API key required")
        self.client = client
        self.schema = schema
        self.url = base_url.rstrip("/") + "/" + endpoint.lstrip("/")
        self._api_key = api_key
        self.model = model
        self.timeout = timeout
        self.min_confidence = min_confidence
        self.max_calls_per_session = max_calls_per_session
        self.max_calls_per_day = max_calls_per_day
        self.metrics = metrics
        self.disabled_reason: str | None = None
        self._per_session: dict[str, int] = defaultdict(int)
        self._day: tuple[date, int] = (date.today(), 0)
        self.requests = 0
        self.input_tokens = 0

    def build_request(self, session_snapshot: dict, new_message: str) -> dict:
        state = _state_text(session_snapshot, new_message)
        body: dict = {"state": state, "questions": QUESTIONS}
        if self.schema == "typesafe_systemone":
            body = {"model": self.model, **body}
        return body

    def _budget_ok(self, session_id: str) -> bool:
        today, n = self._day
        if today != date.today():
            self._day = (date.today(), 0)
            n = 0
        return n < self.max_calls_per_day and self._per_session[session_id] < self.max_calls_per_session

    async def decide(self, session_snapshot: dict, new_message: str) -> Decision:
        if self.disabled_reason:
            return Decision.unknown("fallback", self.disabled_reason)
        sid = str(session_snapshot.get("session_id", ""))
        if not self._budget_ok(sid):
            return Decision.unknown("fallback", "spend_guardrail")
        self._per_session[sid] += 1
        self._day = (self._day[0], self._day[1] + 1)
        self.requests += 1
        body = self.build_request(session_snapshot, new_message)
        started = time.perf_counter()
        try:
            resp = await self.client.post(
                self.url,
                json=body,
                headers={"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"},
                timeout=self.timeout,
            )
        except httpx.TimeoutException:
            return self._fallback("timeout", started)
        except httpx.HTTPError:
            return self._fallback("network", started)
        if resp.status_code in (401, 403):
            self.disabled_reason = "auth_error"
            self._observe(started)
            log.error("jev auth failed (status=%s); Jev disabled until configuration is fixed", resp.status_code)
            raise JevAuthError("Jev rejected the API key")
        if resp.status_code == 429:
            return self._fallback("rate_limited", started)
        if resp.status_code >= 500:
            return self._fallback(f"upstream_{resp.status_code}", started)
        if resp.status_code >= 400:
            return self._fallback(f"bad_request_{resp.status_code}", started)
        try:
            data = resp.json()
        except ValueError:
            return self._fallback("invalid_json", started)
        decision = self.parse_response(data)
        self._observe(started, data)
        return decision

    def parse_response(self, data: object) -> Decision:
        if not isinstance(data, dict) or not isinstance(data.get("answers"), dict):
            return Decision.unknown("fallback", "invalid_shape")
        answers = data["answers"]
        picked: dict[str, tuple[str, float]] = {}
        for qid, allowed in _ENUMS.items():
            a = answers.get(qid)
            if not isinstance(a, dict):
                return Decision.unknown("fallback", f"missing_{qid}")
            choice = a.get("choice")
            if choice not in allowed:
                return Decision.unknown("fallback", f"unknown_label_{qid}")
            conf = a.get("confidence")
            if not isinstance(conf, (int, float)):
                probs = a.get("probabilities") or {}
                conf = probs.get(choice) if isinstance(probs, dict) else None
            if not isinstance(conf, (int, float)) or not 0 <= float(conf) <= 1:
                return Decision.unknown("fallback", f"bad_confidence_{qid}")
            picked[qid] = (choice, float(conf))
        confidence = min(c for _, c in picked.values())
        if confidence < self.min_confidence:
            return Decision("clarify", picked["conflict"][0], "ask_one_question", confidence, "jev", "low_confidence")
        return Decision(picked["intent"][0], picked["conflict"][0], picked["next_action"][0], confidence, "jev")

    def _fallback(self, reason: str, started: float) -> Decision:
        self._observe(started)
        log.warning("jev fallback reason=%s", reason)
        return Decision.unknown("fallback", reason)

    def _observe(self, started: float, data: dict | None = None) -> None:
        elapsed = time.perf_counter() - started
        tokens = 0
        if isinstance(data, dict) and isinstance(data.get("usage"), dict):
            tokens = int(data["usage"].get("input_tokens") or 0)
            self.input_tokens += tokens
        if self.metrics is not None:
            self.metrics.observe("jev", elapsed)  # type: ignore[attr-defined]
        log.info("jev call elapsed_ms=%.0f input_tokens=%d", elapsed * 1000, tokens)


def _state_text(snapshot: dict, message: str) -> str:
    """Minimal context: structured session facts + one redacted message. No names/history."""
    parts = [f"Restaurant planning session status: {snapshot.get('status', 'unknown')}."]
    if snapshot.get("has_location"):
        parts.append("Search area is set.")
    if snapshot.get("cuisines"):
        parts.append("Requested cuisines: " + ", ".join(sorted(snapshot["cuisines"]))[:120] + ".")
    if snapshot.get("has_shortlist"):
        parts.append("A shortlist of options has been shown.")
    parts.append(f'New message: "{redact(message)}"')
    return " ".join(parts)
