"""BlueBubbles Server adapter (public REST + webhooks only; no Private API, no SIP changes).

Contract sources (verify against YOUR installed server version before going live):
- Send text: POST {base}/api/v1/message/text?password=<server password>
  JSON {"chatGuid", "tempGuid", "message", "method": "apple-script"}; tempGuid de-duplicates
  sends (BlueBubbles Postman collection, "Message > Send Text").
- Webhook: POST of {"type": "new-message", "data": <Message>} where Message carries guid, text,
  isFromMe, handle.address, chats[].guid, dateCreated (ms). Other event types are ignored.

STATUS: implemented and unit-tested against mocked HTTP only. Not verified with a live server.
"""

from __future__ import annotations

from datetime import datetime, timezone

import httpx

from app.domain.models import InboundMessage
from app.messaging.base import SendResult


class BlueBubblesProvider:
    name = "bluebubbles"

    def __init__(self, client: httpx.AsyncClient, base_url: str, password: str, method: str = "apple-script", timeout: float = 10.0) -> None:
        self.client = client
        self.base_url = base_url.rstrip("/")
        self._password = password
        self.method = method
        self.timeout = timeout

    async def send(self, chat_guid: str, text: str, idempotency_key: str) -> SendResult:
        try:
            resp = await self.client.post(
                f"{self.base_url}/api/v1/message/text",
                params={"password": self._password},
                json={"chatGuid": chat_guid, "tempGuid": idempotency_key, "message": text, "method": self.method},
                timeout=self.timeout,
            )
        except (httpx.ConnectError, httpx.ConnectTimeout):
            return SendResult(ok=False, retryable=True)  # never reached the server
        except httpx.HTTPError:
            return SendResult(ok=False, uncertain=True)  # may have been sent; do not resend blindly
        if resp.status_code >= 500:
            return SendResult(ok=False, uncertain=True)
        if resp.status_code >= 400:
            return SendResult(ok=False)
        try:
            body = resp.json()
            guid = (body.get("data") or {}).get("guid")
        except (ValueError, AttributeError):
            guid = None
        return SendResult(ok=True, provider_message_id=guid)

    def normalize(self, payload: dict) -> InboundMessage | None:
        if not isinstance(payload, dict) or payload.get("type") != "new-message":
            return None
        data = payload.get("data")
        if not isinstance(data, dict):
            return None
        guid = data.get("guid")
        chats = data.get("chats") or []
        chat_guid = chats[0].get("guid") if chats and isinstance(chats[0], dict) else None
        handle = data.get("handle") or {}
        sender = handle.get("address") if isinstance(handle, dict) else None
        is_from_me = bool(data.get("isFromMe"))
        if not guid or not chat_guid or (not sender and not is_from_me):
            return None
        created = data.get("dateCreated")
        try:
            ts = datetime.fromtimestamp(int(created) / 1000, tz=timezone.utc)
        except (TypeError, ValueError, OSError):
            ts = datetime.now(timezone.utc)
        return InboundMessage(
            event_id=f"bb:{guid}",
            chat_guid=str(chat_guid),
            sender=str(sender or ""),
            text=str(data.get("text") or ""),
            is_from_me=is_from_me,
            timestamp=ts,
        )
