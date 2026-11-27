
CREATE TABLE IF NOT EXISTS territories (
    id TEXT PRIMARY KEY,
    region TEXT NOT NULL,
    locality TEXT NOT NULL,
    UNIQUE (region, locality)
);

CREATE TABLE IF NOT EXISTS directions (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    telegram_user_id INTEGER NOT NULL UNIQUE,
    display_name TEXT,
    territory_id TEXT REFERENCES territories(id),
    age INTEGER CHECK (age >= 0),
    status TEXT NOT NULL DEFAULT 'draft'
        CHECK (status IN (
            'draft',
            'pending',
            'active',
            'rejected',
            'revoked'
        )),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS user_interests (
    user_id TEXT NOT NULL REFERENCES users(id),
    direction_id TEXT NOT NULL REFERENCES directions(id),
    PRIMARY KEY (user_id, direction_id)
);

CREATE TABLE IF NOT EXISTS user_help_types (
    user_id TEXT NOT NULL REFERENCES users(id),
    help_type TEXT NOT NULL,
    PRIMARY KEY (user_id, help_type)
);

CREATE TABLE IF NOT EXISTS consents (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id),
    consent_type TEXT NOT NULL,
    version TEXT NOT NULL,
    accepted_at TEXT NOT NULL,
    revoked_at TEXT,
    UNIQUE (user_id, consent_type, version)
);


CREATE TABLE IF NOT EXISTS user_roles (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id),
    role TEXT NOT NULL CHECK (
        role IN (
            'participant',
            'event_responsible',
            'territory_coordinator',
            'council_representative',
            'general_coordinator',
            'tech_admin'
        )
    ),
    scope_id TEXT NOT NULL DEFAULT '',
    granted_by TEXT NOT NULL REFERENCES users(id),
    granted_at TEXT NOT NULL,
    revoked_by TEXT REFERENCES users(id),
    revoked_at TEXT,
    CHECK (
        (revoked_by IS NULL AND revoked_at IS NULL)
        OR
        (revoked_by IS NOT NULL AND revoked_at IS NOT NULL)
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS unique_active_role
ON user_roles (user_id, role, scope_id)
WHERE revoked_at IS NULL;

CREATE TABLE IF NOT EXISTS authorization_audit (
    id TEXT PRIMARY KEY,
    actor_id TEXT NOT NULL REFERENCES users(id),
    target_id TEXT NOT NULL REFERENCES users(id),
    action TEXT NOT NULL,
    role TEXT,
    scope_id TEXT,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL
);
