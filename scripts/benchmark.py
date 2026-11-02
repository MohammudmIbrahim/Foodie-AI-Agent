"""Locally reproducible stage timings (p50/p95) using mock messaging and the OSM snapshot.

These are measurements of THIS machine's offline pipeline only. They say nothing about public
Overpass latency or real iMessage delivery. Run: python scripts/benchmark.py [--sessions 40]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import platform
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import Settings  # noqa: E402
from app.domain.models import InboundMessage  # noqa: E402
from app.main import build  # noqa: E402
from app.messaging.mock import MockMessagingProvider  # noqa: E402

CHAT = "iMessage;+;bench"
USERS = ["+15550000001", "+15550000002", "+15550000003"]
SCRIPT = [
    (0, "Foodie AI find dinner near Tempe under $20"),
    (1, "halal for me"),
    (2, "no seafood"),
    (1, "lol did you see that game"),
    (2, "Actually burgers instead"),
    (0, "Foodie AI more"),
    (1, "Foodie AI vote 1"),
    (2, "Foodie AI vote 1"),
]


async def run(sessions: int) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        settings = Settings(
            _env_file=None, app_env="test", database_url=f"sqlite:///{tmp}/bench.db",
            allowed_chat_guids=CHAT, setup_senders=USERS[0], group_consent_min=1,
            collect_seconds=0, debounce_seconds=0, timezone="America/Phoenix",
            places_fixture_path=str(ROOT / "tests/fixtures/osm_snapshot.json"),
            geocode_fixture_path=str(ROOT / "tests/fixtures/geocode_snapshot.json"),
        )
        c = build(settings, messaging=MockMessagingProvider())
        o = c.orchestrator
        n = 0

        async def say(user: int, text: str) -> None:
            nonlocal n
            n += 1
            await o.handle(InboundMessage(f"b{n}", CHAT, USERS[user], text, False, datetime.now(timezone.utc)))
            await o.drain()

        for user, text in [(0, "Foodie AI setup"), (0, "Foodie AI agree"), (1, "Foodie AI agree"), (2, "Foodie AI agree"),
                           (0, "Foodie AI close vote"), (0, "Foodie AI skip city")]:
            await say(user, text)
        o.metrics.timings.clear()
        o.metrics.counters.clear()
        start = time.perf_counter()
        for _ in range(sessions):
            for user, text in SCRIPT:
                await say(user, text)
            if o.active_session(o.repo.group_by_chat(CHAT)["id"]):
                await say(0, "Foodie AI stop")
        wall = time.perf_counter() - start
        summary = o.metrics.summary()
        counters = summary["counters"]
        sent = counters.get("outbox_sent", 0)
        failed = counters.get("outbox_failed", 0) + counters.get("outbox_uncertain", 0)
        c.db.close()
        return {
            "measured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "machine": f"{platform.system()} {platform.release()} {platform.machine()} / Python {platform.python_version()}",
            "mode": "offline: mock messaging, OSM snapshot, rules decisions",
            "sessions": sessions,
            "messages": sessions * len(SCRIPT),
            "wall_seconds": round(wall, 2),
            "stages": summary["stages"],
            "stale_discarded": counters.get("stale_discarded", 0),
            "jev_requests_per_session": round(counters.get("jev_requests", 0) / sessions, 2),
            "cache_hit_rate": "n/a (fixture provider has no network cache)",
            "send_failure_rate": round(failed / max(sent + failed, 1), 4),
        }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sessions", type=int, default=40)
    ap.add_argument("--out", type=Path)
    args = ap.parse_args()
    result = asyncio.run(run(args.sessions))
    text = json.dumps(result, indent=1)
    print(text)
    if args.out:
        args.out.write_text(text + "\n")


if __name__ == "__main__":
    main()
