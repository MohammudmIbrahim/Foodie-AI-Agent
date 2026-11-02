"""In-memory messaging provider for tests and the offline demo. Sends nothing anywhere."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.domain.models import InboundMessage
from app.messaging.base import SendResult


@dataclass
class SentMessage:
    chat_guid: str
    text: str
    idempotency_key: str


@dataclass
class MockMessagingProvider:
    name: str = "mock"
    sent: list[SentMessage] = field(default_factory=list)
    fail_mode: str | None = None  # None | "uncertain" | "retryable"

    async def send(self, chat_guid: str, text: str, idempotency_key: str) -> SendResult:
        if self.fail_mode == "uncertain":
            return SendResult(ok=False, uncertain=True)
        if self.fail_mode == "retryable":
            return SendResult(ok=False, retryable=True)
        if any(m.idempotency_key == idempotency_key for m in self.sent):
            return SendResult(ok=True, provider_message_id=f"dup-{idempotency_key}")
        self.sent.append(SentMessage(chat_guid, text, idempotency_key))
        return SendResult(ok=True, provider_message_id=f"mock-{uuid.uuid4().hex[:8]}")

    def normalize(self, payload: dict) -> InboundMessage | None:
        try:
            return InboundMessage(
                event_id=str(payload["event_id"]),
                chat_guid=str(payload["chat_guid"]),
                sender=str(payload["sender"]),
                text=str(payload.get("text") or ""),
                is_from_me=bool(payload.get("is_from_me", False)),
                timestamp=datetime.now(timezone.utc),
            )
        except KeyError:
            return None

    def texts(self, chat_guid: str | None = None) -> list[str]:
        return [m.text for m in self.sent if chat_guid is None or m.chat_guid == chat_guid]
