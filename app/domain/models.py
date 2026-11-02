"""Typed domain entities and enums. No I/O here."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class SessionStatus(str, Enum):
    COLLECTING = "collecting"
    SEARCHING = "searching"
    SHORTLIST = "shortlist"
    VOTING = "voting"
    DONE = "done"
    STOPPED = "stopped"
    EXPIRED = "expired"


ACTIVE_STATUSES = (
    SessionStatus.COLLECTING,
    SessionStatus.SEARCHING,
    SessionStatus.SHORTLIST,
    SessionStatus.VOTING,
)


class OnboardingState(str, Enum):
    NONE = "none"
    CONSENT = "consent"
    POLLS = "polls"
    CITY = "city"
    COMPLETE = "complete"
    DECLINED = "declined"


class ConstraintKind(str, Enum):
    CUISINE = "cuisine"
    AVOID = "avoid"
    DIETARY = "dietary"
    ALLERGY = "allergy"
    BUDGET = "budget"
    RADIUS = "radius"
    LOCATION = "location"
    ARRIVAL = "arrival"


# Kinds that are always treated as hard (safety / explicit requirement).
ALWAYS_HARD = frozenset({ConstraintKind.DIETARY, ConstraintKind.ALLERGY})


class Evidence(str, Enum):
    """Whether provider data supports a requirement. UNKNOWN is never a pass."""

    YES = "yes"
    NO = "no"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class InboundMessage:
    event_id: str
    chat_guid: str
    sender: str
    text: str
    is_from_me: bool
    timestamp: datetime


@dataclass(frozen=True)
class GeoPoint:
    lat: float
    lon: float
    label: str

    def __post_init__(self) -> None:
        if not (-90 <= self.lat <= 90 and -180 <= self.lon <= 180):
            raise ValueError("coordinates out of range")


@dataclass(frozen=True)
class Constraint:
    kind: ConstraintKind
    value: str
    hard: bool
    member_id: str = ""


@dataclass
class Place:
    """Normalized provider place. Optional fields stay None when the provider lacks them."""

    provider: str
    provider_place_id: str
    name: str
    latitude: float
    longitude: float
    maps_url: str
    last_fetched_at: str
    address: str | None = None
    cuisine_tags: list[str] = field(default_factory=list)
    rating: float | None = None
    review_count: int | None = None
    price_level: int | None = None
    opening_hours: str | None = None
    dietary_evidence: dict[str, Evidence] = field(default_factory=dict)
    source_url: str | None = None
    amenity: str | None = None
    website: str | None = None

    def __post_init__(self) -> None:
        if not self.name or not self.name.strip():
            raise ValueError("place requires a real name")
        if not (-90 <= self.latitude <= 90 and -180 <= self.longitude <= 180):
            raise ValueError("place requires real coordinates")

    def to_dict(self) -> dict:
        d = self.__dict__.copy()
        d["dietary_evidence"] = {k: v.value for k, v in self.dietary_evidence.items()}
        return d

    @classmethod
    def from_dict(cls, d: dict) -> Place:
        d = dict(d)
        d["dietary_evidence"] = {k: Evidence(v) for k, v in d.get("dietary_evidence", {}).items()}
        return cls(**d)


@dataclass
class RankedPlace:
    place: Place
    score: float
    distance_miles: float
    confidence: float
    unverified: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    hours_note: str = "hours not confirmed"
    open_at_arrival: bool | None = None
    matched_cuisines: list[str] = field(default_factory=list)


@dataclass
class SearchPlan:
    """Merged, conflict-resolved requirements for one session version."""

    location: GeoPoint | None
    radius_miles: float
    radius_strict: bool
    cuisines_by_member: dict[str, set[str]]
    dietary: set[str]
    allergies: set[str]
    avoid: set[str]
    budget_hard: float | None
    budget_soft: float | None
    arrival: datetime | None
    conflict: str = "none"

    @property
    def all_cuisines(self) -> set[str]:
        out: set[str] = set()
        for c in self.cuisines_by_member.values():
            out |= c
        return out
