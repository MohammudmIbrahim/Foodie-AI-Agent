from datetime import timedelta

from conftest import ALEX, CHAT, JORDAN, MIKE, SAM

from app.services.voting import majority


def session(h):
    return h.repo.active_session(h.repo.group_by_chat(CHAT)["id"])


async def start_shortlist(h, text="Chef find burgers near Tempe"):
    await h.onboard()
    await h.send(ALEX, text)
    await h.send(SAM, "Chef I'm in")
    await h.send(MIKE, "Chef me too")
    assert session(h)["status"] == "shortlist"


async def test_changed_vote_replaces_old_vote(h):
    await start_shortlist(h)
    await h.send(MIKE, "Chef vote 2")
    await h.send(MIKE, "Chef vote 3")
    s = session(h)
    assert h.repo.tally(s["id"], s["shortlist_version"]) == {3: 1}


async def test_majority_finalizes_with_map_link(h):
    await start_shortlist(h)
    await h.send(MIKE, "Chef vote 2")
    await h.send(SAM, "Chef vote 1")
    assert "Current tally" in h.last
    await h.send(MIKE, "Chef vote 1")
    assert h.last.startswith("Going with ")
    assert "https://www.google.com/maps/dir/?api=1&destination=" in h.last
    assert session(h) is None
    done = h.repo.db.one("SELECT status, selected_recommendation_id FROM sessions")
    assert done["status"] == "done" and done["selected_recommendation_id"]


async def test_vote_against_old_shortlist_version_rejected(h):
    await start_shortlist(h)
    old_ts = h.clock() - timedelta(seconds=30)
    h.clock.advance(seconds=5)
    await h.send(ALEX, "Chef more")
    outcome = await h.send(SAM, "Chef vote 2", ts=old_ts)
    assert outcome == "vote_rejected_stale"
    s = session(h)
    assert h.repo.voters(s["id"], s["shortlist_version"]) == 0


async def test_more_gives_new_option_numbers_and_clears_votes(h):
    await start_shortlist(h)
    first = {r["place_id"] for r in h.repo.shortlist(session(h)["id"])}
    await h.send(SAM, "Chef vote 1")
    h.clock.advance(seconds=1)
    await h.send(ALEX, "Chef more")
    s = session(h)
    second = {r["place_id"] for r in h.repo.shortlist(s["id"])}
    assert first.isdisjoint(second)
    assert h.repo.voters(s["id"], s["shortlist_version"]) == 0


async def test_closer_and_directions_and_reviews(h):
    await start_shortlist(h)
    await h.send(ALEX, "Chef directions 1")
    assert "maps/dir/?api=1&destination=" in h.last
    await h.send(ALEX, "Chef reviews 1")
    assert "won't make any up" in h.last
    await h.send(ALEX, "Chef directions 9")
    assert "no option 9" in h.last
    await h.send(ALEX, "Chef closer")
    assert "closest" in h.last or "Map: https://" in h.last


async def test_three_way_tie_one_tiebreaker_then_close(h):
    await start_shortlist(h)
    await h.send(ALEX, "Chef vote 1")
    await h.send(SAM, "Chef vote 2")
    await h.send(MIKE, "Chef vote 3")
    await h.send(ALEX, "Chef close vote")
    assert h.last.startswith("Tied between 1 and 2 and 3")
    await h.send(ALEX, "Chef close vote")
    assert h.last.startswith("Going with ")
    tiebreaks = [t for t in h.texts() if t.startswith("Tied")]
    assert len(tiebreaks) == 1


async def test_vote_with_no_shortlist(h):
    await h.onboard()
    assert await h.send(ALEX, "Chef vote 1") == "vote_no_session"


async def test_late_joiner_does_not_overturn_finalized_plan(h):
    await start_shortlist(h)
    await h.send(MIKE, "Chef vote 1")
    await h.send(SAM, "Chef vote 1")
    assert h.last.startswith("Going with ")
    await h.send(JORDAN, "Chef I'm joining, need vegetarian options")
    assert "already picked" in h.last and "reopen" in h.last
    assert h.repo.db.one("SELECT COUNT(*) FROM sessions")[0] == 1


async def test_late_joiner_during_voting_reopens_transparently(h):
    await start_shortlist(h)
    await h.send(MIKE, "Chef vote 1")
    await h.send(JORDAN, "Chef vegan for me")
    assert "earlier votes were cleared" in h.last
    assert "vegan required" in h.last


def test_majority_rule():
    assert majority(["Chef", "Chef", "Bob"]) == ("Chef", 2, 3)
    assert majority(["Chef", "Bob"])[0] is None
    assert majority(["Chef", "Bob", "Sam"])[0] is None
    assert majority([])[0] is None
