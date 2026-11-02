"""Merge per-member requirements. Members own their constraints; hard needs are never
overwritten by someone else, a vote, or an AI classification."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from app.domain.models import Constraint, ConstraintKind, GeoPoint, SearchPlan
from app.domain.parsing import AVOID_TAGS, CUISINE_TAGS, Extraction

_REPLACE_OWN = (ConstraintKind.BUDGET, ConstraintKind.RADIUS)
_ADD_ONLY = (ConstraintKind.AVOID, ConstraintKind.DIETARY, ConstraintKind.ALLERGY)


@dataclass
class StoredConstraint:
    id: str
    constraint: Constraint


@dataclass
class ConstraintChanges:
    add: list[Constraint] = field(default_factory=list)
    remove_ids: list[str] = field(default_factory=list)
    protected: list[StoredConstraint] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.add or self.remove_ids)


def plan_changes(existing: list[StoredConstraint], ex: Extraction, member_id: str) -> ConstraintChanges:
    """Decide how one member's message changes the session's constraints."""
    ch = ConstraintChanges()
    current = {(s.constraint.member_id, s.constraint.kind, s.constraint.value) for s in existing}

    new_by_kind: dict[ConstraintKind, list[Constraint]] = {}
    for c in ex.constraints:
        new_by_kind.setdefault(c.kind, []).append(
            Constraint(c.kind, c.value, c.hard, member_id=member_id)
        )

    cuisines = new_by_kind.get(ConstraintKind.CUISINE, [])
    if cuisines:
        for s in existing:
            c = s.constraint
            if c.kind != ConstraintKind.CUISINE:
                continue
            mine = c.member_id == member_id
            # Cuisine is a soft preference: a correction or "we all want" replaces it for everyone.
            if (ex.correction or ex.group_claim) or (mine and not ex.additive):
                ch.remove_ids.append(s.id)
        removed = set(ch.remove_ids)
        keep = {(s.constraint.member_id, s.constraint.value) for s in existing if s.id not in removed and s.constraint.kind == ConstraintKind.CUISINE}
        ch.add.extend(c for c in cuisines if (member_id, c.value) not in keep)

    for kind in _REPLACE_OWN:
        for new in new_by_kind.get(kind, []):
            for s in existing:
                c = s.constraint
                if c.kind != kind:
                    continue
                if c.member_id == member_id:
                    ch.remove_ids.append(s.id)
                elif ex.group_claim:
                    if c.hard:
                        ch.protected.append(s)  # needs that member's own confirmation
                    else:
                        ch.remove_ids.append(s.id)
            ch.add.append(new)

    for new in new_by_kind.get(ConstraintKind.ARRIVAL, []):
        ch.remove_ids.extend(s.id for s in existing if s.constraint.kind == ConstraintKind.ARRIVAL)
        ch.add.append(new)

    for kind in _ADD_ONLY:
        for new in new_by_kind.get(kind, []):
            if (member_id, kind, new.value) not in current:
                ch.add.append(new)

    ch.remove_ids = list(dict.fromkeys(ch.remove_ids))
    return ch


def drop_kind(existing: list[StoredConstraint], member_id: str, kind_word: str) -> list[str]:
    """A member removes their own constraints of one kind (`Foodie drop my budget`)."""
    aliases = {
        "budget": ConstraintKind.BUDGET, "price": ConstraintKind.BUDGET,
        "radius": ConstraintKind.RADIUS, "distance": ConstraintKind.RADIUS,
        "cuisine": ConstraintKind.CUISINE, "food": ConstraintKind.CUISINE,
        "diet": ConstraintKind.DIETARY, "dietary": ConstraintKind.DIETARY,
        "allergy": ConstraintKind.ALLERGY, "allergies": ConstraintKind.ALLERGY,
        "avoid": ConstraintKind.AVOID, "requirements": None, "needs": None,
    }
    if kind_word not in aliases:
        return []
    kind = aliases[kind_word]
    return [
        s.id for s in existing
        if s.constraint.member_id == member_id and (kind is None or s.constraint.kind == kind)
    ]


def build_plan(
    constraints: list[Constraint],
    *,
    location: GeoPoint | None,
    default_radius: float,
    max_radius: float,
) -> SearchPlan:
    """Constraints must be ordered oldest-first so later soft values win."""
    strict_radii = [float(c.value) for c in constraints if c.kind == ConstraintKind.RADIUS and c.hard]
    soft_radii = [float(c.value) for c in constraints if c.kind == ConstraintKind.RADIUS and not c.hard]
    if strict_radii:
        radius, strict = min(strict_radii), True
    elif soft_radii:
        radius, strict = soft_radii[-1], False
    else:
        radius, strict = default_radius, False
    radius = min(radius, max_radius)

    hard_budgets = [float(c.value) for c in constraints if c.kind == ConstraintKind.BUDGET and c.hard]
    soft_budgets = [float(c.value) for c in constraints if c.kind == ConstraintKind.BUDGET and not c.hard]

    cuisines: dict[str, set[str]] = {}
    for c in constraints:
        if c.kind == ConstraintKind.CUISINE:
            cuisines.setdefault(c.member_id, set()).add(c.value)

    arrivals = [c.value for c in constraints if c.kind == ConstraintKind.ARRIVAL]
    plan = SearchPlan(
        location=location,
        radius_miles=radius,
        radius_strict=strict,
        cuisines_by_member=cuisines,
        dietary={c.value for c in constraints if c.kind == ConstraintKind.DIETARY},
        allergies={c.value for c in constraints if c.kind == ConstraintKind.ALLERGY},
        avoid={c.value for c in constraints if c.kind == ConstraintKind.AVOID},
        budget_hard=min(hard_budgets) if hard_budgets else None,
        budget_soft=min(soft_budgets) if soft_budgets else None,
        arrival=datetime.fromisoformat(arrivals[-1]) if arrivals else None,
    )
    plan.conflict = detect_conflict(plan)
    return plan


def detect_conflict(plan: SearchPlan) -> str:
    sets = [s for s in plan.cuisines_by_member.values() if s]
    if len(sets) > 1:
        union = set().union(*sets)
        if not set.intersection(*sets) and len(union) > 1:
            return "cuisine"
    if plan.avoid & plan.all_cuisines:
        return "cuisine"
    if plan.budget_hard is not None and plan.budget_soft is not None and plan.budget_soft > plan.budget_hard:
        return "budget"
    return "none"


def blocked_cuisines(plan: SearchPlan) -> list[tuple[str, str]]:
    """Requested cuisines that someone's hard "avoid" rules out entirely (sushi vs no seafood)."""
    out = []
    for cuisine in sorted(plan.all_cuisines):
        tags = CUISINE_TAGS.get(cuisine, frozenset({cuisine}))
        for avoid in sorted(plan.avoid):
            if tags <= AVOID_TAGS.get(avoid, frozenset({avoid})):
                out.append((cuisine, avoid))
                break
    return out
