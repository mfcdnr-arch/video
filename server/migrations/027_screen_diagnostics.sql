-- ============================================================
-- Миграция 027: архивы диагностики экранов
-- ------------------------------------------------------------
-- По кнопке «Диагностика» в панели агент собирает архив логов
-- (journalctl, ds-agent.log, конфиг без токена, версии, df,
-- состояние плеера и часов) и загружает на сервер. Панель даёт
-- скачать архив из списка. Файлы — в /data/backups/diagnostics
-- (том backup_data). Идемпотентна.
-- ============================================================

CREATE TABLE IF NOT EXISTS screen_diagnostics (
    id SERIAL PRIMARY KEY,
    screen_id INTEGER REFERENCES screens(id) ON DELETE CASCADE,
    filename VARCHAR(256) NOT NULL,
    size_bytes BIGINT,
    created_at TIMESTAMP DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_screen_diag_screen
    ON screen_diagnostics(screen_id, created_at DESC);

-- ============================================================
-- Конец миграции 027
-- ============================================================
