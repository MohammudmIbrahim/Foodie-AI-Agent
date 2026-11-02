"""Explicit, deterministic parsers for amounts, places, cuisines, diets and times.

Jev (when enabled) only classifies intent; entities always come from these parsers. When a
parser is unsure it returns nothing and the bot asks rather than guessing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from urllib.parse import parse_qs, unquote, urlparse

from app.domain.models import Constraint, ConstraintKind, GeoPoint

# User word -> canonical cuisine.
CUISINE_WORDS: dict[str, str] = {
    "mexican": "mexican", "tacos": "mexican", "taco": "mexican", "burrito": "mexican",
    "burritos": "mexican", "tex-mex": "mexican",
    "sushi": "sushi", "japanese": "japanese", "ramen": "ramen",
    "burger": "burger", "burgers": "burger", "hamburgers": "burger",
    "pizza": "pizza", "chinese": "chinese", "thai": "thai", "indian": "indian", "curry": "indian",
    "italian": "italian", "pasta": "italian",
    "chicken": "chicken", "fried chicken": "chicken", "wings": "wings", "wing": "wings",
    "bbq": "bbq", "barbecue": "bbq", "korean": "korean", "vietnamese": "vietnamese", "pho": "vietnamese",
    "mediterranean": "mediterranean", "greek": "greek", "middle eastern": "mediterranean",
    "falafel": "mediterranean", "kebab": "mediterranean", "shawarma": "mediterranean",
    "seafood": "seafood", "fish": "seafood", "steak": "steak", "steakhouse": "steak",
    "sandwich": "sandwich", "sandwiches": "sandwich", "subs": "sandwich", "deli": "sandwich",
    "salad": "salad", "salads": "salad", "breakfast": "breakfast", "brunch": "breakfast",
    "poke": "poke", "hawaiian": "poke", "american": "american", "diner": "american",
}

# Canonical cuisine -> OSM `cuisine=*` values that satisfy it.
CUISINE_TAGS: dict[str, frozenset[str]] = {
    "mexican": frozenset({"mexican", "tex-mex", "tacos", "taco", "burrito", "latin_american"}),
    "sushi": frozenset({"sushi"}),
    "japanese": frozenset({"japanese", "sushi", "ramen"}),
    "ramen": frozenset({"ramen", "noodle"}),
    "burger": frozenset({"burger"}),
    "pizza": frozenset({"pizza"}),
    "chinese": frozenset({"chinese", "dim_sum", "cantonese", "szechuan"}),
    "thai": frozenset({"thai"}),
    "indian": frozenset({"indian", "curry", "pakistani", "nepalese"}),
    "italian": frozenset({"italian", "pasta", "italian_pizza"}),
    "chicken": frozenset({"chicken", "fried_chicken", "wings"}),
    "wings": frozenset({"wings", "chicken_wings"}),
    "bbq": frozenset({"bbq", "barbecue"}),
    "korean": frozenset({"korean"}),
    "vietnamese": frozenset({"vietnamese", "pho"}),
    "mediterranean": frozenset({"mediterranean", "greek", "lebanese", "middle_eastern", "falafel", "kebab", "turkish", "persian"}),
    "greek": frozenset({"greek"}),
    "seafood": frozenset({"seafood", "fish", "fish_and_chips"}),
    "steak": frozenset({"steak_house", "steak"}),
    "sandwich": frozenset({"sandwich", "deli", "sub", "subs"}),
    "salad": frozenset({"salad"}),
    "breakfast": frozenset({"breakfast", "brunch", "pancake"}),
    "poke": frozenset({"poke", "hawaiian"}),
    "american": frozenset({"american", "diner", "burger"}),
}

# Avoidance is conservative: "no seafood" also excludes sushi/poke.
AVOID_TAGS: dict[str, frozenset[str]] = {
    **CUISINE_TAGS,
    "seafood": frozenset({"seafood", "fish", "fish_and_chips", "sushi", "poke", "crab", "oyster"}),
}

DIETARY_WORDS = {
    "halal": "halal", "kosher": "kosher", "vegetarian": "vegetarian", "veggie": "vegetarian",
    "vegan": "vegan", "plant based": "vegan", "plant-based": "vegan",
    "gluten free": "gluten_free", "gluten-free": "gluten_free", "celiac": "gluten_free", "coeliac": "gluten_free",
}

ALLERGENS = {
    "peanut": "peanut", "peanuts": "peanut", "nut": "tree_nut", "nuts": "tree_nut", "tree nut": "tree_nut",
    "shellfish": "shellfish", "shrimp": "shellfish", "dairy": "dairy", "milk": "dairy", "lactose": "dairy",
    "egg": "egg", "eggs": "egg", "soy": "soy", "wheat": "wheat", "gluten": "wheat", "sesame": "sesame",
    "fish": "fish",
}

NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "half a": 0.5, "a": 1,
}

_HARD_BUDGET = re.compile(r"\b(hard|max|maximum|cap|strict|at most|no more than|limit|can'?t (go|spend) over|must)\b")
_STRICT_RADIUS = re.compile(r"\b(max|maximum|within|at most|no more than|strict|no farther|no further)\b")
_CORRECTION = re.compile(r"\b(actually|instead|switch(ing)? to|change (it )?to|rather|scratch that|make it)\b")
_GROUP_CLAIM = re.compile(r"\b(we all|everyone|all of us|the whole group|we('re| are) all)\b")
_ADDITIVE = re.compile(r"\b(also|too|as well|or)\b")
_LOCATION_STOP = re.compile(
    r"\s+\b(under|for|with|within|tonight|today|tomorrow|at|by|and|but|please|around|max|that|who|which)\b.*$",
    re.IGNORECASE,
)
_LOC_CAP = re.compile(r"\b(?:near|in|around|by|close to)\s+((?:[A-Z][\w'.\-]*)(?:[ ,]+(?:[A-Z][\w'.\-]*|[A-Z]{2}))*)")
_LOC_NEAR = re.compile(r"\bnear\s+([a-z][\w'.\-]*(?:\s+[a-z][\w'.\-]*){0,2})", re.IGNORECASE)
_LOC_STOPWORDS = frozenset({"me", "us", "here", "there", "my", "our", "the", "a", "campus", "home", "work", "you"})
_MAPS_HOSTS = ("google.com", "www.google.com", "maps.google.com", "maps.app.goo.gl", "goo.gl", "maps.apple.com")


@dataclass
class Extraction:
    constraints: list[Constraint] = field(default_factory=list)
    location_text: str | None = None
    location_point: GeoPoint | None = None
    correction: bool = False
    group_claim: bool = False
    additive: bool = False

    @property
    def empty(self) -> bool:
        return not self.constraints and not self.location_text and not self.location_point


def _word_hits(text: str, words: dict[str, str]) -> list[str]:
    hits: list[str] = []
    for word in sorted(words, key=len, reverse=True):
        if re.search(rf"(?<![\w-]){re.escape(word)}(?![\w-])", text):
            canon = words[word]
            if canon not in hits:
                hits.append(canon)
            text = re.sub(rf"(?<![\w-]){re.escape(word)}(?![\w-])", " ", text)
    return hits


def parse_budget(text: str) -> tuple[float, bool] | None:
    low = text.lower()
    m = re.search(r"\$\s?(\d{1,3}(?:\.\d{1,2})?)", low) or re.search(r"\b(\d{1,3})\s*(?:dollars|bucks|usd)\b", low)
    if not m:
        return None
    amount = float(m.group(1))
    if not 1 <= amount <= 500:
        return None
    return amount, bool(_HARD_BUDGET.search(low))


def parse_radius(text: str, max_radius: float) -> tuple[float, bool] | None:
    low = text.lower()
    num = r"(\d{1,2}(?:\.\d)?|half a|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|a)"
    m = re.search(rf"\b{num}\s*(?:-\s*)?(?:mi|mile|miles)\b", low)
    if not m:
        return None
    raw = m.group(1)
    value = float(NUMBER_WORDS.get(raw, raw) if raw in NUMBER_WORDS else raw)
    if value <= 0:
        return None
    return min(value, max_radius), bool(_STRICT_RADIUS.search(low))


def parse_arrival(text: str, now: datetime) -> datetime | None:
    low = text.lower()
    m = re.search(r"\b(?:at|by|around|for)\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", low)
    if not m:
        m = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", low)
    if not m:
        return None
    hour, minute, ampm = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if not (1 <= hour <= 12 and 0 <= minute < 60):
        return None
    hour = hour % 12 + (12 if ampm == "pm" else 0)
    when = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if when < now - timedelta(hours=1):
        when += timedelta(days=1)
    return when


def parse_maps_link(text: str) -> GeoPoint | None:
    """Accept a voluntarily shared Google/Apple Maps link that contains explicit coordinates."""
    m = re.search(r"https?://\S+", text)
    if not m:
        return None
    url = m.group(0).rstrip(").,!")
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    if parsed.scheme != "https" or (parsed.hostname or "") not in _MAPS_HOSTS:
        return None
    candidates: list[str] = []
    at = re.search(r"@(-?\d{1,2}\.\d+),(-?\d{1,3}\.\d+)", unquote(parsed.path))
    if at:
        candidates.append(f"{at.group(1)},{at.group(2)}")
    qs = parse_qs(parsed.query)
    for key in ("query", "q", "ll", "destination", "sll"):
        candidates.extend(qs.get(key, []))
    for cand in candidates:
        cm = re.fullmatch(r"\s*(-?\d{1,2}(?:\.\d+)?)\s*,\s*(-?\d{1,3}(?:\.\d+)?)\s*", cand)
        if cm:
            try:
                return GeoPoint(float(cm.group(1)), float(cm.group(2)), "shared map pin")
            except ValueError:
                return None
    return None


def parse_location_text(text: str) -> str | None:
    for pattern in (_LOC_CAP, _LOC_NEAR):
        m = pattern.search(text)
        if not m:
            continue
        loc = _LOCATION_STOP.sub("", m.group(1)).strip(" ,.!?")
        words = loc.lower().split()
        if not words or words[0] in _LOC_STOPWORDS:
            continue
        if any(w in CUISINE_WORDS or w in DIETARY_WORDS for w in words):
            continue
        if 2 <= len(loc) <= 60:
            return loc
    return None


def parse_avoid(low: str) -> list[str]:
    out: list[str] = []
    for m in re.finditer(r"\b(?:no|not|avoid|without|anything but|nothing)\s+([a-z\- ]{3,20})", low):
        phrase = m.group(1)
        for word in sorted(CUISINE_WORDS, key=len, reverse=True):
            if re.match(rf"{re.escape(word)}\b", phrase):
                canon = CUISINE_WORDS[word]
                if canon not in out:
                    out.append(canon)
                break
    return out


def parse_allergies(low: str) -> list[str]:
    out: list[str] = []
    for m in re.finditer(r"\b([a-z]+(?: nut)?)\s+allerg(?:y|ies|ic)\b|\ballergic to\s+([a-z]+(?: nuts?)?)", low):
        word = (m.group(1) or m.group(2) or "").strip()
        canon = ALLERGENS.get(word) or ALLERGENS.get(word.rstrip("s"))
        if canon and canon not in out:
            out.append(canon)
    return out


def extract(text: str, *, now: datetime, max_radius: float) -> Extraction:
    """Extract structured constraints from one message. Unknown words are ignored, not guessed."""
    low = text.lower()
    ex = Extraction(
        correction=bool(_CORRECTION.search(low)),
        group_claim=bool(_GROUP_CLAIM.search(low)),
        additive=bool(_ADDITIVE.search(low)),
    )
    allergies = parse_allergies(low)
    for a in allergies:
        ex.constraints.append(Constraint(ConstraintKind.ALLERGY, a, hard=True))

    avoid = parse_avoid(low)
    for a in avoid:
        ex.constraints.append(Constraint(ConstraintKind.AVOID, a, hard=True))

    # Remove allergen/avoid phrases before looking for positive cuisines ("no seafood" != seafood).
    scrubbed = re.sub(r"\b(?:no|not|avoid|without|anything but|nothing)\s+[a-z\-]+(?: [a-z]+)?", " ", low)
    scrubbed = re.sub(r"\b[a-z]+(?: nut)?\s+allerg(?:y|ies|ic)\b|\ballergic to\s+[a-z]+", " ", scrubbed)
    for diet in _word_hits(scrubbed, DIETARY_WORDS):
        ex.constraints.append(Constraint(ConstraintKind.DIETARY, diet, hard=True))
    for cuisine in _word_hits(scrubbed, CUISINE_WORDS):
        if cuisine not in avoid:
            ex.constraints.append(Constraint(ConstraintKind.CUISINE, cuisine, hard=False))

    budget = parse_budget(text)
    if budget:
        ex.constraints.append(Constraint(ConstraintKind.BUDGET, f"{budget[0]:g}", hard=budget[1]))
    radius = parse_radius(text, max_radius)
    if radius:
        ex.constraints.append(Constraint(ConstraintKind.RADIUS, f"{radius[0]:g}", hard=radius[1]))
    arrival = parse_arrival(text, now)
    if arrival:
        ex.constraints.append(Constraint(ConstraintKind.ARRIVAL, arrival.isoformat(), hard=False))

    ex.location_point = parse_maps_link(text)
    if not ex.location_point:
        ex.location_text = parse_location_text(re.sub(r"https?://\S+", " ", text))
    return ex


_FOOD_HINTS = re.compile(
    r"\b(food|eat|eating|dinner|lunch|breakfast|brunch|hungry|restaurant|place|spot|menu|cheap|"
    r"expensive|close|far|drive|walk|price|budget|spicy|healthy|lighter|heavier|craving|order|"
    r"option|options|pick|choose|tonight|that one|not that|different)\b"
)


def looks_food_related(text: str) -> bool:
    """Cheap local relevance hint used to decide whether an ambiguous message is worth Jev."""
    return bool(_FOOD_HINTS.search(text.lower()))
