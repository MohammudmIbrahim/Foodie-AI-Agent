"""Event admission, routing, onboarding, privacy commands, sessions and outbox delivery.

Admission order (cheapest first, before any storage or network):
self-sent -> chat allowlist -> wake word / active session -> event dedupe -> per-group lock.
Dormant messages that fail these checks leave no trace: no body, no event id, no call.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import defaultdict
from datetime import datetime, timedelta, tzinfo
from zoneinfo import ZoneInfo

from app.config import Settings
from app.decisions.base import Decision, DecisionProvider
from app.decisions.jev import JevAuthError
from app.decisions.rules import RulesDecisionProvider
from app.domain import replies
from app.domain.commands import Routed, route
from app.domain.conflict import ConstraintChanges, drop_kind, plan_changes
from app.domain.models import (
    ACTIVE_STATUSES,
    Constraint,
    ConstraintKind,
    InboundMessage,
    OnboardingState,
    SessionStatus,
)
from app.domain.parsing import Extraction, extract, looks_food_related
from app.domain.sessions import check_transition
from app.messaging.base import MessagingProvider
from app.observability import Metrics
from app.persistence.repositories import Repo, iso
from app.restaurants.base import Geocoder, PlacesProvider, ProviderError
from app.security.consent import chat_allowed, is_setup_sender
from app.security.redaction import hash_sender, short_id

log = logging.getLogger("foodie")
_ACTIVE = {s.value for s in ACTIVE_STATUSES}


class Orchestrator:
    def __init__(
        self,
        settings: Settings,
        repo: Repo,
        messaging: MessagingProvider,
        decisions: DecisionProvider,
        places: PlacesProvider,
        geocoder: Geocoder,
        metrics: Metrics | None = None,
    ) -> None:
        from app.services.recommendations import RecommendationService
        from app.services.voting import VotingService

        self.s = settings
        self.repo = repo
        self.messaging = messaging
        self.decisions = decisions
        self.rules = RulesDecisionProvider(settings.max_radius_miles)
        self.places = places
        self.geocoder = geocoder
        self.metrics = metrics or Metrics()
        self.tz: tzinfo | None = ZoneInfo(settings.timezone) if settings.timezone else None
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._flush_lock = asyncio.Lock()
        self.timers: dict[str, asyncio.Task] = {}
        self.jobs: dict[str, asyncio.Task] = {}
        self.pending_remember: dict[tuple[str, str], tuple[list[Constraint], datetime]] = {}
        self.clarified: set[tuple[str, int]] = set()
        self.votes_cleared: set[str] = set()
        self.collect_deadline: dict[str, float] = {}
        self.recs = RecommendationService(self)
        self.voting = VotingService(self)

    # ---- helpers ----------------------------------------------------------------------------
    def now(self) -> datetime:
        return self.repo.clock()

    def local_now(self) -> datetime:
        return self.now().astimezone(self.tz) if self.tz else self.now().astimezone()

    def name(self, group) -> str:
        return group["agent_name"] if group else "Foodie AI"

    def personality(self, group) -> str:
        try:
            return json.loads(group["personality_json"]).get("style", "adaptive")
        except (ValueError, TypeError):
            return "adaptive"

    async def reply(self, chat_guid: str, text: str, session_id: str | None = None, version: int | None = None) -> None:
        self.repo.enqueue(chat_guid, text, self.s.outbox_ttl_seconds, session_id, version)
        await self.flush_outbox()

    async def flush_outbox(self) -> None:
        """Deliver pending messages. Each is re-validated against the live session version first."""
        async with self._flush_lock:
            for row in self.repo.pending_outbox():
                claimed = self.repo.claim_outbox(row["id"])
                if claimed is None:
                    self.metrics.incr("outbox_discarded_stale")
                    continue
                start = time.perf_counter()
                result = await self.messaging.send(claimed["chat_guid"], claimed["payload"], claimed["id"])
                self.metrics.observe("send", time.perf_counter() - start)
                if result.ok:
                    self.repo.outbox_set(claimed["id"], "sent", result.provider_message_id)
                    if result.provider_message_id:
                        # Outgoing marker: an echo of our own message is never processed.
                        self.repo.mark_processed(f"bb:{result.provider_message_id}")
                    self.metrics.incr("outbox_sent")
                elif result.uncertain:
                    self.repo.outbox_set(claimed["id"], "uncertain")
                    self.metrics.incr("outbox_uncertain")
                    log.warning("send uncertain outbox=%s; not retrying automatically", claimed["id"][:8])
                elif result.retryable and claimed["attempts"] + 1 < self.s.outbox_max_attempts:
                    self.repo.outbox_set(claimed["id"], "pending")
                    self.metrics.incr("outbox_retry")
                else:
                    self.repo.outbox_set(claimed["id"], "failed")
                    self.metrics.incr("outbox_failed")

    def active_session(self, group_id: str):
        s = self.repo.active_session(group_id)
        if s and s["expires_at"] <= iso(self.now()):
            self.repo.end_session(s["id"], SessionStatus.EXPIRED, "timeout")
            self._cancel(group_id)
            return None
        return s

    def _cancel(self, group_id: str) -> None:
        for d in (self.timers, self.jobs):
            t = d.pop(group_id, None)
            if t and not t.done():
                t.cancel()

    async def drain(self) -> None:
        """Wait for scheduled searches and jobs (tests / demo)."""
        while True:
            tasks = [t for t in (*self.timers.values(), *self.jobs.values()) if not t.done()]
            if not tasks:
                return
            await asyncio.gather(*tasks, return_exceptions=True)

    # ---- lifecycle --------------------------------------------------------------------------
    def recover(self) -> dict:
        """On startup: expire stale/in-flight work instead of replaying it."""
        expired = self.repo.expire_stale(on_restart=True)
        uncertain, dropped = self.repo.recover_outbox()
        self.repo.audit(None, "startup_recovery", expired_sessions=expired, uncertain_sends=uncertain, dropped_outbox=dropped)
        return {"expired_sessions": expired, "uncertain_sends": uncertain, "dropped_outbox": dropped}

    async def tick(self) -> None:
        """Periodic housekeeping: idle timeouts, poll deadlines, retention, outbox retries."""
        for row in self.repo.db.query("SELECT DISTINCT group_id FROM sessions WHERE status IN ('collecting','searching','shortlist','voting')"):
            self.active_session(row[0])
        for poll in self.repo.due_polls():
            group = self.repo.group(poll["group_id"])
            if group:
                async with self._locks[group["chat_guid"]]:
                    await self.voting.close_personality_polls(group, reason="time")
        self.repo.purge_retention(self.s.retention_minutes)
        cutoff = self.now()
        self.pending_remember = {k: v for k, v in self.pending_remember.items() if v[1] > cutoff}
        await self.flush_outbox()

    # ---- admission --------------------------------------------------------------------------
    async def handle(self, msg: InboundMessage) -> str:
        start = time.perf_counter()
        try:
            outcome = await self._admit(msg)
        finally:
            self.metrics.observe("admission_total", time.perf_counter() - start)
        self.metrics.incr(f"outcome_{outcome}")
        return outcome

    async def _admit(self, msg: InboundMessage) -> str:
        if msg.is_from_me:
            return "self"
        if not chat_allowed(self.s, msg.chat_guid):
            return "not_allowed"
        group = self.repo.group_by_chat(msg.chat_guid)
        routed = route(msg.text, group["agent_name"] if group else None)
        # Only an active, onboarded session may look at non-wake messages.
        if not routed.woke and (
            not group
            or group["onboarding_state"] != OnboardingState.COMPLETE.value
            or not self.repo.active_session(group["id"])
        ):
            return "dormant"
        if not self.repo.mark_processed(msg.event_id):
            return "duplicate"
        async with self._locks[msg.chat_guid]:
            group = self.repo.group_by_chat(msg.chat_guid)
            return await self._dispatch(msg, group, routed)

    async def _dispatch(self, msg: InboundMessage, group, routed: Routed) -> str:
        cmd = routed.command
        if group is None:
            if cmd == "setup" and is_setup_sender(self.s, msg.sender):
                return await self._setup(msg)
            return "ignored_not_setup"
        member = self.repo.member(group["id"], hash_sender(self.s.pepper, msg.sender))
        assert member is not None
        name = self.name(group)

        if cmd == "stop":
            return await self._stop(group)
        if cmd == "forget_me":
            return await self._forget_me(group, member)
        if cmd == "privacy":
            await self.reply(group["chat_guid"], replies.privacy_text(name, self.s.jev_active))
            return "privacy"
        if cmd == "help":
            await self.reply(group["chat_guid"], replies.help_text(name))
            return "help"
        if cmd == "erase_group":
            return await self._erase_group(group, msg)
        if cmd == "setup":
            if is_setup_sender(self.s, msg.sender) and group["onboarding_state"] == OnboardingState.DECLINED.value:
                return await self._setup(msg)
            if group["intro_sent_at"]:
                await self.reply(group["chat_guid"], f"I'm already set up here. Say \"{name} help\" for commands.")
                return "setup_repeat"
            return "ignored"

        state = group["onboarding_state"]
        if state != OnboardingState.COMPLETE.value:
            return await self._onboarding(group, member, routed, msg)

        session = self.active_session(group["id"])
        if cmd in ("city", "confirm", "skip_city") or (cmd == "radius" and not session):
            return await self._city_settings(group, member, routed)
        if cmd in ("rename", "restyle"):
            return await self.voting.open_personality_polls(group, {"rename": "nickname", "restyle": "personality"}[cmd])
        if cmd in ("name", "style"):
            return await self.voting.personality_vote(group, member, routed)
        if cmd == "close" and not (session and session["status"] in ("shortlist", "voting")) and self.repo.open_polls(group["id"]):
            if not is_setup_sender(self.s, msg.sender):
                await self.reply(group["chat_guid"], "Only an organizer can close the vote early; it also closes on its own.")
                return "poll_close_denied"
            await self.voting.close_personality_polls(group, reason="organizer")
            return "polls_closed"
        if cmd in ("remember", "confirm_remember"):
            return await self._remember(group, member, routed)
        if cmd == "reopen":
            return await self._contribute(group, member, None, "", woke=True, msg=msg, force_new=True)
        if cmd in ("vote", "vote_open", "close"):
            return await self.voting.restaurant_command(group, member, session, routed, msg)
        if cmd in ("more", "closer", "directions", "reviews"):
            return await self.recs.shortlist_command(group, session, routed)
        if cmd == "go":
            if session and session["status"] == SessionStatus.COLLECTING.value:
                self.schedule_search(group, 0)
                return "go"
            return "ignored"
        if cmd == "drop" and session:
            ids = drop_kind(self.repo.constraints(session["id"]), member["id"], routed.arg)
            if not ids:
                await self.reply(group["chat_guid"], "You don't have a requirement like that in this search.")
                return "drop_none"
            return await self._apply_changes(group, session, ConstraintChanges(remove_ids=ids), Extraction(), note_drop=True)
        if cmd == "radius" and session:
            ex = Extraction(constraints=[Constraint(ConstraintKind.RADIUS, f"{min(float(routed.arg), self.s.max_radius_miles):g}", hard=True)])
            return await self._contribute(group, member, session, "", woke=True, msg=msg, ex=ex)
        if cmd in ("agree", "decline"):
            self.repo.update_member(member["id"], session_consent=int(cmd == "agree"))
            self.repo.audit(group["id"], f"member_{cmd}")
            await self.reply(group["chat_guid"], "Thanks, noted." if cmd == "agree" else "Noted. I'll only read your messages when you start them with my name.")
            return f"member_{cmd}"
        if cmd is None:
            if not routed.woke and not (member["session_consent"] or (session and member["id"] in self.repo.participants(session["id"]))):
                return "ignored_no_consent"
            return await self._contribute(group, member, session, routed.body, woke=routed.woke, msg=msg)
        return "ignored"

    # ---- onboarding -------------------------------------------------------------------------
    async def _setup(self, msg: InboundMessage) -> str:
        group = self.repo.group_by_chat(msg.chat_guid) or self.repo.create_group(msg.chat_guid, self.s.default_radius_miles)
        if group["intro_sent_at"] and group["onboarding_state"] != OnboardingState.DECLINED.value:
            return "setup_repeat"
        with self.repo.db.tx() as c:
            # Conditional update: only one caller can win the once-only intro.
            n = c.execute(
                "UPDATE groups SET onboarding_state = ?, intro_sent_at = ? WHERE id = ? AND (intro_sent_at IS NULL OR onboarding_state = ?)",
                (OnboardingState.CONSENT.value, iso(self.now()), group["id"], OnboardingState.DECLINED.value),
            ).rowcount
            c.execute("UPDATE members SET declined = 0, session_consent = 0 WHERE group_id = ?", (group["id"],))
        if not n:
            return "setup_repeat"
        self.repo.audit(group["id"], "onboarding_started")
        places_live = self.s.places_provider == "osm"
        await self.reply(msg.chat_guid, replies.intro(group["agent_name"], self.s.jev_active, places_live, self.s.group_consent_min))
        return "intro"

    async def _onboarding(self, group, member, routed: Routed, msg: InboundMessage) -> str:
        state = group["onboarding_state"]
        cmd = routed.command
        if state == OnboardingState.CONSENT.value:
            if cmd == "agree":
                self.repo.update_member(member["id"], session_consent=1, declined=0)
                count = self.repo.count_consented(group["id"])
                if count >= self.s.group_consent_min:
                    self.repo.update_group(group["id"], onboarding_state=OnboardingState.POLLS.value)
                    self.repo.audit(group["id"], "group_consent", consented=count)
                    return await self.voting.open_personality_polls(group, "both", onboarding=True)
                await self.reply(group["chat_guid"], f"Thanks, noted. Waiting for {self.s.group_consent_min - count} more to agree.")
                return "consent_partial"
            if cmd == "decline":
                self.repo.update_member(member["id"], declined=1, session_consent=0)
                self.repo.update_group(group["id"], onboarding_state=OnboardingState.DECLINED.value)
                self.repo.audit(group["id"], "group_declined")
                await self.reply(group["chat_guid"], "Understood. I won't take part here. An organizer can run setup again later.")
                return "declined"
            return "ignored_onboarding"
        if state == OnboardingState.POLLS.value:
            if cmd in ("name", "style"):
                return await self.voting.personality_vote(group, member, routed)
            if cmd == "close":
                if not is_setup_sender(self.s, msg.sender):
                    await self.reply(group["chat_guid"], "Only an organizer can close the vote early; it also closes on its own.")
                    return "poll_close_denied"
                await self.voting.close_personality_polls(group, reason="organizer")
                return "polls_closed"
            if cmd == "agree":
                self.repo.update_member(member["id"], session_consent=1)
                return "consent_late"
            return "ignored_onboarding"
        if state == OnboardingState.CITY.value:
            if cmd in ("city", "confirm", "skip_city", "radius"):
                return await self._city_settings(group, member, routed)
            if cmd == "agree":
                self.repo.update_member(member["id"], session_consent=1)
                return "consent_late"
            return "ignored_onboarding"
        if state == OnboardingState.DECLINED.value:
            return "ignored_declined"
        return "ignored_onboarding"

    async def _city_settings(self, group, member, routed: Routed) -> str:
        name = self.name(group)
        chat = group["chat_guid"]
        cmd = routed.command
        if cmd == "skip_city":
            if group["onboarding_state"] == OnboardingState.CITY.value:
                self.repo.update_group(group["id"], onboarding_state=OnboardingState.COMPLETE.value, onboarded=1)
                await self.reply(chat, replies.setup_done(name))
                return "setup_complete"
            return "ignored"
        if cmd == "city":
            try:
                point = await self.geocoder.geocode(routed.arg)
            except ProviderError:
                await self.reply(chat, "I can't reach the map lookup right now. Try again in a minute.")
                return "geocode_error"
            if not point:
                await self.reply(chat, f"I couldn't find \"{routed.arg[:40]}\". Try \"{name} city Tempe, AZ\".")
                return "geocode_miss"
            radius = group["pending_radius_miles"] or group["default_radius_miles"] or self.s.default_radius_miles
            self.repo.update_group(group["id"], pending_city=point.label, pending_lat=point.lat, pending_lon=point.lon, pending_radius_miles=radius)
            await self.reply(chat, replies.confirm_city(name, point.label, radius))
            return "city_pending"
        if cmd == "radius":
            radius = min(float(routed.arg), self.s.max_radius_miles)
            if radius <= 0:
                return "ignored"
            self.repo.update_group(group["id"], pending_radius_miles=radius)
            if group["pending_city"]:
                await self.reply(chat, replies.confirm_city(name, group["pending_city"], radius))
            return "radius_pending"
        if cmd == "confirm":
            if not group["pending_city"]:
                return "ignored"
            radius = group["pending_radius_miles"] or self.s.default_radius_miles
            fields = dict(
                default_city=group["pending_city"], default_lat=group["pending_lat"], default_lon=group["pending_lon"],
                default_radius_miles=radius, pending_city=None, pending_lat=None, pending_lon=None, pending_radius_miles=None,
                settings_version=group["settings_version"] + 1,
            )
            if group["onboarding_state"] == OnboardingState.CITY.value:
                fields.update(onboarding_state=OnboardingState.COMPLETE.value, onboarded=1)
            self.repo.update_group(group["id"], **fields)
            self.repo.audit(group["id"], "default_city_saved")
            if group["onboarding_state"] == OnboardingState.CITY.value:
                await self.reply(chat, f"Saved {group['pending_city'].split(',')[0]} ({radius:g} mi). " + replies.setup_done(name))
                return "setup_complete"
            await self.reply(chat, f"Saved {group['pending_city'].split(',')[0]} as the usual area ({radius:g} mi).")
            return "city_saved"
        return "ignored"

    # ---- privacy ----------------------------------------------------------------------------
    async def _stop(self, group) -> str:
        session = self.repo.active_session(group["id"])
        self._cancel(group["id"])
        if session:
            self.repo.end_session(session["id"], SessionStatus.STOPPED, "stop")
            self.repo.audit(group["id"], "session_stopped")
            await self.reply(group["chat_guid"], "Stopped. Call me again when you're ready.")
            return "stopped"
        await self.reply(group["chat_guid"], "Nothing is running. Call me when you're ready.")
        return "stop_idle"

    async def _forget_me(self, group, member) -> str:
        try:
            prefs, cons = self.repo.forget_member(member["id"])
        except Exception:
            log.exception("forget_me failed")
            await self.reply(group["chat_guid"], "Sorry, I couldn't delete your data just now. Please try again.")
            return "forget_failed"
        self.pending_remember.pop((group["id"], member["id"]), None)
        self.repo.audit(group["id"], "forget_me", preferences=prefs, constraints=cons)
        session = self.active_session(group["id"])
        if session and cons:
            self.repo.bump_version(session["id"], SessionStatus.COLLECTING)
            self.schedule_search(group, self.s.debounce_seconds)
        await self.reply(group["chat_guid"], "I've deleted your saved preferences for this group.")
        return "forgot"

    async def _erase_group(self, group, msg: InboundMessage) -> str:
        if not is_setup_sender(self.s, msg.sender):
            await self.reply(group["chat_guid"], "Only an organizer can erase this group's data.")
            return "erase_denied"
        self._cancel(group["id"])
        self.repo.delete_group(group["id"])
        self.repo.audit(None, "group_erased", chat=short_id(group["chat_guid"]))
        await self.reply(group["chat_guid"], "Erased this group's settings and saved data. Run setup again to use me.")
        return "erased"

    async def _remember(self, group, member, routed: Routed) -> str:
        chat, name = group["chat_guid"], self.name(group)
        key = (group["id"], member["id"])
        if routed.command == "confirm_remember":
            pending = self.pending_remember.pop(key, None)
            if not pending:
                return "ignored"
            self.repo.update_member(member["id"], memory_consent=1)
            for c in pending[0]:
                self.repo.add_preference(member["id"], c.kind.value, c.value)
            self.repo.audit(group["id"], "preference_saved", count=len(pending[0]))
            await self.reply(chat, "Saved. I'll apply it when you join a search.")
            return "remembered"
        ex = extract(routed.arg, now=self.local_now(), max_radius=self.s.max_radius_miles)
        keep = [c for c in ex.constraints if c.kind in (ConstraintKind.DIETARY, ConstraintKind.ALLERGY, ConstraintKind.AVOID, ConstraintKind.CUISINE)]
        if not keep:
            await self.reply(chat, "I can remember dietary needs, allergies, foods to avoid, or favorite cuisines.")
            return "remember_unparsed"
        summary = ", ".join(f"{c.kind.value}: {c.value.replace('_', ' ')}" for c in keep)
        if member["memory_consent"]:
            for c in keep:
                self.repo.add_preference(member["id"], c.kind.value, c.value)
            await self.reply(chat, f"Saved ({summary}).")
            return "remembered"
        self.pending_remember[key] = (keep, self.now() + timedelta(minutes=10))
        await self.reply(chat, f"I'd save {summary} for you in this group until you say \"{name} forget me\". Reply \"{name} confirm remember\" to allow.")
        return "remember_pending"

    # ---- sessions & constraints -------------------------------------------------------------
    def _ensure_participant(self, session_id: str, member) -> bool:
        """Adds the member; applies their opt-in saved preferences the first time they join."""
        if not self.repo.add_participant(session_id, member["id"]):
            return False
        if member["memory_consent"]:
            saved = []
            for p in self.repo.preferences(member["id"]):
                kind = ConstraintKind(p["category"])
                saved.append(Constraint(kind, p["value"], hard=kind != ConstraintKind.CUISINE, member_id=member["id"]))
            if saved:
                self.repo.apply_constraint_changes(session_id, saved, [], source="saved")
                return True
        return False

    async def _contribute(self, group, member, session, text: str, *, woke: bool, msg: InboundMessage,
                          ex: Extraction | None = None, force_new: bool = False) -> str:
        chat, name = group["chat_guid"], self.name(group)
        if ex is None:
            ex = extract(text, now=self.local_now(), max_radius=self.s.max_radius_miles)

        if session is None:
            if not woke:
                return "dormant"
            last = self.repo.last_ended_session(group["id"])
            if not force_new and last and last["status"] == SessionStatus.DONE.value and last["selected_recommendation_id"] \
                    and _only_diet(ex) and last["ended_at"] > iso(self.now() - timedelta(minutes=self.s.retention_minutes)):
                return await self.voting.late_joiner(group, last, ex)
            prefix = ""
            if last and last["end_reason"] in ("timeout", "restart") and not last["expiry_notice_sent"] \
                    and last["ended_at"] > iso(self.now() - timedelta(hours=24)):
                prefix = "I'm back. The earlier search expired, so I'm starting fresh. "
                self.repo.touch_ended(last["id"])
            session = self.repo.create_session(group["id"], self.s.session_idle_minutes)
            self.repo.audit(group["id"], "session_started")
            self._ensure_participant(session["id"], member)
            changes = plan_changes(self.repo.constraints(session["id"]), ex, member["id"])
            self.repo.apply_constraint_changes(session["id"], changes.add, changes.remove_ids)
            self._set_location(session["id"], ex, group)
            session = self.repo.session(session["id"])
            label = self._location_label(session, group)
            if not label:
                await self.reply(chat, prefix + "What area should I search? Say something like \"near Tempe\" or share a Maps link.", session["id"], session["version"])
                return "session_started_need_location"
            wait = f" I'll search in {self.s.collect_seconds:g} seconds, or say \"{name} go\"." if self.s.collect_seconds >= 1 else ""
            await self.reply(
                chat,
                prefix + replies.ack(self.personality(group), f"I'll look around {label.split(',')[0]}. Any dietary needs, budget, or cuisine?{wait}"),
                session["id"], session["version"],
            )
            self.collect_deadline[group["id"]] = asyncio.get_running_loop().time() + self.s.collect_seconds
            self.schedule_search(group, self.s.collect_seconds)
            return "session_started"

        joined_with_saved = self._ensure_participant(session["id"], member)
        if ex.empty:
            if joined_with_saved:
                return await self._apply_changes(group, session, ConstraintChanges(), ex, force=True)
            return await self._ambiguous(group, session, text, woke)
        changes = plan_changes(self.repo.constraints(session["id"]), ex, member["id"])
        if changes.protected:
            kinds = sorted({s.constraint.kind.value for s in changes.protected})
            await self.reply(chat, f"I kept someone's hard {', '.join(kinds)} limit; only that person can change it (\"{name} drop my {kinds[0]}\").")
        return await self._apply_changes(group, session, changes, ex, force=joined_with_saved)

    async def _apply_changes(self, group, session, changes: ConstraintChanges, ex: Extraction, *, note_drop: bool = False, force: bool = False) -> str:
        location_changed = self._set_location(session["id"], ex, group)
        if changes.changed:
            self.repo.apply_constraint_changes(session["id"], changes.add, changes.remove_ids)
        if not (changes.changed or location_changed or force):
            self.repo.touch_session(session["id"], self.s.session_idle_minutes)
            return "no_change"
        prior = session["status"]
        check_transition(prior, SessionStatus.COLLECTING)
        if prior in (SessionStatus.SHORTLIST.value, SessionStatus.VOTING.value) and self.repo.voters(session["id"], session["shortlist_version"]):
            self.votes_cleared.add(session["id"])
        version = self.repo.bump_version(session["id"], SessionStatus.COLLECTING)
        self.repo.touch_session(session["id"], self.s.session_idle_minutes)
        session = self.repo.session(session["id"])
        if not self._location_label(session, group):
            await self.reply(group["chat_guid"], "What area should I search? Say something like \"near Tempe\" or share a Maps link.", session["id"], version)
            return "need_location"
        # Coalesce bursts with a short debounce, but never search before the first
        # collection window (opened on wake) has elapsed.
        loop = asyncio.get_running_loop()
        delay = max(self.s.debounce_seconds, self.collect_deadline.get(group["id"], 0) - loop.time())
        self.schedule_search(group, delay)
        self.metrics.incr("constraint_updates")
        return "updated"

    async def _ambiguous(self, group, session, text: str, woke: bool) -> str:
        if not woke and not looks_food_related(text):
            return "ignored_unrelated"  # never forwarded to Jev
        decision = await self.decide(session, text)
        name = self.name(group)
        key = (session["id"], session["version"])
        if decision.intent == "vote":
            await self.reply(group["chat_guid"], f"To vote, say \"{name} vote 1\" (or 2, 3).")
            return "vote_hint"
        if decision.next_action == "ask_one_question" or (woke and decision.intent in ("clarify", "search", "modify")):
            if key in self.clarified:
                return "clarify_suppressed"
            self.clarified.add(key)
            await self.reply(group["chat_guid"], "What should I change: cuisine, budget, distance, or area?")
            return "clarify"
        return "ignored_ambiguous"

    async def decide(self, session, text: str) -> Decision:
        cons = self.repo.constraints(session["id"])
        snapshot = {
            "session_id": session["id"],
            "status": session["status"],
            "has_location": bool(session["lat"] or session["location_label"]),
            "cuisines": sorted({c.constraint.value for c in cons if c.constraint.kind == ConstraintKind.CUISINE}),
            "has_shortlist": bool(session["shortlist_version"]),
            "constraint_count": len(cons),
        }
        start = time.perf_counter()
        try:
            decision = await self.decisions.decide(snapshot, text)
        except JevAuthError:
            self.repo.audit(None, "jev_auth_failed")
            decision = Decision.unknown("fallback", "auth_error")
        self.metrics.observe("classification", time.perf_counter() - start)
        if decision.source == "jev":
            self.metrics.incr("jev_requests")
        if decision.source == "fallback":
            self.metrics.incr("decision_fallback")
            decision = await self.rules.decide(snapshot, text)
        return decision

    def _set_location(self, session_id: str, ex: Extraction, group) -> bool:
        if ex.location_point:
            p = ex.location_point
            self.repo.set_session_location(session_id, p.label, p.lat, p.lon)
            return True
        if ex.location_text:
            text = ex.location_text
            if group["default_city"] and text.lower().split(",")[0] == group["default_city"].lower().split(",")[0]:
                self.repo.set_session_location(session_id, group["default_city"], group["default_lat"], group["default_lon"])
            else:
                self.repo.set_session_location(session_id, text, None, None)
            return True
        return False

    @staticmethod
    def _location_label(session, group) -> str | None:
        return session["location_label"] or (group["default_city"] if group["default_lat"] is not None else None)

    def schedule_search(self, group, delay: float) -> None:
        gid = group["id"]
        old = self.timers.pop(gid, None)
        if old and not old.done():
            old.cancel()
        self.timers[gid] = asyncio.create_task(self._delayed_search(group["chat_guid"], gid, delay))

    async def _delayed_search(self, chat_guid: str, group_id: str, delay: float) -> None:
        if delay > 0:
            await asyncio.sleep(delay)
        await asyncio.shield(self._start_search(chat_guid, group_id))

    async def _start_search(self, chat_guid: str, group_id: str) -> None:
        async with self._locks[chat_guid]:
            session = self.active_session(group_id)
            if not session or session["status"] != SessionStatus.COLLECTING.value:
                return
            check_transition(session["status"], SessionStatus.SEARCHING)
            self.repo.set_status(session["id"], SessionStatus.SEARCHING)
            version = session["version"]
            old = self.jobs.pop(group_id, None)
            if old and not old.done():
                old.cancel()  # best effort; correctness relies on the version check
                self.metrics.incr("jobs_superseded")
            self.jobs[group_id] = asyncio.create_task(self.recs.run_search(group_id, session["id"], version))


def _only_diet(ex: Extraction) -> bool:
    kinds = {c.kind for c in ex.constraints}
    return bool(kinds) and kinds <= {ConstraintKind.DIETARY, ConstraintKind.ALLERGY} and not ex.location_text and not ex.location_point

