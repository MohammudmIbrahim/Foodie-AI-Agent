import json

from conftest import ALEX, CHAT, MIKE, SAM, Harness

from app.persistence.db import Database


async def test_full_onboarding_flow(h):
    await h.onboard(name="Chef", city="Tempe, AZ")
    g = h.repo.group_by_chat(CHAT)
    assert g["agent_name"] == "Chef"
    assert json.loads(g["personality_json"]) == {"style": "direct"}
    assert g["default_city"].startswith("Tempe")
    assert g["default_radius_miles"] == 5
    assert "All set" in h.last


async def test_intro_once_even_with_duplicates_and_repeats(h):
    await h.send(ALEX, "Foodie AI setup", event_id="e1")
    assert await h.send(ALEX, "Foodie AI setup", event_id="e1") == "duplicate"
    assert await h.send(ALEX, "Foodie AI setup", event_id="e2") == "setup_repeat"
    intros = [t for t in h.texts() if t.startswith("Hi, I'm")]
    assert len(intros) == 1


async def test_onboarding_once_after_restart(tmp_path):
    h1 = Harness(tmp_path)
    await h1.send(ALEX, "Foodie AI setup", event_id="boot-1")
    h1.container.db.close()
    h2 = Harness(tmp_path, settings=h1.settings, db=Database(h1.settings.sqlite_path))
    h2.orch.recover()
    assert await h2.send(ALEX, "Foodie AI setup", event_id="boot-1") == "duplicate"
    assert await h2.send(ALEX, "Foodie AI setup", event_id="boot-2") == "setup_repeat"
    assert not any(t.startswith("Hi, I'm") for t in h2.texts())


async def test_city_not_saved_until_confirmed(h):
    await h.send(ALEX, "Foodie AI setup")
    await h.send(ALEX, "Foodie AI agree")
    await h.send(SAM, "Foodie AI agree")
    await h.send(ALEX, "Foodie AI close vote")
    await h.send(ALEX, "Foodie AI city Tempe")
    assert h.repo.group_by_chat(CHAT)["default_city"] is None
    await h.send(MIKE, "Foodie AI radius 3")
    assert "3-mile" in h.last
    await h.send(MIKE, "Foodie AI confirm")
    g = h.repo.group_by_chat(CHAT)
    assert g["default_city"].startswith("Tempe") and g["default_radius_miles"] == 3


async def test_unknown_city_is_not_invented(h):
    await h.send(ALEX, "Foodie AI setup")
    await h.send(ALEX, "Foodie AI agree")
    await h.send(SAM, "Foodie AI agree")
    await h.send(ALEX, "Foodie AI close vote")
    await h.send(ALEX, "Foodie AI city Atlantis")
    assert "couldn't find" in h.last


async def test_tonights_location_does_not_overwrite_default(h):
    await h.onboard(city="Tempe, AZ")
    await h.send(ALEX, "Chef find tacos in Mesa")
    assert "Mesa" in h.last
    assert h.repo.group_by_chat(CHAT)["default_city"].startswith("Tempe")


async def test_nickname_tie_keeps_default(h):
    await h.send(ALEX, "Foodie AI setup")
    await h.send(ALEX, "Foodie AI agree")
    await h.send(SAM, "Foodie AI agree")
    await h.send(ALEX, "Foodie AI name Chef")
    await h.send(SAM, "Foodie AI name Buddy")
    await h.send(ALEX, "Foodie AI close vote")
    assert h.repo.group_by_chat(CHAT)["agent_name"] == "Foodie AI"
    assert "No majority for a name" in h.texts()[-1]


async def test_personality_tie_keeps_current(h):
    await h.onboard()  # style direct
    await h.send(SAM, "Chef you're too formal")
    await h.send(SAM, "Chef style funny")
    await h.send(MIKE, "Chef style chill")
    await h.send(ALEX, "Chef close vote")
    assert json.loads(h.repo.group_by_chat(CHAT)["personality_json"])["style"] == "direct"


async def test_only_consenting_members_vote_and_duplicates_replace(h):
    await h.send(ALEX, "Foodie AI setup")
    await h.send(ALEX, "Foodie AI agree")
    await h.send(SAM, "Foodie AI agree")
    assert await h.send(MIKE, "Foodie AI name Bob") == "poll_vote_no_consent"
    await h.send(ALEX, "Foodie AI name Chef")
    await h.send(ALEX, "Foodie AI name Chef")
    await h.send(SAM, "Foodie AI name Chef")
    await h.send(ALEX, "Foodie AI close vote")
    assert "2 of 2 votes" in h.last


async def test_poll_closes_on_timeout(h):
    await h.send(ALEX, "Foodie AI setup")
    await h.send(ALEX, "Foodie AI agree")
    await h.send(SAM, "Foodie AI agree")
    await h.send(ALEX, "Foodie AI name Chef")
    await h.send(SAM, "Foodie AI name Chef")
    h.clock.advance(minutes=11)
    await h.orch.tick()
    assert h.repo.group_by_chat(CHAT)["agent_name"] == "Chef"
    assert "usual city" in h.last


async def test_fallback_wake_word_after_rename(h):
    await h.onboard(name="Chef")
    assert await h.send(SAM, "Foodie AI help") == "help"
    assert await h.send(SAM, "Chef help") == "help"
    assert await h.send(SAM, "Foodie help") == "help"
