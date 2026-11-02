"""Restaurant votes (stable option numbers per shortlist version) and nickname/personality polls."""

from __future__ import annotations

import json
from collections import Counter
from typing import TYPE_CHECKING

from app.domain import replies
from app.domain.commands import Routed, sanitize_nickname, sanitize_personality
from app.domain.models import (
    ConstraintKind,
    Evidence,
    InboundMessage,
    OnboardingState,
    SessionStatus,
)
from app.domain.parsing import Extraction
from app.domain.ranking import _diet_evidence
from app.persistence.repositories import iso, ranked_from_row
from app.restaurants.maps import maps_directions_url

if TYPE_CHECKING:
    from app.services.orchestration import Orchestrator


def majority(choices: list[str]) -> tuple[str | None, int, int]:
    """Explicit majority of submitted votes (> 50%). Ties / pluralities return None."""
    if not choices:
        return None, 0, 0
    counts = Counter(choices).most_common()
    top, n = counts[0]
    if n * 2 > len(choices):
        return top, n, len(choices)
    return None, n, len(choices)


class VotingService:
    def __init__(self, orch: Orchestrator) -> None:
        self.o = orch

    # ---- restaurant voting ------------------------------------------------------------------
    async def restaurant_command(self, group, member, session, routed: Routed, msg: InboundMessage) -> str:
        o, chat, name = self.o, group["chat_guid"], self.o.name(group)
        if not session:
            await o.reply(chat, f"There's nothing to vote on right now. Say \"{name}\" to start a search.")
            return "vote_no_session"
        if session["status"] in (SessionStatus.COLLECTING.value, SessionStatus.SEARCHING.value):
            if session["shortlist_version"]:
                await o.reply(chat, "That list is out of date because the requirements changed. I'll post a new one; please vote on that.")
                return "vote_rejected_stale"
            await o.reply(chat, "There's no shortlist yet.")
            return "vote_no_shortlist"
        o._ensure_participant(session["id"], member)
        slv = session["shortlist_version"]
        if routed.command == "vote_open":
            o.repo.set_status(session["id"], SessionStatus.VOTING)
            n = len(o.repo.shortlist(session["id"]))
            opts = ", ".join(f"\"{name} vote {i}\"" for i in range(1, n + 1))
            await o.reply(chat, f"Vote on the current shortlist: {opts}.", session["id"], session["version"])
            return "vote_opened"
        if routed.command == "close":
            return await self._close(group, session)

        # A vote sent before the current list was posted refers to an older list: reject, never reassign.
        if session["shortlist_sent_at"] and iso(msg.timestamp) < session["shortlist_sent_at"]:
            await o.reply(chat, "The options changed after that vote. Please vote again on the current list.")
            return "vote_rejected_stale"
        n = int(routed.arg)
        row = next((r for r in o.repo.shortlist(session["id"]) if r["option_number"] == n), None)
        if not row:
            await o.reply(chat, f"There's no option {n} on the current list.")
            return "vote_bad_option"
        o.repo.cast_vote(session["id"], member["id"], row["id"], slv)
        if session["status"] != SessionStatus.VOTING.value:
            o.repo.set_status(session["id"], SessionStatus.VOTING)
        o.repo.touch_session(session["id"], o.s.session_idle_minutes)
        tally = o.repo.tally(session["id"], slv)
        participants = max(len(o.repo.participants(session["id"])), 1)
        top_opt, top_n = max(tally.items(), key=lambda kv: (kv[1], -kv[0]))
        if top_n * 2 > participants:
            return await self._finalize(group, session, top_opt, tally)
        await o.reply(chat, "Current tally: " + _tally_text(tally) + ".", session["id"], session["version"])
        return "voted"

    async def _close(self, group, session) -> str:
        o, chat, name = self.o, group["chat_guid"], self.o.name(group)
        tally = o.repo.tally(session["id"], session["shortlist_version"])
        if not tally:
            await o.reply(chat, "No votes yet.")
            return "close_empty"
        best = max(tally.values())
        tied = sorted(opt for opt, n in tally.items() if n == best)
        if len(tied) == 1:
            return await self._finalize(group, session, tied[0], tally)
        if not session["tiebreak_sent"]:
            with o.repo.db.tx() as c:
                c.execute("UPDATE sessions SET tiebreak_sent = 1 WHERE id = ?", (session["id"],))
            await o.reply(chat, f"Tied between {' and '.join(map(str, tied))}. One more vote decides, or say \"{name} close vote\" again and I'll pick the higher-ranked option.",
                          session["id"], session["version"])
            return "tiebreak"
        # Second close after the single tiebreak prompt: deterministic best-ranked option.
        return await self._finalize(group, session, tied[0], tally)

    async def _finalize(self, group, session, option: int, tally: dict[int, int]) -> str:
        o = self.o
        row = next(r for r in o.repo.shortlist(session["id"]) if r["option_number"] == option)
        with o.repo.db.tx() as c:
            c.execute("UPDATE sessions SET selected_recommendation_id = ? WHERE id = ?", (row["id"], session["id"]))
        if not o.repo.end_session(session["id"], SessionStatus.DONE, "finalized"):
            return "finalize_race"
        o._cancel(group["id"])
        p = ranked_from_row(row).place
        o.repo.audit(group["id"], "session_finalized", votes=sum(tally.values()))
        await o.reply(group["chat_guid"], f"Going with {p.name} ({_tally_text(tally)}). Map: {p.maps_url} Directions: {maps_directions_url(p.latitude, p.longitude)}")
        return "finalized"

    async def late_joiner(self, group, last_session, ex: Extraction) -> str:
        """Someone joins after the decision: check the chosen place, never silently overturn it."""
        o, name = self.o, self.o.name(group)
        row = o.repo.recommendation(last_session["selected_recommendation_id"])
        if not row:
            return "late_joiner_no_selection"
        place = ranked_from_row(row).place
        lines = []
        for c in ex.constraints:
            label = c.value.replace("_", "-")
            if c.kind == ConstraintKind.ALLERGY:
                lines.append(f"I can't verify {label} allergy safety at {place.name}; please check with them directly.")
                continue
            ev = _diet_evidence(place, c.value)
            if ev == Evidence.YES:
                lines.append(f"{place.name} lists {label} options in OpenStreetMap (worth confirming with them).")
            elif ev == Evidence.NO:
                lines.append(f"{place.name}'s listing says it doesn't offer {label} options.")
            else:
                lines.append(f"I can't verify {label} options at {place.name} from its listing.")
        lines.append(f"The group already picked it. Want to reopen the search? Say \"{name} reopen\".")
        await o.reply(group["chat_guid"], " ".join(lines))
        return "late_joiner"

    # ---- nickname / personality polls -------------------------------------------------------
    async def open_personality_polls(self, group, kind: str, onboarding: bool = False) -> str:
        o, name = self.o, self.o.name(group)
        kinds = ("nickname", "personality") if kind == "both" else (kind,)
        for k in kinds:
            o.repo.open_poll(group["id"], k, o.s.poll_minutes)
        if onboarding:
            await o.reply(group["chat_guid"], replies.polls_open(name))
        elif kind == "nickname":
            await o.reply(group["chat_guid"], f"Name vote is open: reply \"{name} name <new name>\" (or \"{name} name Foodie\" to reset). Closes in {o.s.poll_minutes:g} minutes.")
        else:
            await o.reply(group["chat_guid"], f"Want a different style? Vote with \"{name} style chill\", funny, direct, or adaptive. Closes in {o.s.poll_minutes:g} minutes.")
        return "polls_opened"

    async def personality_vote(self, group, member, routed: Routed) -> str:
        o, chat = self.o, group["chat_guid"]
        if not member["session_consent"]:
            await o.reply(chat, f"Only members who opted in can vote. Reply \"{o.name(group)} agree\" first.")
            return "poll_vote_no_consent"
        kind = "nickname" if routed.command == "name" else "personality"
        poll = next((p for p in o.repo.open_polls(group["id"]) if p["kind"] == kind), None)
        if not poll:
            await o.reply(chat, f"There's no open {kind} vote. Say \"{o.name(group)} {'rename' if kind == 'nickname' else 'change style'}\" to start one.")
            return "poll_closed"
        if kind == "nickname":
            raw = routed.arg
            choice = "Foodie AI" if raw.strip().lower() in ("foodie", "foodie ai") else sanitize_nickname(raw)
        else:
            choice = sanitize_personality(routed.arg)
        if not choice:
            msg = ("Names need 2-20 letters and can't be a command word." if kind == "nickname"
                   else "Styles: adaptive, chill, funny, direct, or a short custom description (style only).")
            await o.reply(chat, msg)
            return "poll_vote_invalid"
        o.repo.poll_vote(poll["id"], member["id"], choice)
        return "poll_vote"

    async def close_personality_polls(self, group, reason: str) -> None:
        o = self.o
        group = o.repo.group(group["id"])
        if not group:
            return
        lines = []
        for poll in o.repo.open_polls(group["id"]):
            choices = o.repo.poll_choices(poll["id"])
            winner, n, total = majority(choices)
            if not o.repo.close_poll(poll["id"], winner):
                continue
            if poll["kind"] == "nickname":
                if winner:
                    o.repo.update_group(group["id"], agent_name=winner, settings_version=group["settings_version"] + 1)
                    lines.append(f"Name: {winner} ({n} of {total} votes). \"Foodie AI\" still works too.")
                else:
                    lines.append(f"No majority for a name, so I'm keeping {group['agent_name']}.")
            else:
                if winner:
                    o.repo.update_group(group["id"], personality_json=json.dumps({"style": winner}))
                    lines.append(f"Style: {winner.replace('custom:', '')} ({n} of {total} votes).")
                else:
                    style = o.personality(group)
                    lines.append(f"No majority for a style, so I'm keeping {style.replace('custom:', '')}.")
        if not lines:
            return
        o.repo.audit(group["id"], "polls_closed", reason=reason)
        group = o.repo.group(group["id"])
        text = " ".join(lines)
        if group["onboarding_state"] == OnboardingState.POLLS.value:
            if group["default_city"]:
                o.repo.update_group(group["id"], onboarding_state=OnboardingState.COMPLETE.value, onboarded=1)
                text += " " + replies.setup_done(group["agent_name"])
            else:
                o.repo.update_group(group["id"], onboarding_state=OnboardingState.CITY.value)
                text += " " + replies.ask_city(group["agent_name"])
        await o.reply(group["chat_guid"], text)


def _tally_text(tally: dict[int, int]) -> str:
    return ", ".join(f"{n} for option {opt}" for opt, n in sorted(tally.items(), key=lambda kv: (-kv[1], kv[0])))
