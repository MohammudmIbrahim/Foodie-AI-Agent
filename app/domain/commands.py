"""Exact wake-word detection and high-priority command routing (deterministic, local)."""

from __future__ import annotations

import re
from dataclasses import dataclass

UNIVERSAL_NAMES = ("foodie ai", "foodie")
RESERVED_WORDS = frozenset(
    {
        "stop", "help", "vote", "setup", "privacy", "forget", "confirm", "agree", "decline",
        "more", "closer", "directions", "reviews", "remember", "name", "style", "city",
        "radius", "go", "search", "reopen", "erase", "everyone", "all", "here", "ai",
    }
)

# Order matters: first match wins. Patterns run on the lower-cased body after the wake word.
_COMMANDS: list[tuple[str, re.Pattern[str]]] = [
    (name, re.compile(pattern))
    for name, pattern in [
        ("stop", r"^(stop|cancel|never ?mind|nevermind)\b"),
        ("forget_me", r"^forget me\b"),
        ("erase_group", r"^erase (the )?group\b"),
        ("privacy", r"^privacy\b"),
        ("help", r"^(help|commands)\s*[?.!]*$"),
        ("setup", r"^set ?up\b"),
        ("agree", r"^(i )?(agree|opt in|consent)\b"),
        ("decline", r"^(i )?(decline|opt out)\b"),
        ("vote", r"^vote\s+(?:for\s+)?(?:option\s+)?#?(\d{1,2})\s*$"),
        ("vote_open", r"^(vote|start (a )?vote|let'?s vote)\s*$"),
        ("close", r"^(close|end|finish) (the )?(vote|poll|polls|voting)\b|^finali[sz]e\b"),
        ("more", r"^more\b"),
        ("closer", r"^closer\b"),
        ("directions", r"^directions?\s+(?:to\s+)?#?(\d{1,2})\b"),
        ("reviews", r"^reviews?\s+(?:for\s+)?#?(\d{1,2})\b"),
        ("confirm_remember", r"^confirm remember\b"),
        ("remember", r"^remember\s+(?:that\s+)?(.+)$"),
        ("confirm", r"^(confirm|confirmed|yes,? save( it)?)\b"),
        ("name", r"^name\s+(.+)$"),
        ("style", r"^style\s+(.+)$"),
        ("city", r"^city\s+(.+)$"),
        ("radius", r"^radius\s+(\d{1,2}(?:\.\d)?)\b"),
        ("skip_city", r"^skip city\b"),
        ("go", r"^(go|search now|search|just search|go ahead)\s*[.!]*$"),
        ("drop", r"^drop my (\w+)"),
        ("reopen", r"^reopen\b"),
        ("rename", r"^(rename|new name|change (your )?name|vote on (a )?(new )?name)\b"),
        (
            "restyle",
            r"^(change (your )?(style|personality)|vote on (a )?(new )?(style|personality))\b"
            r"|^you'?re too (formal|stiff|casual|wordy|chatty|serious|silly)\b",
        ),
    ]
]


@dataclass(frozen=True)
class Routed:
    woke: bool
    command: str | None
    arg: str
    body: str


def _wake_pattern(names: tuple[str, ...]) -> re.Pattern[str]:
    alts = "|".join(re.escape(n) for n in sorted({n.lower() for n in names if n}, key=len, reverse=True))
    return re.compile(rf"^\s*@?(?:{alts})(?![\w])[\s,:;!.\-]*", re.IGNORECASE)


def route(text: str, agent_name: str | None = None) -> Routed:
    """Detect the wake word at the start of a message and classify an exact command.

    `Foodie AI` / `Foodie` always work as a universal fallback, alongside the group nickname.
    """
    names = UNIVERSAL_NAMES + ((agent_name,) if agent_name else ())
    m = _wake_pattern(names).match(text or "")
    if not m:
        return Routed(woke=False, command=None, arg="", body=(text or "").strip())
    body = text[m.end():].strip()
    lowered = re.sub(r"\s+", " ", body.lower()).strip()
    for name, pat in _COMMANDS:
        cm = pat.search(lowered)
        if cm:
            arg = cm.group(1) if cm.groups() and name in _ARG_COMMANDS else ""
            if name in _RAW_ARG_COMMANDS:
                # Preserve original casing for free-text args (names, cities).
                arg = _raw_tail(body, cm.group(1))
            return Routed(woke=True, command=name, arg=(arg or "").strip(), body=body)
    return Routed(woke=True, command=None, arg="", body=body)


_ARG_COMMANDS = frozenset({"vote", "directions", "reviews", "radius", "drop", "remember", "name", "style", "city"})
_RAW_ARG_COMMANDS = frozenset({"remember", "name", "style", "city"})


def _raw_tail(body: str, lowered_arg: str) -> str:
    idx = body.lower().rfind(lowered_arg)
    return body[idx:] if idx >= 0 else lowered_arg


_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z ]{0,18}[A-Za-z]$|^[A-Za-z]{2}$")


def sanitize_nickname(raw: str) -> str | None:
    """Return a safe nickname or None. Letters/spaces, 2-20 chars, not a command word."""
    name = re.sub(r"\s+", " ", (raw or "").strip().strip(".!?\"'"))
    if not _NAME_RE.match(name):
        return None
    if any(w in RESERVED_WORDS for w in name.lower().split()):
        return None
    return name.title() if name.islower() else name


PERSONALITY_PRESETS = ("adaptive", "chill", "funny", "direct")
_URL_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
_DANGEROUS = re.compile(
    r"ignore|instruction|system|prompt|privacy|password|forget|data|delete|pretend|human|"
    r"jailbreak|override|rule|secret|reveal|token|admin",
    re.IGNORECASE,
)


def sanitize_personality(raw: str) -> str | None:
    """Presets or a short style-only custom description. Rejects anything policy-like."""
    text = re.sub(r"\s+", " ", (raw or "").strip()).strip(".!")
    low = text.lower()
    if low in PERSONALITY_PRESETS:
        return low
    if low.startswith("custom"):
        text = text[6:].lstrip(" :-")
    if not text or len(text) > 60 or _URL_RE.search(text) or _DANGEROUS.search(text):
        return None
    if not re.fullmatch(r"[A-Za-z ,'\-]+", text):
        return None
    return f"custom:{text.lower()}"
