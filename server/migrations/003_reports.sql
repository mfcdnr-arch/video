-- ============================================================
-- Digital Signage — Вариант Б (Мини ПК)
-- Миграция 003: отчёты — нерабочие ролики и ошибки воспроизведения
-- ------------------------------------------------------------
-- Идемпотентна. Применяется ПОСЛЕ 002_admin_panel.sql.
-- ============================================================

-- Пометка «нерабочий ролик»: агент не смог воспроизвести файл.
ALTER TABLE media ADD COLUMN IF NOT EXISTS is_broken BOOLEAN DEFAULT FALSE;
ALTER TABLE media ADD COLUMN IF NOT EXISTS last_error TEXT;
ALTER TABLE media ADD COLUMN IF NOT EXISTS error_count INTEGER DEFAULT 0;
ALTER TABLE media ADD COLUMN IF NOT EXISTS last_error_at TIMESTAMP;

-- Журнал ошибок воспроизведения (для отчёта «нерабочие ролики»).
CREATE TABLE IF NOT EXISTS playback_errors (
    id SERIAL PRIMARY KEY,
    screen_id INTEGER REFERENCES screens(id) ON DELETE CASCADE,
    media_id INTEGER REFERENCES media(id) ON DELETE SET NULL,
    filename VARCHAR(256),
    reason TEXT,
    created_at TIMESTAMP DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_pberr_media ON playback_errors(media_id);
CREATE INDEX IF NOT EXISTS idx_pberr_created ON playback_errors(created_at DESC);

-- Индекс для отчётов по времени показа (play_log по медиа и периоду).
CREATE INDEX IF NOT EXISTS idx_play_log_media ON play_log(media_id);
CREATE INDEX IF NOT EXISTS idx_play_log_started ON play_log(started_at);

-- ============================================================
-- Конец миграции 003
-- ============================================================
