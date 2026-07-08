-- ============================================================
-- Digital Signage — Вариант Б (Мини ПК)
-- Миграция 009: заглушки (filler), часовой плейлист, журнал потерь связи
-- ------------------------------------------------------------
-- Идемпотентна. Применяется ПОСЛЕ 008_custom_reports.sql.
-- ============================================================

-- 1. ЗАГЛУШКИ ---------------------------------------------------------------
-- Пометка ролика как «заглушка» (материал для заполнения пустого эфирного
-- времени). Заглушки лежат в служебной папке «Заглушки».
ALTER TABLE media ADD COLUMN IF NOT EXISTS is_filler BOOLEAN DEFAULT FALSE;

-- Служебный рекламодатель и папка для заглушек (чтобы они были в медиатеке
-- отдельной папкой, как обычные материалы).
INSERT INTO advertisers (name, color, note) VALUES
  ('Служебное', '#9298a3', 'Системные материалы: заглушки')
ON CONFLICT (name) DO NOTHING;

INSERT INTO media_folders (advertiser_id, name)
SELECT a.id, 'Заглушки' FROM advertisers a WHERE a.name = 'Служебное'
ON CONFLICT (advertiser_id, name) DO NOTHING;

-- 2. ЧАСОВОЙ ПЛЕЙЛИСТ -------------------------------------------------------
-- Признак, что плейлист должен заполнять РОВНО час: реклама + заглушки
-- равномерно в оставшееся время. Длительность считается по duration_seconds.
ALTER TABLE playlists ADD COLUMN IF NOT EXISTS fill_to_hour BOOLEAN DEFAULT TRUE;
ALTER TABLE playlists ADD COLUMN IF NOT EXISTS target_seconds INTEGER DEFAULT 3600;

-- 3. ЖУРНАЛ ПОТЕРЬ СВЯЗИ ----------------------------------------------------
-- Фиксируется, когда экран ушёл в offline и когда вернулся (или ещё не вернулся).
-- duration_seconds заполняется при восстановлении связи.
CREATE TABLE IF NOT EXISTS connection_losses (
    id SERIAL PRIMARY KEY,
    screen_id INTEGER REFERENCES screens(id) ON DELETE CASCADE,
    screen_name VARCHAR(128),
    lost_at TIMESTAMP DEFAULT NOW(),
    restored_at TIMESTAMP,
    duration_seconds INTEGER
);
CREATE INDEX IF NOT EXISTS idx_conn_loss_screen ON connection_losses(screen_id);
CREATE INDEX IF NOT EXISTS idx_conn_loss_lost ON connection_losses(lost_at DESC);

-- ============================================================
-- Конец миграции 009
-- ============================================================
