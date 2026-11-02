"""Allowlist and authorization checks. Cheap, local, and run before anything else."""

from __future__ import annotations

from app.config import Settings
from app.security.redaction import normalize_sender


def chat_allowed(settings: Settings, chat_guid: str) -> bool:
    return chat_guid in settings.allowed_chats


def is_setup_sender(settings: Settings, sender: str) -> bool:
    return normalize_sender(sender) in {normalize_sender(s) for s in settings.setup_sender_set}
