import pytest
from conftest import ALEX, CHAT, MIKE, SAM

from app.domain.models import SessionStatus
from app.domain.sessions import InvalidTransition, check_transition


def active(h):
    g = h.repo.group_by_chat(CHAT)
    return h.repo.active_session(g["id"])


async def test_wake_creates_exactly_one_session(h):
    await h.onboard()
    await h.send(ALEX, "Chef find dinner near Tempe", drain=False)
    await h.send(SAM, "Chef find food", drain=False)
    await h.orch.drain()
    n = h.repo.db.one("SELECT COUNT(*) FROM sessions")[0]
    assert n == 1


async def test_dormant_until_wake_then_accepts_contributions(h):
    await h.onboard()
    assert await h.send(SAM, "Yeah I'm starving") == "dormant"
    await h.send(ALEX, "Chef find dinner near Tempe")
    assert await h.send(SAM, "Halal for me.") == "updated"
    cons = h.repo.constraints(active(h)["id"])
    assert [c.constraint.value for c in cons] == ["halal"]


async def test_unrelated_message_in_active_session_is_ignored(h):
    await h.onboard()
    await h.send(ALEX, "Chef find dinner near Tempe")
    before = len(h.texts())
    assert await h.send(SAM, "lol did you see the game") == "ignored_unrelated"
    assert len(h.texts()) == before


async def test_asks_for_area_when_unknown(h):
    await h.onboard(city=None)
    assert await h.send(ALEX, "Chef help me find dinner") == "session_started_need_location"
    assert "What area" in h.last
    await h.send(SAM, "near Tempe please")
    assert "within 5 mi of Tempe" in h.last


async def test_stop_ends_session_and_returns_dormant(h):
    await h.onboard()
    await h.send(ALEX, "Chef find dinner near Tempe")
    assert await h.send(SAM, "Chef stop") == "stopped"
    assert h.last == "Stopped. Call me again when you're ready."
    assert active(h) is None
    assert await h.send(SAM, "halal for me") == "dormant"


async def test_idle_timeout_expires_and_reports_on_return(h):
    await h.onboard()
    await h.send(ALEX, "Chef find food near Tempe")
    h.clock.advance(minutes=11)
    await h.orch.tick()
    assert active(h) is None
    assert h.repo.db.one("SELECT end_reason FROM sessions")[0] == "timeout"
    await h.send(ALEX, "Chef are you back? find food near Tempe")
    assert any("earlier search expired" in t for t in h.texts()[-2:])


async def test_session_constraints_cleared_on_end(h):
    await h.onboard()
    await h.send(ALEX, "Chef find dinner near Tempe")
    await h.send(SAM, "vegan for me")
    await h.send(MIKE, "Chef stop")
    assert h.repo.db.one("SELECT COUNT(*) FROM session_constraints")[0] == 0


def test_state_machine_rejects_invalid_transitions():
    check_transition(SessionStatus.COLLECTING, SessionStatus.SEARCHING)
    check_transition(SessionStatus.SEARCHING, SessionStatus.COLLECTING)
    with pytest.raises(InvalidTransition):
        check_transition(SessionStatus.DONE, SessionStatus.SEARCHING)
    with pytest.raises(InvalidTransition):
        check_transition(SessionStatus.COLLECTING, SessionStatus.VOTING)
