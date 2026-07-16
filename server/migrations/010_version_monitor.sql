-- ============================================================
-- Digital Signage — Вариант Б (Мини ПК)
-- Миграция 010: мониторинг версий ОС и плеера (mpv) на мини ПК
-- ------------------------------------------------------------
-- Идемпотентна. Применяется ПОСЛЕ 009_fillers_hourly.sql.
-- Только МОНИТОРИНГ и ОПОВЕЩЕНИЕ о расхождении версий.
-- ПРИМЕЧАНИЕ: колонки vlc_version — историческое имя, плеер давно mpv.
-- Автообновление не выполняется (обновление ОС/плеера — ручная
-- операция администратора в обслуживаемое окно).
-- ============================================================

-- Версии, фактически установленные на мини ПК (приходят в heartbeat).
ALTER TABLE screens       ADD COLUMN IF NOT EXISTS os_version  VARCHAR(64);
ALTER TABLE screens       ADD COLUMN IF NOT EXISTS vlc_version VARCHAR(64);
ALTER TABLE minipc_status ADD COLUMN IF NOT EXISTS os_version  VARCHAR(64);
ALTER TABLE minipc_status ADD COLUMN IF NOT EXISTS vlc_version VARCHAR(64);

-- Целевые («эталонные») версии — задаются администратором в настройках.
-- Singleton: одна строка с id = 1.
CREATE TABLE IF NOT EXISTS target_versions (
    id INTEGER PRIMARY KEY DEFAULT 1,
    os_version  VARCHAR(64),
    vlc_version VARCHAR(64),
    updated_by  VARCHAR(64),
    updated_at  TIMESTAMP,
    CHECK (id = 1)
);
INSERT INTO target_versions (id) VALUES (1) ON CONFLICT (id) DO NOTHING;

-- ============================================================
-- Конец миграции 010
-- ============================================================
