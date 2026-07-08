-- ============================================================
-- Digital Signage — Вариант Б (Мини ПК)
-- Миграция 019: контроль часов, архив журнала показов, скидки в счетах
-- ------------------------------------------------------------
-- Идемпотентна. Применяется ПОСЛЕ 018_power_volume.sql.
-- ============================================================

-- Дрейф часов мини-ПК: разница между временем сервера и временем агента
-- на момент последнего heartbeat (секунды, положительное = часы агента отстают).
ALTER TABLE screens ADD COLUMN IF NOT EXISTS clock_drift_seconds NUMERIC(10,1);

-- Дневные агрегаты показов: сюда ежемесячная задача сворачивает строки
-- play_log старше года (отчёты по свежим периодам продолжают читать play_log).
CREATE TABLE IF NOT EXISTS play_log_daily (
    id         SERIAL PRIMARY KEY,
    on_date    DATE NOT NULL,
    screen_id  INTEGER,           -- без FK: экран может быть уже удалён
    media_id   INTEGER,
    filename   VARCHAR(256),
    plays      INTEGER NOT NULL DEFAULT 0,
    seconds    NUMERIC(14,1) NOT NULL DEFAULT 0,
    UNIQUE (on_date, screen_id, media_id, filename)
);
CREATE INDEX IF NOT EXISTS idx_pld_date ON play_log_daily(on_date);

-- Архив сырых строк (та же структура, что play_log, но без FK —
-- архив должен переживать удаление экранов и роликов).
CREATE TABLE IF NOT EXISTS play_log_archive (
    id         INTEGER,
    screen_id  INTEGER,
    media_id   INTEGER,
    started_at TIMESTAMP NOT NULL,
    ended_at   TIMESTAMP,
    filename   VARCHAR(256)
);
CREATE INDEX IF NOT EXISTS idx_pla_started ON play_log_archive(started_at);

-- Скидка/корректировка в счёте (например, компенсация за простой экранов).
-- Отрицательное значение = скидка. Итог к оплате = amount + adjustment_amount.
ALTER TABLE invoices ADD COLUMN IF NOT EXISTS adjustment_amount NUMERIC(14,2) DEFAULT 0;
ALTER TABLE invoices ADD COLUMN IF NOT EXISTS adjustment_note TEXT;

-- ============================================================
-- Конец миграции 019
-- ============================================================
