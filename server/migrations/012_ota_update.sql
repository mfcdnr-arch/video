-- Миграция 012: OTA-обновление агента
-- Идемпотентна.

-- Версия агента, которую должны иметь все устройства
-- Singleton: одна строка id=1
ALTER TABLE target_versions ADD COLUMN IF NOT EXISTS agent_version VARCHAR(32);
ALTER TABLE target_versions ADD COLUMN IF NOT EXISTS agent_files JSONB;
-- Список файлов для обновления: [{"name": "ds_agent.py", "md5": "...", "size": 1234}]

-- Таблица пакетов обновлений (история)
CREATE TABLE IF NOT EXISTS agent_updates (
    id SERIAL PRIMARY KEY,
    version VARCHAR(32) NOT NULL,
    files JSONB NOT NULL,         -- список файлов в пакете
    changelog TEXT,               -- описание изменений
    created_by VARCHAR(64),
    created_at TIMESTAMP DEFAULT NOW(),
    screens_updated INTEGER DEFAULT 0,
    screens_total INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_agent_updates_created ON agent_updates(created_at DESC);
