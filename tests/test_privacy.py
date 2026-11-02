"""P0 privacy: dormant discard, allowlist, consent, memory opt-in, forget me, erase."""

from conftest import ALEX, CHAT, JORDAN, MIKE, OTHER_CHAT, SAM, Harness

from app.decisions.rules import RulesDecisionProvider
from app.security.redaction import hash_sender, redact


class CountingDecisions(RulesDecisionProvider):
    name = "counting"


def table_count(h: Harness, table: str) -> int:
    return h.repo.db.one(f"SELECT COUNT(*) FROM {table}")[0]


def all_text_in_db(h: Harness) -> str:
    out = []
    for (t,) in h.repo.db.query("SELECT name FROM sqlite_master WHERE type='table'"):
        for row in h.repo.db.query(f"SELECT * FROM {t}"):
            out.append(" ".join(str(v) for v in tuple(row)))
    return "\n".join(out)


async def test_dormant_unrelated_message_zero_calls_storage_replies(tmp_path):
    decisions = CountingDecisions()
    h = Harness(tmp_path, decisions=decisions)
    await h.onboard()
    sent_before = len(h.texts())
    events_before = table_count(h, "processed_events")
    outcome = await h.send(SAM, "Anybody hungry? my number is 480-555-1234")
    assert outcome == "dormant"
    assert decisions.calls == 0
    assert len(h.texts()) == sent_before
    assert table_count(h, "processed_events") == events_before  # not even an event id
    assert "Anybody hungry" not in all_text_in_db(h)
    assert "480-555-1234" not in all_text_in_db(h)


async def test_unauthorized_group_is_not_processed(h):
    assert await h.send(ALEX, "Foodie AI setup", chat=OTHER_CHAT) == "not_allowed"
    assert await h.send(ALEX, "Foodie find food", chat=OTHER_CHAT) == "not_allowed"
    assert h.texts(OTHER_CHAT) == []
    assert table_count(h, "groups") == 0
    assert table_count(h, "processed_events") == 0


async def test_setup_requires_authorized_sender(h):
    assert await h.send(SAM, "Foodie AI setup") == "ignored_not_setup"
    assert h.texts() == []
    assert table_count(h, "groups") == 0


async def test_wake_ignored_before_consent(h):
    await h.send(ALEX, "Foodie AI setup")
    assert await h.send(SAM, "Foodie find tacos near Tempe") == "ignored_onboarding"
    assert table_count(h, "sessions") == 0


async def test_decline_stops_participation(h):
    await h.send(ALEX, "Foodie AI setup")
    assert await h.send(SAM, "Foodie AI decline") == "declined"
    assert await h.send(MIKE, "Foodie find tacos") == "ignored_declined"


async def test_no_raw_phone_or_email_stored(h):
    await h.onboard()
    await h.send(JORDAN, "Chef find tacos near Tempe")
    blob = all_text_in_db(h)
    for raw in ("5550000001", "5550000002", "jordan@example.com"):
        assert raw not in blob
    assert hash_sender("p", "+1 (555) 000-0001") == hash_sender("p", "+15550000001")


async def test_remember_requires_explicit_confirmation(h):
    await h.onboard()
    await h.send(SAM, "Chef remember I'm vegetarian")
    assert "confirm remember" in h.last
    assert table_count(h, "preferences") == 0
    await h.send(SAM, "Chef confirm remember")
    assert table_count(h, "preferences") == 1


async def test_saved_preference_applies_when_member_joins(h):
    await h.onboard()
    await h.send(SAM, "Chef remember halal")
    await h.send(SAM, "Chef confirm remember")
    await h.send(ALEX, "Chef find dinner near Tempe")
    assert "halal" not in h.last  # Sam hasn't joined this session yet
    await h.send(SAM, "Chef I'm in")
    assert "halal required" in h.last


async def test_forget_me_deletes_only_that_member(h):
    await h.onboard()
    for who, pref in ((SAM, "vegan"), (MIKE, "halal")):
        await h.send(who, f"Chef remember {pref}")
        await h.send(who, "Chef confirm remember")
    assert table_count(h, "preferences") == 2
    await h.send(SAM, "Chef forget me")
    assert h.last == "I've deleted your saved preferences for this group."
    rows = h.repo.db.query("SELECT value FROM preferences")
    assert [r[0] for r in rows] == ["halal"]


async def test_erase_group_requires_organizer(h):
    await h.onboard()
    assert await h.send(SAM, "Chef erase group") == "erase_denied"
    assert await h.send(ALEX, "Chef erase group") == "erased"
    assert h.repo.group_by_chat(CHAT) is None
    assert table_count(h, "members") == 0


async def test_privacy_command(h):
    await h.onboard()
    await h.send(SAM, "Chef privacy")
    assert "never store message text" in h.last
    assert "Jev is off" in h.last


def test_redaction():
    out = redact("call me 480-555-1234 or a@b.com https://x.y/z")
    assert "480" not in out and "a@b.com" not in out and "https" not in out


async def test_forget_me_removes_saved_derived_constraints_only(h):
    await h.onboard()
    await h.send(SAM, "Chef remember halal")
    await h.send(SAM, "Chef confirm remember")
    await h.send(ALEX, "Chef find dinner near Tempe")
    await h.send(SAM, "Chef I'm in, 3 miles max")
    session = h.repo.active_session(h.repo.group_by_chat(CHAT)["id"])
    assert sorted(c.constraint.value for c in h.repo.constraints(session["id"])) == ["3", "halal"]
    await h.send(SAM, "Chef forget me")
    assert [c.constraint.value for c in h.repo.constraints(session["id"])] == ["3"]
    assert "halal" not in h.last
