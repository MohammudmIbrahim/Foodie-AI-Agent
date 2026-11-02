-- Foodie AI initial schema. No raw message bodies are stored anywhere.

CREATE TABLE groups (
    id TEXT PRIMARY KEY,
    chat_guid TEXT NOT NULL UNIQUE,
    agent_name TEXT NOT NULL DEFAULT 'Foodie AI',
    personality_json TEXT NOT NULL DEFAULT '{"style":"adaptive"}',
    default_city TEXT,
    default_lat REAL,
    default_lon REAL,
    default_radius_miles REAL NOT NULL DEFAULT 5,
    pending_city TEXT,
    pending_lat REAL,
    pending_lon REAL,
    pending_radius_miles REAL,
    onboarding_state TEXT NOT NULL DEFAULT 'none',
    onboarded INTEGER NOT NULL DEFAULT 0,
    intro_sent_at TEXT,
    settings_version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE members (
    id TEXT PRIMARY KEY,
    group_id TEXT NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    sender_hash TEXT NOT NULL,
    session_consent INTEGER NOT NULL DEFAULT 0,
    memory_consent INTEGER NOT NULL DEFAULT 0,
    declined INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    UNIQUE (group_id, sender_hash)
);

CREATE TABLE preferences (
    id TEXT PRIMARY KEY,
    member_id TEXT NOT NULL REFERENCES members(id) ON DELETE CASCADE,
    category TEXT NOT NULL,
    value TEXT NOT NULL,
    source_command TEXT NOT NULL DEFAULT 'remember',
    asserted_at TEXT NOT NULL,
    UNIQUE (member_id, category, value)
);

CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    group_id TEXT NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    status TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    shortlist_version INTEGER NOT NULL DEFAULT 0,
    shortlist_sent_at TEXT,
    location_label TEXT,
    lat REAL,
    lon REAL,
    tiebreak_sent INTEGER NOT NULL DEFAULT 0,
    selected_recommendation_id TEXT,
    end_reason TEXT,
    expiry_notice_sent INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    ended_at TEXT
);
-- Single active dining session per group (chat GUID).
CREATE UNIQUE INDEX one_active_session ON sessions(group_id)
    WHERE status IN ('collecting', 'searching', 'shortlist', 'voting');

CREATE TABLE session_participants (
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    member_id TEXT NOT NULL REFERENCES members(id) ON DELETE CASCADE,
    joined_at TEXT NOT NULL,
    PRIMARY KEY (session_id, member_id)
);

CREATE TABLE session_constraints (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    member_id TEXT NOT NULL REFERENCES members(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    value TEXT NOT NULL,
    hard INTEGER NOT NULL,
    source TEXT NOT NULL DEFAULT 'message',
    created_at TEXT NOT NULL,
    seq INTEGER NOT NULL
);

CREATE TABLE recommendations (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    session_version INTEGER NOT NULL,
    shortlist_version INTEGER,
    option_number INTEGER,
    provider TEXT NOT NULL,
    place_id TEXT NOT NULL,
    rank INTEGER NOT NULL,
    place_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX recs_by_session ON recommendations(session_id, shortlist_version);

CREATE TABLE votes (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    member_id TEXT NOT NULL REFERENCES members(id) ON DELETE CASCADE,
    recommendation_id TEXT NOT NULL REFERENCES recommendations(id) ON DELETE CASCADE,
    shortlist_version INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (session_id, shortlist_version, member_id)
);

CREATE TABLE personality_polls (
    id TEXT PRIMARY KEY,
    group_id TEXT NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK (kind IN ('nickname', 'personality')),
    status TEXT NOT NULL CHECK (status IN ('open', 'closed')),
    closes_at TEXT NOT NULL,
    result TEXT,
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX one_open_poll_per_kind ON personality_polls(group_id, kind) WHERE status = 'open';

CREATE TABLE personality_votes (
    id TEXT PRIMARY KEY,
    poll_id TEXT NOT NULL REFERENCES personality_polls(id) ON DELETE CASCADE,
    member_id TEXT NOT NULL REFERENCES members(id) ON DELETE CASCADE,
    choice TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (poll_id, member_id)
);

CREATE TABLE processed_events (
    event_id TEXT PRIMARY KEY,
    received_at TEXT NOT NULL
);

CREATE TABLE outbox (
    id TEXT PRIMARY KEY,
    chat_guid TEXT NOT NULL,
    session_id TEXT,
    session_version INTEGER,
    payload TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('pending', 'sending', 'sent', 'uncertain', 'discarded', 'failed')),
    attempts INTEGER NOT NULL DEFAULT 0,
    provider_message_id TEXT,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE INDEX outbox_pending ON outbox(state, created_at);

CREATE TABLE audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id TEXT,
    action TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
