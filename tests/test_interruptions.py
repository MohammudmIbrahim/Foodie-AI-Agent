"""Interruptions, stale results, restart recovery and outbox delivery uncertainty."""

import asyncio

from conftest import ALEX, CHAT, FIXTURES, MIKE, SAM, Harness

from app.messaging.mock import MockMessagingProvider
from app.persistence.db import Database
from app.restaurants.mock import FixturePlacesProvider


class GatedPlaces(FixturePlacesProvider):
    """Real fixture data, but each search waits for the test to release it."""

    def __init__(self):
        super().__init__(FIXTURES / "osm_snapshot.json")
        self.gate = asyncio.Event()
        self.started = asyncio.Event()

    async def search(self, center, radius_miles, cuisine_tags):
        self.started.set()
        await self.gate.wait()
        return await super().search(center, radius_miles, cuisine_tags)


def shortlists(h):
    return [t for t in h.texts() if "Map: https://" in t]


async def test_new_preference_during_lookup_never_sends_old_results(tmp_path):
    places = GatedPlaces()
    h = Harness(tmp_path, places=places)
    places.gate.set()
    await h.onboard()
    places.gate.clear()
    await h.send(ALEX, "Chef find burgers in Mesa", drain=False)
    await asyncio.wait_for(places.started.wait(), 2)
    g = h.repo.group_by_chat(CHAT)
    v_start = h.repo.active_session(g["id"])["version"]
    await h.send(MIKE, "Actually chicken instead", drain=False)
    assert h.repo.active_session(g["id"])["version"] > v_start
    places.gate.set()
    await h.orch.drain()
    lists = shortlists(h)
    assert len(lists) == 1, lists
    assert "chicken" in lists[0] and "burger" not in lists[0].split("\n")[0]
    assert h.orch.metrics.counters["stale_discarded"] + h.orch.metrics.counters["search_cancelled"] >= 1
    # The old version never reached the outbox.
    rows = h.repo.db.query("SELECT payload FROM outbox WHERE payload LIKE '%Map: https://%'")
    assert len(rows) == 1


async def test_stop_mid_search_suppresses_late_results(tmp_path):
    places = GatedPlaces()
    h = Harness(tmp_path, places=places)
    places.gate.set()
    await h.onboard()
    places.gate.clear()
    await h.send(ALEX, "Chef find tacos near Tempe", drain=False)
    await asyncio.wait_for(places.started.wait(), 2)
    await h.send(SAM, "Chef stop", drain=False)
    places.gate.set()
    await h.orch.drain()
    await asyncio.sleep(0.05)
    assert h.last == "Stopped. Call me again when you're ready."
    assert shortlists(h) == []


async def test_stale_outbox_row_is_discarded_at_delivery(h):
    await h.onboard()
    await h.send(ALEX, "Chef find tacos near Tempe")
    g = h.repo.group_by_chat(CHAT)
    s = h.repo.active_session(g["id"])
    h.repo.enqueue(CHAT, "old shortlist", 60, s["id"], s["version"] - 1)
    await h.orch.flush_outbox()
    assert "old shortlist" not in h.texts()
    assert h.repo.db.one("SELECT state FROM outbox WHERE payload = 'old shortlist'")[0] == "discarded"


async def test_two_members_at_once_update_one_session(h):
    await h.onboard()
    await h.send(ALEX, "Chef find dinner near Tempe")
    await asyncio.gather(
        h.send(SAM, "halal for me", drain=False),
        h.send(MIKE, "$15 is a hard max", drain=False),
    )
    await h.orch.drain()
    s = h.repo.active_session(h.repo.group_by_chat(CHAT)["id"])
    cons = h.repo.constraints(s["id"])
    owners = {c.constraint.value: c.constraint.member_id for c in cons}
    assert set(owners) == {"halal", "15"}
    assert owners["halal"] != owners["15"]
    assert h.repo.db.one("SELECT COUNT(*) FROM sessions")[0] == 1


async def test_own_change_only_updates_own_constraint(h):
    await h.onboard()
    await h.send(ALEX, "Chef find dinner near Tempe")
    await h.send(SAM, "$15 is a hard max")
    await h.send(MIKE, "under $30 for me")
    await h.send(MIKE, "under $20 for me")
    s = h.repo.active_session(h.repo.group_by_chat(CHAT)["id"])
    budgets = sorted((c.constraint.value, c.constraint.hard) for c in h.repo.constraints(s["id"]) if c.constraint.kind.value == "budget")
    assert budgets == [("15", True), ("20", False)]


async def test_we_all_cannot_overwrite_someones_hard_budget(h):
    await h.onboard()
    await h.send(ALEX, "Chef find dinner near Tempe")
    await h.send(SAM, "$15 is a hard max")
    await h.send(MIKE, "we all want to spend up to $40")
    assert "only that person can change it" in h.texts()[-2] or "only that person can change it" in h.last
    s = h.repo.active_session(h.repo.group_by_chat(CHAT)["id"])
    assert ("15", True) in [(c.constraint.value, c.constraint.hard) for c in h.repo.constraints(s["id"])]
    await h.send(SAM, "Chef drop my budget")
    assert ("15", True) not in [(c.constraint.value, c.constraint.hard) for c in h.repo.constraints(s["id"])]


async def test_restart_expires_inflight_search_and_keeps_dedupe(tmp_path):
    places = GatedPlaces()
    h1 = Harness(tmp_path, places=places)
    places.gate.set()
    await h1.onboard()
    places.gate.clear()
    await h1.send(ALEX, "Chef find tacos near Tempe", event_id="evt-before-crash", drain=False)
    await asyncio.wait_for(places.started.wait(), 2)
    # Simulate a crash: abandon the job; leave an in-flight send in 'sending'.
    h1.orch._cancel(h1.repo.group_by_chat(CHAT)["id"])
    h1.repo.enqueue(CHAT, "half-sent message", 60)
    h1.repo.db.query("UPDATE outbox SET state = 'sending' WHERE payload = 'half-sent message'")
    h1.container.db.close()

    messaging = MockMessagingProvider()
    h2 = Harness(tmp_path, settings=h1.settings, db=Database(h1.settings.sqlite_path), messaging=messaging, clock=h1.clock)
    result = h2.orch.recover()
    assert result["expired_sessions"] == 1
    assert result["uncertain_sends"] == 1
    assert await h2.send(ALEX, "Chef find tacos near Tempe", event_id="evt-before-crash") == "duplicate"
    await h2.orch.flush_outbox()
    assert messaging.sent == []  # nothing replayed
    await h2.send(ALEX, "Chef are you back? tacos near Tempe")
    assert "earlier search expired" in messaging.texts()[0]


async def test_uncertain_delivery_is_not_retried(h):
    await h.onboard()
    h.messaging.fail_mode = "uncertain"
    await h.send(ALEX, "Chef help")
    h.messaging.fail_mode = None
    await h.orch.flush_outbox()
    await h.orch.tick()
    assert not any("Start:" in t for t in h.texts())
    assert h.repo.db.one("SELECT COUNT(*) FROM outbox WHERE state = 'uncertain'")[0] == 1


async def test_retryable_delivery_is_bounded(h):
    await h.onboard()
    h.messaging.fail_mode = "retryable"
    await h.send(ALEX, "Chef help")
    for _ in range(5):
        await h.orch.flush_outbox()
    assert h.repo.db.one("SELECT attempts, state FROM outbox WHERE payload LIKE 'Start:%'")[:] == (2, "failed")


async def test_vote_on_invalidated_shortlist_is_reopened(h):
    await h.onboard()
    await h.send(ALEX, "Chef find burgers near Tempe")
    await h.send(SAM, "Chef vote 1")
    await h.send(MIKE, "Actually pizza instead")
    assert "earlier votes were cleared" in h.last
    assert h.repo.db.one("SELECT COUNT(*) FROM votes")[0] == 0
