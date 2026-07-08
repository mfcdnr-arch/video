-- ============================================================
-- Digital Signage — Вариант Б (Мини ПК)
-- Миграция 008: конструктор отчётов (сохранённые отчёты)
-- ------------------------------------------------------------
-- Идемпотентна. Применяется ПОСЛЕ 007_media_validity.sql.
-- Хранит НАСТРОЙКИ отчёта (выбор из готовых блоков), а не SQL.
-- ============================================================

CREATE TABLE IF NOT EXISTS custom_reports (
    id SERIAL PRIMARY KEY,
    owner_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
    name VARCHAR(128) NOT NULL,
    -- config: JSON с выбранными блоками (metric, dimension, period, filters).
    -- Поля валидируются на сервере по белому списку — произвольный SQL невозможен.
    config JSONB NOT NULL,
    created_at TIMESTAMP DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_custom_reports_owner ON custom_reports(owner_id);

-- ============================================================
-- Конец миграции 008
-- ============================================================
