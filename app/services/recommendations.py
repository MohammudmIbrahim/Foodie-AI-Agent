"""Versioned restaurant search and shortlist commands (more / closer / directions / reviews).

The search itself is the deterministic workflow in app/workflow/search.py; this service runs it for
one session version and delivers the result only if that version is still current.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from app.domain import replies
from app.domain.commands import Routed
from app.domain.conflict import build_plan
from app.domain.models import GeoPoint, RankedPlace, SearchPlan
from app.persistence.repositories import ranked_from_row
from app.restaurants.maps import maps_directions_url
from app.workflow.base import Halt
from app.workflow.search import SEARCH_WORKFLOW, SearchState

if TYPE_CHECKING:
    from app.services.orchestration import Orchestrator

log = logging.getLogger("foodie.search")
POOL_SIZE = 15


class RecommendationService:
    def __init__(self, orch: Orchestrator) -> None:
        self.o = orch
        self.searched: set[str] = set()

    def plan_for(self, session, group, point: GeoPoint | None) -> SearchPlan:
        cons = [s.constraint for s in self.o.repo.constraints(session["id"])]
        return build_plan(
            cons, location=point,
            default_radius=group["default_radius_miles"] or self.o.s.default_radius_miles,
            max_radius=self.o.s.max_radius_miles,
        )

    async def run_search(self, group_id: str, session_id: str, version: int) -> None:
        start = time.perf_counter()
        try:
            await self._run(group_id, session_id, version)
        except asyncio.CancelledError:
            self.o.metrics.incr("search_cancelled")
            raise
        except Exception:
            log.exception("search job failed")
            self.o.repo.revert_to_collecting(session_id, version)
        finally:
            self.o.metrics.observe("search_job_total", time.perf_counter() - start)

    async def _fail(self, group, session_id: str, version: int, text: str) -> None:
        if self.o.repo.revert_to_collecting(session_id, version):
            await self.o.reply(group["chat_guid"], text, session_id, version)
        else:
            self.o.metrics.incr("stale_discarded")

    async def _run(self, group_id: str, session_id: str, version: int) -> None:
        o = self.o
        group, session = o.repo.group(group_id), o.repo.session(session_id)
        if not group or not session:
            return
        st = SearchState(
            o=o, group=group, session=session, version=version,
            updated=session_id in self.searched, votes_cleared=session_id in o.votes_cleared,
        )
        try:
            await SEARCH_WORKFLOW.run(st)
        except Halt as h:
            o.metrics.incr(f"search_halt_{h.reason}")
            await self._fail(group, session_id, version, h.reply)
            return
        top = st.pool[:POOL_SIZE]
        stored = top + [r for r in st.shortlist if r not in top]
        if o.repo.store_pool_and_shortlist(session_id, version, stored, [r.place.provider_place_id for r in st.shortlist]) is None:
            o.metrics.incr("stale_discarded")
            log.info("discarded stale search result version=%d", version)
            return
        o.votes_cleared.discard(session_id)
        self.searched.add(session_id)
        await o.reply(group["chat_guid"], st.text, session_id, version)

    # ---- shortlist commands -----------------------------------------------------------------
    async def shortlist_command(self, group, session, routed: Routed) -> str:
        o, name, chat = self.o, self.o.name(group), group["chat_guid"]
        if not session or session["status"] not in ("shortlist", "voting"):
            await o.reply(chat, f"There's no current shortlist. Say \"{name} find dinner near <area>\" to start.")
            return "no_shortlist"
        current = o.repo.shortlist(session["id"])
        if routed.command in ("directions", "reviews"):
            n = int(routed.arg)
            row = next((r for r in current if r["option_number"] == n), None)
            if not row:
                await o.reply(chat, f"There's no option {n} on the current list.")
                return "bad_option"
            p = ranked_from_row(row).place
            if routed.command == "directions":
                await o.reply(chat, f"Directions to {p.name} from wherever you are: {maps_directions_url(p.latitude, p.longitude)}")
                return "directions"
            await o.reply(chat, f"I don't have review text for {p.name} from {o.places.name}, and I won't make any up. The Maps link shows public reviews: {p.maps_url}")
            return "reviews_unavailable"

        pool = [ranked_from_row(r) for r in o.repo.pool(session["id"])]
        shown = {r["place_id"] for r in current}
        if routed.command == "more":
            last_rank = max((r["rank"] for r in current), default=0)
            nxt = [r for r, row in zip(pool, o.repo.pool(session["id"]), strict=True) if row["rank"] > last_rank and r.place.provider_place_id not in shown][:3]
            if not nxt:
                await o.reply(chat, f"That's everything I found that fits. Say \"{name} radius 10\" to look wider.")
                return "more_exhausted"
            picked = nxt
        else:
            picked = sorted(pool, key=lambda r: (len(r.unverified), r.distance_miles))[:3]
            if {r.place.provider_place_id for r in picked} == shown:
                await o.reply(chat, "These are already the closest options that fit.")
                return "closer_same"
        o.repo.reassign_shortlist(session["id"], [r.place.provider_place_id for r in picked])
        session = o.repo.session(session["id"])
        plan = self.plan_for(session, group, None)
        text = replies.shortlist(name, picked, plan, o.places.attribution, ["New option numbers; earlier votes were cleared."])
        await o.reply(chat, text, session["id"], session["version"])
        return routed.command or "shortlist"



__all__ = ["RecommendationService", "RankedPlace"]
