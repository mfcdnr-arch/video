-- 035: «Копия на флешку» — что и когда скачивали, пароль на копию (зашифрован ключом из SECRET_KEY).
CREATE TABLE IF NOT EXISTS offsite_copy_state (
    id               SMALLINT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    password_enc     TEXT,
    password_set_at  TIMESTAMP,
    password_set_by  VARCHAR(64),
    last_copy_at     TIMESTAMP,
    last_copy_by     VARCHAR(64),
    last_copy_file   VARCHAR(128),
    last_copy_bytes  BIGINT,
    media_watermark  INTEGER NOT NULL DEFAULT 0,
    last_media_at    TIMESTAMP,
    last_media_by    VARCHAR(64),
    last_media_scope VARCHAR(8),
    last_media_count INTEGER,
    last_media_bytes BIGINT
);
INSERT INTO offsite_copy_state (id) VALUES (1) ON CONFLICT (id) DO NOTHING;
