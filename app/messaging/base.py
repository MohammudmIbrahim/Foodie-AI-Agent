"""Messaging provider interface."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.domain.models import InboundMessage


@dataclass(frozen=True)
class SendResult:
    ok: bool
    provider_message_id: str | None = None
    # True when the request may have been delivered but no confirmation arrived.
    uncertain: bool = False
    # True when the request definitely did not leave (safe to retry, bounded).
    retryable: bool = False


class MessagingProvider(Protocol):
    name: str

    async def send(self, chat_guid: str, text: str, idempotency_key: str) -> SendResult: ...

    def normalize(self, payload: dict) -> InboundMessage | None:
        """Convert a provider webhook payload to an InboundMessage (or None to ignore)."""
        ...
