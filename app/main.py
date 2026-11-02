"""FastAPI app: minimal health, BlueBubbles webhook, dev-only simulator."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from app.config import Settings
from app.decisions.base import DecisionProvider
from app.decisions.jev import JevDecisionProvider
from app.decisions.rules import RulesDecisionProvider
from app.domain.models import InboundMessage
from app.messaging.base import MessagingProvider
from app.messaging.bluebubbles import BlueBubblesProvider
from app.messaging.mock import MockMessagingProvider
from app.observability import Metrics, setup_logging
from app.persistence.db import Database
from app.persistence.repositories import Repo, utcnow
from app.restaurants.base import Geocoder, PlacesProvider
from app.restaurants.mock import FixtureGeocoder, FixturePlacesProvider
from app.restaurants.osm import NominatimGeocoder, OverpassPlacesProvider
from app.security.webhook import is_loopback, verify_webhook
from app.services.orchestration import Orchestrator

log = logging.getLogger("foodie")
TICK_SECONDS = 30
MAX_BODY_BYTES = 64_000


@dataclass
class Container:
    settings: Settings
    db: Database
    orchestrator: Orchestrator
    http: httpx.AsyncClient | None
    tasks: set[asyncio.Task] = field(default_factory=set)


def build(
    settings: Settings,
    *,
    messaging: MessagingProvider | None = None,
    places: PlacesProvider | None = None,
    geocoder: Geocoder | None = None,
    decisions: DecisionProvider | None = None,
    clock=utcnow,
    db: Database | None = None,
) -> Container:
    """Wire providers from configuration. Paid / live providers only when explicitly enabled."""
    http = httpx.AsyncClient() if (
        settings.message_provider == "bluebubbles" or settings.places_provider == "osm" or settings.jev_active
    ) else None
    metrics = Metrics()
    db = db or Database(settings.sqlite_path)
    if messaging is None:
        messaging = (
            BlueBubblesProvider(http, settings.bb_base_url, settings.bb_password, settings.bb_send_method, settings.bb_timeout_seconds)
            if settings.message_provider == "bluebubbles" and http else MockMessagingProvider()
        )
    if places is None or geocoder is None:
        if settings.places_provider == "osm" and http:
            places = places or OverpassPlacesProvider(http, settings.overpass_url, settings.osm_user_agent, settings.places_timeout_seconds, settings.places_cache_seconds)
            geocoder = geocoder or NominatimGeocoder(http, settings.nominatim_url, settings.osm_user_agent)
        else:
            places = places or FixturePlacesProvider(settings.places_fixture_path)
            geocoder = geocoder or FixtureGeocoder(settings.geocode_fixture_path)
    if decisions is None:
        if settings.jev_active and http:
            decisions = JevDecisionProvider(
                http, schema=settings.jev_schema, base_url=settings.jev_base_url, endpoint=settings.jev_endpoint,
                api_key=settings.jev_api_key, model=settings.jev_model, timeout=settings.jev_timeout_seconds,
                min_confidence=settings.jev_min_confidence, max_calls_per_session=settings.jev_max_calls_per_session,
                max_calls_per_day=settings.jev_max_calls_per_day, metrics=metrics,
            )
        else:
            decisions = RulesDecisionProvider(settings.max_radius_miles)
    orch = Orchestrator(settings, Repo(db, clock), messaging, decisions, places, geocoder, metrics)
    return Container(settings, db, orch, http)


class SimulatedMessage(BaseModel):
    chat_guid: str = Field(max_length=200)
    sender: str = Field(max_length=200)
    text: str = Field(max_length=2000)
    event_id: str | None = Field(default=None, max_length=200)
    is_from_me: bool = False


def create_app(settings: Settings | None = None, container: Container | None = None) -> FastAPI:
    settings = settings or (container.settings if container else Settings())
    setup_logging(settings.log_level)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        c = container or build(settings)
        app.state.c = c
        recovered = c.orchestrator.recover()
        log.info("startup recovery %s", recovered)

        async def ticker() -> None:
            while True:
                await asyncio.sleep(TICK_SECONDS)
                try:
                    await c.orchestrator.tick()
                except Exception:
                    log.exception("tick failed")

        t = asyncio.create_task(ticker())
        try:
            yield
        finally:
            t.cancel()
            for task in list(c.tasks):
                task.cancel()
            if c.http:
                await c.http.aclose()

    app = FastAPI(title="Foodie AI", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    def _spawn(c: Container, msg: InboundMessage) -> None:
        task = asyncio.create_task(c.orchestrator.handle(msg))
        c.tasks.add(task)
        task.add_done_callback(c.tasks.discard)

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok"}

    @app.post("/webhooks/bluebubbles")
    async def bluebubbles_webhook(request: Request) -> dict:
        c: Container = request.app.state.c
        token = request.query_params.get("token")
        if not verify_webhook(c.settings, token, request.client.host if request.client else None):
            raise HTTPException(status_code=403, detail="forbidden")
        raw = await request.body()
        if len(raw) > MAX_BODY_BYTES:
            raise HTTPException(status_code=413, detail="too large")
        try:
            payload = await request.json()
        except ValueError as e:
            raise HTTPException(status_code=400, detail="invalid json") from e
        if c.settings.message_provider != "bluebubbles":
            return {"status": "ignored"}
        msg = c.orchestrator.messaging.normalize(payload)
        if msg is None:
            return {"status": "ignored"}
        _spawn(c, msg)  # queue fast; processing happens off the request path
        return {"status": "accepted"}

    @app.post("/dev/simulate")
    async def simulate(request: Request, body: SimulatedMessage) -> dict:
        c: Container = request.app.state.c
        if c.settings.app_env != "development" or not is_loopback(request.client.host if request.client else None):
            raise HTTPException(status_code=404, detail="not found")
        msg = InboundMessage(
            event_id=body.event_id or f"sim:{datetime.now(timezone.utc).timestamp()}",
            chat_guid=body.chat_guid, sender=body.sender, text=body.text,
            is_from_me=body.is_from_me, timestamp=datetime.now(timezone.utc),
        )
        outcome = await c.orchestrator.handle(msg)
        await c.orchestrator.drain()
        sent = []
        if isinstance(c.orchestrator.messaging, MockMessagingProvider):
            sent = c.orchestrator.messaging.texts(body.chat_guid)[-5:]
        return {"outcome": outcome, "recent_replies": sent}

    @app.get("/dev/metrics")
    async def dev_metrics(request: Request) -> dict:
        c: Container = request.app.state.c
        if c.settings.app_env != "development" or not is_loopback(request.client.host if request.client else None):
            raise HTTPException(status_code=404, detail="not found")
        return c.orchestrator.metrics.summary()

    return app


_app: FastAPI | None = None


def __getattr__(name: str) -> FastAPI:
    """`uvicorn app.main:app` builds the app lazily so importing this module has no side effects."""
    global _app
    if name == "app":
        if _app is None:
            _app = create_app()
        return _app
    raise AttributeError(name)
