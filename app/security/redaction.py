"""Identity hashing and text redaction. Raw phone numbers/emails never reach the DB or logs."""

from __future__ import annotations

import hashlib
import hmac
import re

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_PHONE = re.compile(r"(?<!\w)\+?\d[\d\s().-]{7,}\d(?!\w)")
_URL = re.compile(r"https?://\S+")


def normalize_sender(sender: str) -> str:
    s = (sender or "").strip().lower()
    if _EMAIL.fullmatch(s):
        return s
    digits = re.sub(r"\D", "", s)
    if len(digits) >= 7:
        return "+" + digits
    return s


def hash_sender(pepper: str, sender: str) -> str:
    """Stable pseudonymous member identifier (HMAC-SHA256 with a secret pepper)."""
    return hmac.new(pepper.encode(), normalize_sender(sender).encode(), hashlib.sha256).hexdigest()[:32]


def short_id(value: str) -> str:
    """Log-safe identifier for chat GUIDs and similar."""
    return hashlib.sha256(value.encode()).hexdigest()[:10]


def redact(text: str, max_len: int = 280) -> str:
    """Strip emails, phone numbers and URLs before any external call; bound length."""
    out = _EMAIL.sub("[email]", text or "")
    out = _PHONE.sub("[phone]", out)
    out = _URL.sub("[link]", out)
    return out[:max_len]
