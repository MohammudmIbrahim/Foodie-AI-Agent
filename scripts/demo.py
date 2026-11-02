"""Offline group-chat demo: no Apple hardware, BlueBubbles, Jev or network needed.

Uses the mock messaging provider and the real OpenStreetMap snapshot in tests/fixtures.
Run: python scripts/demo.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import Settings  # noqa: E402
from app.domain.models import InboundMessage  # noqa: E402
from app.main import build  # noqa: E402
from app.messaging.mock import MockMessagingProvider  # noqa: E402
from app.restaurants.mock import FixturePlacesProvider  # noqa: E402

CHAT = "iMessage;+;chat-demo"
PEOPLE = {"Alex": "+15550000001", "Sam": "+15550000002", "Mike": "+15550000003", "Jordan": "jordan@example.com"}


class Demo:
    def __init__(self, tmp: str) -> None:
        settings = Settings(
            _env_file=None, app_env="development", database_url=f"sqlite:///{tmp}/demo.db",
            allowed_chat_guids=CHAT, setup_senders=PEOPLE["Alex"], id_hash_pepper="demo-pepper",
            collect_seconds=0.6, debounce_seconds=0.3, timezone="America/Phoenix",
            places_fixture_path=str(ROOT / "tests/fixtures/osm_snapshot.json"),
            geocode_fixture_path=str(ROOT / "tests/fixtures/geocode_snapshot.json"),
        )
        self.messaging = MockMessagingProvider()
        # Small artificial latency so mid-search interruptions are visible.
        places = FixturePlacesProvider(settings.places_fixture_path, latency_seconds=0.4)
        self.container = build(settings, messaging=self.messaging, places=places)
        self.orch = self.container.orchestrator
        self.n = 0
        self.printed = 0

    async def say(self, who: str, text: str, wait: float = 0.0) -> None:
        self.n += 1
        print(f"\n{who}: {text}")
        msg = InboundMessage(f"demo-{self.n}", CHAT, PEOPLE[who], text, False, datetime.now(timezone.utc))
        outcome = await self.orch.handle(msg)
        if wait:
            await asyncio.sleep(wait)
        else:
            await self.orch.drain()
        self.flush(outcome)

    def flush(self, outcome: str | None = None) -> None:
        texts = self.messaging.texts(CHAT)
        if outcome in ("dormant", "ignored_unrelated"):
            print("   [no reply, nothing stored, no AI call]")
        for t in texts[self.printed:]:
            name = self.container.orchestrator.repo.group_by_chat(CHAT)
            label = name["agent_name"] if name else "Foodie AI"
            print(f"{label}: " + t.replace("\n", "\n" + " " * (len(label) + 2)))
        self.printed = len(texts)

    def banner(self, title: str) -> None:
        print("\n" + "=" * 72 + f"\n{title}\n" + "=" * 72)


async def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        d = Demo(tmp)
        d.banner("1. Onboarding (once), consent, nickname + style vote, default city")
        await d.say("Alex", "Foodie AI setup")
        await d.say("Alex", "Foodie AI agree")
        await d.say("Sam", "Foodie AI agree")
        await d.say("Mike", "Foodie AI agree")
        await d.say("Alex", "Foodie AI name Chef")
        await d.say("Sam", "Foodie AI name Chef")
        await d.say("Mike", "Foodie AI style direct")
        await d.say("Alex", "Foodie AI close vote")
        await d.say("Alex", "Chef city Tempe, AZ")
        await d.say("Sam", "Chef confirm")

        d.banner("2. Dormant chat is ignored; wake + per-person constraints coalesce")
        await d.say("Mike", "Anybody hungry?")
        await d.say("Alex", "Chef find us dinner near Tempe, under $25.", wait=0.1)
        await d.say("Sam", "Halal for me.", wait=0.1)
        await d.say("Mike", "No seafood.", wait=0.1)
        await d.orch.drain()
        d.flush()

        d.banner("3. Conflicting cravings + hard budget")
        await d.say("Alex", "Chef sushi please", wait=0.05)
        await d.say("Mike", "Burgers for me", wait=0.05)
        await d.say("Sam", "$15 is a hard maximum", wait=0.05)
        await d.orch.drain()
        d.flush()

        d.banner("4. Vote and finalize")
        await d.say("Mike", "Chef vote 2")
        await d.say("Sam", "Chef vote 1")
        await d.say("Mike", "Chef vote 1")

        d.banner("5. Late joiner after the decision")
        await d.say("Jordan", "Chef I'm joining, need vegetarian options")

        d.banner("6. Mid-search correction: the burger search is invalidated, never sent")
        # Collection window 0.6 s, then a search that takes 0.4 s: the correction lands mid-flight.
        await d.say("Alex", "Chef find burgers in Mesa", wait=0.75)
        print("   [burger search in flight]")
        await d.say("Mike", "Actually chicken instead", wait=0.05)
        await d.say("Sam", "Five miles max", wait=0.05)
        await d.orch.drain()
        d.flush()

        d.banner("7. Privacy: remember (opt-in), forget me, stop")
        await d.say("Sam", "Chef remember halal")
        await d.say("Sam", "Chef confirm remember")
        await d.say("Sam", "Chef forget me")
        await d.say("Alex", "Chef stop")
        await d.say("Mike", "ok so tacos tomorrow?")

        d.banner("Run metrics (this run only; not a benchmark)")
        print(json.dumps(d.orch.metrics.summary()["counters"], indent=1))
        sent = len(d.messaging.sent)
        print(f"Messages sent by mock provider: {sent}. Real network calls: 0.")
        d.container.db.close()


if __name__ == "__main__":
    asyncio.run(main())
