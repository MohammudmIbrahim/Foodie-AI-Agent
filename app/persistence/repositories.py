"""Repository functions over SQLite. All timestamps are ISO-8601 UTC strings."""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from app.domain.conflict import StoredConstraint
from app.domain.models import (
    ACTIVE_STATUSES,
    Constraint,
    ConstraintKind,
    Place,
    RankedPlace,
    SessionStatus,
)
from app.persistence.db import Database

Clock = Callable[[], datetime]
_ACTIVE = tuple(s.value for s in ACTIVE_STATUSES)
_ACTIVE_SQL = "(" + ",".join("?" * len(_ACTIVE)) + ")"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _id() -> str:
    return uuid.uuid4().hex


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s)


class Repo:
    def __init__(self, db: Database, clock: Clock = utcnow) -> None:
        self.db = db
        self.clock = clock

    def now(self) -> str:
        return iso(self.clock())

    # ---- groups -----------------------------------------------------------------------------
    def group_by_chat(self, chat_guid: str) -> sqlite3.Row | None:
        return self.db.one("SELECT * FROM groups WHERE chat_guid = ?", (chat_guid,))

    def group(self, group_id: str) -> sqlite3.Row | None:
        return self.db.one("SELECT * FROM groups WHERE id = ?", (group_id,))

    def create_group(self, chat_guid: str, default_radius: float) -> sqlite3.Row:
        with self.db.tx() as c:
            c.execute(
                "INSERT OR IGNORE INTO groups(id, chat_guid, default_radius_miles, created_at) VALUES (?,?,?,?)",
                (_id(), chat_guid, default_radius, self.now()),
            )
        row = self.group_by_chat(chat_guid)
        assert row is not None
        return row

    def update_group(self, group_id: str, **fields: object) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self.db.tx() as c:
            c.execute(f"UPDATE groups SET {cols} WHERE id = ?", (*fields.values(), group_id))

    def delete_group(self, group_id: str) -> None:
        with self.db.tx() as c:
            c.execute("DELETE FROM outbox WHERE session_id IN (SELECT id FROM sessions WHERE group_id = ?)", (group_id,))
            c.execute("DELETE FROM groups WHERE id = ?", (group_id,))

    # ---- members ----------------------------------------------------------------------------
    def member(self, group_id: str, sender_hash: str, create: bool = True) -> sqlite3.Row | None:
        row = self.db.one("SELECT * FROM members WHERE group_id = ? AND sender_hash = ?", (group_id, sender_hash))
        if row or not create:
            return row
        with self.db.tx() as c:
            c.execute(
                "INSERT OR IGNORE INTO members(id, group_id, sender_hash, created_at) VALUES (?,?,?,?)",
                (_id(), group_id, sender_hash, self.now()),
            )
        return self.db.one("SELECT * FROM members WHERE group_id = ? AND sender_hash = ?", (group_id, sender_hash))

    def update_member(self, member_id: str, **fields: object) -> None:
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self.db.tx() as c:
            c.execute(f"UPDATE members SET {cols} WHERE id = ?", (*fields.values(), member_id))

    def count_consented(self, group_id: str) -> int:
        row = self.db.one("SELECT COUNT(*) FROM members WHERE group_id = ? AND session_consent = 1", (group_id,))
        return int(row[0]) if row else 0

    # ---- preferences (explicit opt-in memory) -----------------------------------------------
    def add_preference(self, member_id: str, category: str, value: str) -> None:
        with self.db.tx() as c:
            c.execute(
                "INSERT OR REPLACE INTO preferences(id, member_id, category, value, asserted_at) VALUES (?,?,?,?,?)",
                (_id(), member_id, category, value, self.now()),
            )

    def preferences(self, member_id: str) -> list[sqlite3.Row]:
        return self.db.query("SELECT * FROM preferences WHERE member_id = ? ORDER BY asserted_at", (member_id,))

    def forget_member(self, member_id: str) -> tuple[int, int]:
        """Delete one member's saved preferences and session constraints derived from them.

        What they said in the current session stays until the session ends (then it is cleared).
        """
        with self.db.tx() as c:
            prefs = c.execute("DELETE FROM preferences WHERE member_id = ?", (member_id,)).rowcount
            cons = c.execute("DELETE FROM session_constraints WHERE member_id = ? AND source = 'saved'", (member_id,)).rowcount
            c.execute("UPDATE members SET memory_consent = 0 WHERE id = ?", (member_id,))
        return prefs, cons

    # ---- sessions ---------------------------------------------------------------------------
    def active_session(self, group_id: str) -> sqlite3.Row | None:
        return self.db.one(
            f"SELECT * FROM sessions WHERE group_id = ? AND status IN {_ACTIVE_SQL}", (group_id, *_ACTIVE)
        )

    def session(self, session_id: str) -> sqlite3.Row | None:
        return self.db.one("SELECT * FROM sessions WHERE id = ?", (session_id,))

    def last_ended_session(self, group_id: str) -> sqlite3.Row | None:
        return self.db.one(
            f"SELECT * FROM sessions WHERE group_id = ? AND status NOT IN {_ACTIVE_SQL} ORDER BY ended_at DESC LIMIT 1",
            (group_id, *_ACTIVE),
        )

    def create_session(self, group_id: str, idle_minutes: float) -> sqlite3.Row:
        now = self.clock()
        sid = _id()
        with self.db.tx() as c:
            c.execute(
                "INSERT INTO sessions(id, group_id, status, created_at, updated_at, expires_at) VALUES (?,?,?,?,?,?)",
                (sid, group_id, SessionStatus.COLLECTING.value, iso(now), iso(now), iso(now + timedelta(minutes=idle_minutes))),
            )
        row = self.session(sid)
        assert row is not None
        return row

    def touch_session(self, session_id: str, idle_minutes: float, **fields: object) -> None:
        now = self.clock()
        fields = {**fields, "updated_at": iso(now), "expires_at": iso(now + timedelta(minutes=idle_minutes))}
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self.db.tx() as c:
            c.execute(f"UPDATE sessions SET {cols} WHERE id = ?", (*fields.values(), session_id))

    def bump_version(self, session_id: str, status: SessionStatus | None = None) -> int:
        """Every mutation to location/requirements/cuisine/radius increments the version."""
        with self.db.tx() as c:
            if status:
                c.execute("UPDATE sessions SET version = version + 1, status = ? WHERE id = ?", (status.value, session_id))
            else:
                c.execute("UPDATE sessions SET version = version + 1 WHERE id = ?", (session_id,))
            row = c.execute("SELECT version FROM sessions WHERE id = ?", (session_id,)).fetchone()
        return int(row[0])

    def set_status(self, session_id: str, status: SessionStatus) -> None:
        with self.db.tx() as c:
            c.execute("UPDATE sessions SET status = ? WHERE id = ?", (status.value, session_id))

    def end_session(self, session_id: str, status: SessionStatus, reason: str) -> bool:
        """Returns False when it was not active (already ended)."""
        with self.db.tx() as c:
            n = c.execute(
                f"UPDATE sessions SET status = ?, end_reason = ?, ended_at = ?, version = version + 1, "
                f"location_label = NULL, lat = NULL, lon = NULL "
                f"WHERE id = ? AND status IN {_ACTIVE_SQL}",
                (status.value, reason, self.now(), session_id, *_ACTIVE),
            ).rowcount
            if n:
                # Clear temporary data immediately; keep only the finalized selection.
                c.execute("DELETE FROM session_constraints WHERE session_id = ?", (session_id,))
                c.execute("UPDATE outbox SET state = 'discarded' WHERE session_id = ? AND state = 'pending'", (session_id,))
                if status != SessionStatus.DONE:
                    c.execute("DELETE FROM votes WHERE session_id = ?", (session_id,))
                    c.execute("DELETE FROM recommendations WHERE session_id = ?", (session_id,))
        return bool(n)

    def expire_stale(self, *, on_restart: bool = False) -> int:
        """Expire idle sessions; on restart also expire searches whose job died with the process."""
        now = self.now()
        rows = self.db.query(
            f"SELECT id, status, expires_at FROM sessions WHERE status IN {_ACTIVE_SQL}", _ACTIVE
        )
        n = 0
        for r in rows:
            stale = r["expires_at"] <= now or (on_restart and r["status"] == SessionStatus.SEARCHING.value)
            if stale and self.end_session(r["id"], SessionStatus.EXPIRED, "restart" if on_restart else "timeout"):
                n += 1
        return n

    def purge_retention(self, retention_minutes: float) -> None:
        cutoff = iso(self.clock() - timedelta(minutes=retention_minutes))
        week = iso(self.clock() - timedelta(days=7))
        with self.db.tx() as c:
            c.execute(
                f"DELETE FROM recommendations WHERE session_id IN (SELECT id FROM sessions WHERE status NOT IN {_ACTIVE_SQL} AND ended_at < ?)",
                (*_ACTIVE, cutoff),
            )
            c.execute(
                f"DELETE FROM sessions WHERE status NOT IN {_ACTIVE_SQL} AND ended_at < ? AND expiry_notice_sent = 1",
                (*_ACTIVE, week),
            )
            c.execute("DELETE FROM outbox WHERE created_at < ? AND state != 'pending'", (week,))
            c.execute("DELETE FROM processed_events WHERE received_at < ?", (iso(self.clock() - timedelta(days=30)),))

    # ---- participants & constraints ---------------------------------------------------------
    def add_participant(self, session_id: str, member_id: str) -> bool:
        with self.db.tx() as c:
            n = c.execute(
                "INSERT OR IGNORE INTO session_participants(session_id, member_id, joined_at) VALUES (?,?,?)",
                (session_id, member_id, self.now()),
            ).rowcount
        return bool(n)

    def participants(self, session_id: str) -> list[str]:
        return [r[0] for r in self.db.query("SELECT member_id FROM session_participants WHERE session_id = ?", (session_id,))]

    def constraints(self, session_id: str) -> list[StoredConstraint]:
        rows = self.db.query("SELECT * FROM session_constraints WHERE session_id = ? ORDER BY seq", (session_id,))
        return [
            StoredConstraint(r["id"], Constraint(ConstraintKind(r["kind"]), r["value"], bool(r["hard"]), r["member_id"]))
            for r in rows
        ]

    def apply_constraint_changes(self, session_id: str, add: list[Constraint], remove_ids: list[str], source: str = "message") -> None:
        with self.db.tx() as c:
            for cid in remove_ids:
                c.execute("DELETE FROM session_constraints WHERE id = ? AND session_id = ?", (cid, session_id))
            seq = c.execute("SELECT COALESCE(MAX(seq), 0) FROM session_constraints WHERE session_id = ?", (session_id,)).fetchone()[0]
            for i, con in enumerate(add, start=1):
                c.execute(
                    "INSERT INTO session_constraints(id, session_id, member_id, kind, value, hard, source, created_at, seq) VALUES (?,?,?,?,?,?,?,?,?)",
                    (_id(), session_id, con.member_id, con.kind.value, con.value, int(con.hard), source, self.now(), seq + i),
                )

    def revert_to_collecting(self, session_id: str, version: int) -> bool:
        """A failed/empty search returns to collecting only if nothing changed meanwhile."""
        with self.db.tx() as c:
            n = c.execute(
                "UPDATE sessions SET status = 'collecting' WHERE id = ? AND version = ? AND status = 'searching'",
                (session_id, version),
            ).rowcount
        return bool(n)

    def set_session_coords(self, session_id: str, version: int, lat: float, lon: float, label: str) -> bool:
        with self.db.tx() as c:
            n = c.execute(
                "UPDATE sessions SET lat = ?, lon = ?, location_label = ? WHERE id = ? AND version = ?",
                (lat, lon, label, session_id, version),
            ).rowcount
        return bool(n)

    def set_session_location(self, session_id: str, label: str | None, lat: float | None, lon: float | None) -> None:
        with self.db.tx() as c:
            c.execute("UPDATE sessions SET location_label = ?, lat = ?, lon = ? WHERE id = ?", (label, lat, lon, session_id))

    def touch_ended(self, session_id: str) -> None:
        with self.db.tx() as c:
            c.execute("UPDATE sessions SET expiry_notice_sent = 1 WHERE id = ?", (session_id,))

    # ---- recommendations & votes ------------------------------------------------------------
    def store_pool_and_shortlist(
        self, session_id: str, expected_version: int, pool: list[RankedPlace], shortlist_ids: list[str]
    ) -> int | None:
        """Atomically re-check version/status and persist results. None = stale, nothing stored."""
        with self.db.tx() as c:
            s = c.execute("SELECT version, status, shortlist_version FROM sessions WHERE id = ?", (session_id,)).fetchone()
            if not s or s["version"] != expected_version or s["status"] != SessionStatus.SEARCHING.value:
                return None
            slv = s["shortlist_version"] + 1
            c.execute("DELETE FROM votes WHERE session_id = ?", (session_id,))
            c.execute("DELETE FROM recommendations WHERE session_id = ?", (session_id,))
            for rank, r in enumerate(pool, start=1):
                pid = r.place.provider_place_id
                option = shortlist_ids.index(pid) + 1 if pid in shortlist_ids else None
                c.execute(
                    "INSERT INTO recommendations(id, session_id, session_version, shortlist_version, option_number, provider, place_id, rank, place_json, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (_id(), session_id, expected_version, slv if option else None, option, r.place.provider, pid, rank, _ranked_json(r), self.now()),
                )
            c.execute(
                "UPDATE sessions SET status = ?, shortlist_version = ?, shortlist_sent_at = ? WHERE id = ?",
                (SessionStatus.SHORTLIST.value, slv, self.now(), session_id),
            )
        return slv

    def reassign_shortlist(self, session_id: str, place_ids: list[str]) -> int:
        """`more`/`closer`: new option numbers in a new shortlist version (old votes cleared)."""
        with self.db.tx() as c:
            slv = c.execute("SELECT shortlist_version FROM sessions WHERE id = ?", (session_id,)).fetchone()[0] + 1
            c.execute("UPDATE recommendations SET option_number = NULL, shortlist_version = NULL WHERE session_id = ?", (session_id,))
            for i, pid in enumerate(place_ids, start=1):
                c.execute(
                    "UPDATE recommendations SET option_number = ?, shortlist_version = ? WHERE session_id = ? AND place_id = ?",
                    (i, slv, session_id, pid),
                )
            c.execute("DELETE FROM votes WHERE session_id = ?", (session_id,))
            c.execute(
                "UPDATE sessions SET shortlist_version = ?, shortlist_sent_at = ?, tiebreak_sent = 0, status = ? WHERE id = ?",
                (slv, self.now(), SessionStatus.SHORTLIST.value, session_id),
            )
        return slv

    def pool(self, session_id: str) -> list[sqlite3.Row]:
        return self.db.query("SELECT * FROM recommendations WHERE session_id = ? ORDER BY rank", (session_id,))

    def shortlist(self, session_id: str) -> list[sqlite3.Row]:
        s = self.session(session_id)
        if not s:
            return []
        return self.db.query(
            "SELECT * FROM recommendations WHERE session_id = ? AND shortlist_version = ? ORDER BY option_number",
            (session_id, s["shortlist_version"]),
        )

    def recommendation(self, rec_id: str) -> sqlite3.Row | None:
        return self.db.one("SELECT * FROM recommendations WHERE id = ?", (rec_id,))

    def cast_vote(self, session_id: str, member_id: str, rec_id: str, shortlist_version: int) -> None:
        with self.db.tx() as c:
            c.execute(
                "INSERT INTO votes(id, session_id, member_id, recommendation_id, shortlist_version, created_at) VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(session_id, shortlist_version, member_id) DO UPDATE SET recommendation_id = excluded.recommendation_id, created_at = excluded.created_at",
                (_id(), session_id, member_id, rec_id, shortlist_version, self.now()),
            )

    def tally(self, session_id: str, shortlist_version: int) -> dict[int, int]:
        rows = self.db.query(
            "SELECT r.option_number AS opt, COUNT(*) AS n FROM votes v JOIN recommendations r ON r.id = v.recommendation_id "
            "WHERE v.session_id = ? AND v.shortlist_version = ? GROUP BY r.option_number",
            (session_id, shortlist_version),
        )
        return {int(r["opt"]): int(r["n"]) for r in rows}

    def voters(self, session_id: str, shortlist_version: int) -> int:
        row = self.db.one("SELECT COUNT(*) FROM votes WHERE session_id = ? AND shortlist_version = ?", (session_id, shortlist_version))
        return int(row[0]) if row else 0

    # ---- personality / nickname polls -------------------------------------------------------
    def open_poll(self, group_id: str, kind: str, minutes: float) -> str:
        pid = _id()
        closes = iso(self.clock() + timedelta(minutes=minutes))
        with self.db.tx() as c:
            existing = c.execute("SELECT id FROM personality_polls WHERE group_id = ? AND kind = ? AND status = 'open'", (group_id, kind)).fetchone()
            if existing:
                return existing[0]
            c.execute(
                "INSERT INTO personality_polls(id, group_id, kind, status, closes_at, created_at) VALUES (?,?,?,?,?,?)",
                (pid, group_id, kind, "open", closes, self.now()),
            )
        return pid

    def open_polls(self, group_id: str) -> list[sqlite3.Row]:
        return self.db.query("SELECT * FROM personality_polls WHERE group_id = ? AND status = 'open' ORDER BY kind", (group_id,))

    def due_polls(self) -> list[sqlite3.Row]:
        return self.db.query("SELECT * FROM personality_polls WHERE status = 'open' AND closes_at <= ?", (self.now(),))

    def poll_vote(self, poll_id: str, member_id: str, choice: str) -> None:
        with self.db.tx() as c:
            c.execute(
                "INSERT INTO personality_votes(id, poll_id, member_id, choice, created_at) VALUES (?,?,?,?,?) "
                "ON CONFLICT(poll_id, member_id) DO UPDATE SET choice = excluded.choice",
                (_id(), poll_id, member_id, choice, self.now()),
            )

    def poll_choices(self, poll_id: str) -> list[str]:
        return [r[0] for r in self.db.query("SELECT choice FROM personality_votes WHERE poll_id = ?", (poll_id,))]

    def close_poll(self, poll_id: str, result: str | None) -> bool:
        with self.db.tx() as c:
            n = c.execute("UPDATE personality_polls SET status = 'closed', result = ? WHERE id = ? AND status = 'open'", (result, poll_id)).rowcount
        return bool(n)

    # ---- events, outbox, audit --------------------------------------------------------------
    def mark_processed(self, event_id: str) -> bool:
        """True if this event is new. Persisted, so duplicates are rejected across restarts."""
        with self.db.tx() as c:
            n = c.execute("INSERT OR IGNORE INTO processed_events(event_id, received_at) VALUES (?,?)", (event_id, self.now())).rowcount
        return bool(n)

    def enqueue(self, chat_guid: str, text: str, ttl_seconds: int, session_id: str | None = None, session_version: int | None = None) -> str:
        oid = _id()
        with self.db.tx() as c:
            c.execute(
                "INSERT INTO outbox(id, chat_guid, session_id, session_version, payload, state, created_at, expires_at) VALUES (?,?,?,?,?,?,?,?)",
                (oid, chat_guid, session_id, session_version, text, "pending", self.now(), iso(self.clock() + timedelta(seconds=ttl_seconds))),
            )
        return oid

    def claim_outbox(self, outbox_id: str) -> sqlite3.Row | None:
        """Re-validate session version/status in a transaction right before sending."""
        with self.db.tx() as c:
            row = c.execute("SELECT * FROM outbox WHERE id = ? AND state = 'pending'", (outbox_id,)).fetchone()
            if not row:
                return None
            reason = None
            if row["expires_at"] <= self.now():
                reason = "expired"
            elif row["session_id"]:
                s = c.execute("SELECT version, status FROM sessions WHERE id = ?", (row["session_id"],)).fetchone()
                if not s or s["version"] != row["session_version"] or s["status"] not in _ACTIVE + (SessionStatus.DONE.value,):
                    reason = "stale"
            if reason:
                c.execute("UPDATE outbox SET state = 'discarded' WHERE id = ?", (outbox_id,))
                return None
            c.execute("UPDATE outbox SET state = 'sending', attempts = attempts + 1 WHERE id = ?", (outbox_id,))
            return row

    def outbox_set(self, outbox_id: str, state: str, provider_message_id: str | None = None) -> None:
        with self.db.tx() as c:
            c.execute("UPDATE outbox SET state = ?, provider_message_id = COALESCE(?, provider_message_id) WHERE id = ?", (state, provider_message_id, outbox_id))

    def pending_outbox(self) -> list[sqlite3.Row]:
        return self.db.query("SELECT * FROM outbox WHERE state = 'pending' ORDER BY created_at, rowid")

    def recover_outbox(self) -> tuple[int, int]:
        """After a crash: in-flight sends become 'uncertain' (never blindly resent)."""
        with self.db.tx() as c:
            unc = c.execute("UPDATE outbox SET state = 'uncertain' WHERE state = 'sending'").rowcount
            exp = c.execute("UPDATE outbox SET state = 'discarded' WHERE state = 'pending' AND expires_at <= ?", (self.now(),)).rowcount
        return unc, exp

    def audit(self, group_id: str | None, action: str, **detail: object) -> None:
        """Non-content metadata only: IDs, counts, states. Never message text."""
        with self.db.tx() as c:
            c.execute(
                "INSERT INTO audit_events(group_id, action, detail_json, created_at) VALUES (?,?,?,?)",
                (group_id, action, json.dumps(detail, sort_keys=True), self.now()),
            )


def _ranked_json(r: RankedPlace) -> str:
    return json.dumps(
        {
            "place": r.place.to_dict(), "score": r.score, "distance_miles": r.distance_miles,
            "confidence": r.confidence, "unverified": r.unverified, "reasons": r.reasons,
            "hours_note": r.hours_note, "open_at_arrival": r.open_at_arrival, "matched_cuisines": r.matched_cuisines,
        }
    )


def ranked_from_row(row: sqlite3.Row) -> RankedPlace:
    d = json.loads(row["place_json"])
    return RankedPlace(
        place=Place.from_dict(d["place"]), score=d["score"], distance_miles=d["distance_miles"],
        confidence=d["confidence"], unverified=d["unverified"], reasons=d["reasons"],
        hours_note=d["hours_note"], open_at_arrival=d["open_at_arrival"], matched_cuisines=d["matched_cuisines"],
    )
