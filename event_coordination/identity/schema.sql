
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
