-- Users table
CREATE TABLE IF NOT EXISTS users (
    id         UUID PRIMARY KEY,
    phone      VARCHAR(20) NOT NULL UNIQUE,
    nickname   VARCHAR(100),
    avatar_url TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_users_phone ON users(phone);

-- Todos table (synced across devices)
CREATE TABLE IF NOT EXISTS todos (
    id           VARCHAR(64) NOT NULL,
    user_id      UUID NOT NULL REFERENCES users(id),
    content      TEXT NOT NULL DEFAULT '',
    title        TEXT,
    status       VARCHAR(20) NOT NULL DEFAULT 'pending',
    types        JSONB NOT NULL DEFAULT '[]',
    tags         JSONB NOT NULL DEFAULT '[]',
    priority     INTEGER,
    due_at       TIMESTAMPTZ,
    plan         JSONB,
    agent_output TEXT,
    agent_status VARCHAR(20),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted      BOOLEAN NOT NULL DEFAULT FALSE,

    PRIMARY KEY (id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_todos_user_updated ON todos(user_id, updated_at);
CREATE INDEX IF NOT EXISTS idx_todos_user_status  ON todos(user_id, status);
