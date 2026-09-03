CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE IF NOT EXISTS chats (
    jid VARCHAR(100) PRIMARY KEY,
    name TEXT,
    is_group BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_message_time TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS messages (
    id VARCHAR(100) PRIMARY KEY,
    chat_jid VARCHAR(100) NOT NULL REFERENCES chats(jid) ON DELETE CASCADE,
    sender_jid VARCHAR(100),
    sender_name TEXT,
    content TEXT,
    "timestamp" TIMESTAMPTZ NOT NULL,
    from_me BOOLEAN NOT NULL DEFAULT FALSE,
    has_media BOOLEAN NOT NULL DEFAULT FALSE,
    media_type VARCHAR(50),
    media_path TEXT,
    media_mime VARCHAR(100),
    quoted_message_id VARCHAR(100)
);

CREATE INDEX IF NOT EXISTS ix_messages_chat_timestamp_desc
    ON messages (chat_jid, "timestamp" DESC);

CREATE INDEX IF NOT EXISTS ix_messages_quoted_message_id
    ON messages (quoted_message_id)
    WHERE quoted_message_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_messages_content_fts_portuguese
    ON messages
    USING GIN (to_tsvector('portuguese'::regconfig, COALESCE(content, '')));

CREATE INDEX IF NOT EXISTS ix_chats_name_trgm
    ON chats
    USING GIN (name gin_trgm_ops);

CREATE TABLE IF NOT EXISTS import_jobs (
    id VARCHAR(36) PRIMARY KEY,
    filename TEXT NOT NULL,
    source_sha256 VARCHAR(64),
    status VARCHAR(20) NOT NULL DEFAULT 'queued',
    source_schema VARCHAR(30),
    chats_processed BIGINT NOT NULL DEFAULT 0,
    messages_processed BIGINT NOT NULL DEFAULT 0,
    media_copied BIGINT NOT NULL DEFAULT 0,
    media_missing BIGINT NOT NULL DEFAULT 0,
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS ix_import_jobs_created_at_desc
    ON import_jobs (created_at DESC);
