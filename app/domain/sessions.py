"""Session state machine. DORMANT is the absence of an active session for a group."""

from __future__ import annotations

from app.domain.models import SessionStatus as S

TRANSITIONS: dict[S, frozenset[S]] = {
    S.COLLECTING: frozenset({S.SEARCHING, S.STOPPED, S.EXPIRED}),
    S.SEARCHING: frozenset({S.COLLECTING, S.SHORTLIST, S.STOPPED, S.EXPIRED}),
    S.SHORTLIST: frozenset({S.VOTING, S.COLLECTING, S.SHORTLIST, S.DONE, S.STOPPED, S.EXPIRED}),
    S.VOTING: frozenset({S.DONE, S.COLLECTING, S.SHORTLIST, S.STOPPED, S.EXPIRED}),
    S.DONE: frozenset(),
    S.STOPPED: frozenset(),
    S.EXPIRED: frozenset(),
}


class InvalidTransition(RuntimeError):
    pass


def check_transition(current: S | str, new: S) -> None:
    cur = S(current)
    if cur == new and cur in (S.COLLECTING,):
        return
    if new not in TRANSITIONS[cur]:
        raise InvalidTransition(f"{cur.value} -> {new.value}")
