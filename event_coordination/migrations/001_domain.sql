CREATE TABLE events (
    id TEXT PRIMARY KEY,
    author_id TEXT NOT NULL,
    title TEXT NOT NULL CHECK (length(trim(title)) > 0),
    place TEXT NOT NULL,
    starts_at TEXT NOT NULL,
    territory_id TEXT NOT NULL,
    direction_id TEXT NOT NULL,
    purpose TEXT NOT NULL,
    needed_help TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN (
        'submitted', 'approved', 'preparing', 'ongoing',
        'completed', 'archived', 'rejected', 'cancelled'
    )),
    owner_id TEXT,
    reason TEXT,
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (owner_id IS NULL OR length(trim(owner_id)) > 0),
    CHECK (status NOT IN ('approved','preparing','ongoing','completed','archived')
        OR owner_id IS NOT NULL)
);
CREATE INDEX events_author ON events(author_id);
CREATE INDEX events_status ON events(status);
CREATE TABLE participation (
    event_id TEXT NOT NULL REFERENCES events(id),
    user_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN (
        'requested','invited','active','rejected','removed'
    )),
    reason TEXT,
    PRIMARY KEY (event_id, user_id)
);
CREATE INDEX participation_user ON participation(user_id, status);
CREATE TABLE tasks (
    id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL REFERENCES events(id),
    title TEXT NOT NULL CHECK (length(trim(title)) > 0),
    assignee_id TEXT NOT NULL,
    due_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN (
        'assigned','in_progress','needs_help','done','cancelled'
    )),
    note TEXT,
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (event_id, assignee_id) REFERENCES participation(event_id, user_id)
);
CREATE INDEX tasks_event ON tasks(event_id);
CREATE INDEX tasks_assignee ON tasks(assignee_id, status);
CREATE TABLE commands (
    actor_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    operation TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    receipt TEXT NOT NULL,
    PRIMARY KEY (actor_id, request_id)
);
CREATE TABLE audit (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_id TEXT NOT NULL,
    action TEXT NOT NULL,
    event_id TEXT NOT NULL REFERENCES events(id),
    detail TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE outbox (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id TEXT NOT NULL UNIQUE,
    topic TEXT NOT NULL,
    event_id TEXT NOT NULL REFERENCES events(id),
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL,
    acknowledged_at TEXT
);
CREATE INDEX outbox_pending ON outbox(acknowledged_at, sequence);
