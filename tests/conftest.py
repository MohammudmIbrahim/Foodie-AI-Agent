from __future__ import annotations

import itertools
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.config import Settings
from app.domain.models import InboundMessage
from app.main import build
from app.messaging.mock import MockMessagingProvider
from app.persistence.db import Database

FIXTURES = Path(__file__).parent / "fixtures"
CHAT = "iMessage;+;chat-test-1"
OTHER_CHAT = "iMessage;+;chat-not-allowed"
ALEX, SAM, MIKE, JORDAN = "+1 (555) 000-0001", "+15550000002", "+15550000003", "jordan@example.com"
# Friday 2026-10-23 18:30 in Phoenix (UTC-7, no DST).
START = datetime(2026, 10, 24, 1, 30, tzinfo=timezone.utc)


class FakeClock:
    def __init__(self, now: datetime = START) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw) -> None:
        self.now += timedelta(**kw)


def make_settings(tmp_path: Path, **over) -> Settings:
    base = dict(
        _env_file=None,
        app_env="test",
        database_url=f"sqlite:///{tmp_path}/foodie.db",
        allowed_chat_guids=f"{CHAT},iMessage;+;chat-test-2",
        setup_senders="+15550000001",
        collect_seconds=0,
        debounce_seconds=0,
        group_consent_min=2,
        places_fixture_path=str(FIXTURES / "osm_snapshot.json"),
        geocode_fixture_path=str(FIXTURES / "geocode_snapshot.json"),
        timezone="America/Phoenix",
        id_hash_pepper="test-pepper",
    )
    base.update(over)
    return Settings(**base)


class Harness:
    def __init__(self, tmp_path: Path, settings: Settings | None = None, db: Database | None = None, clock: FakeClock | None = None, **providers) -> None:
        self.tmp_path = tmp_path
        self.settings = settings or make_settings(tmp_path)
        self.clock = clock or FakeClock()
        self.messaging = providers.pop("messaging", None) or MockMessagingProvider()
        self.container = build(self.settings, messaging=self.messaging, clock=self.clock, db=db, **providers)
        self.orch = self.container.orchestrator
        self.repo = self.orch.repo
        self._ids = itertools.count(1)

    async def send(self, sender: str, text: str, chat: str = CHAT, event_id: str | None = None,
                   is_from_me: bool = False, ts: datetime | None = None, drain: bool = True) -> str:
        msg = InboundMessage(
            event_id=event_id or f"evt-{next(self._ids)}-{id(self)}",
            chat_guid=chat, sender=sender, text=text, is_from_me=is_from_me,
            timestamp=ts or self.clock(),
        )
        outcome = await self.orch.handle(msg)
        if drain:
            await self.orch.drain()
        return outcome

    def texts(self, chat: str = CHAT) -> list[str]:
        return self.messaging.texts(chat)

    @property
    def last(self) -> str:
        return self.texts()[-1]

    async def onboard(self, name: str = "Chef", city: str | None = "Tempe, AZ") -> None:
        assert await self.send(ALEX, "Foodie AI setup") == "intro"
        await self.send(ALEX, "Foodie AI agree")
        await self.send(SAM, "Foodie AI agree")
        await self.send(MIKE, "Foodie AI agree")
        await self.send(ALEX, f"Foodie AI name {name}")
        await self.send(SAM, f"Foodie AI name {name}")
        await self.send(MIKE, "Foodie AI style direct")
        await self.send(ALEX, "Foodie AI close vote")
        if city:
            await self.send(ALEX, f"{name} city {city}")
            await self.send(SAM, f"{name} confirm")
        else:
            await self.send(ALEX, f"{name} skip city")
        group = self.repo.group_by_chat(CHAT)
        assert group["onboarding_state"] == "complete"


@pytest.fixture
def h(tmp_path: Path) -> Harness:
    return Harness(tmp_path)
