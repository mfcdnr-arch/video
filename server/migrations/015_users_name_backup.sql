-- Миграция 015: Поля имени/фамилии пользователя + таблица резервных копий
-- Версия: 16.0 | Дата: 2026-06-28

-- Добавляем first_name и last_name к пользователям
ALTER TABLE users ADD COLUMN IF NOT EXISTS first_name VARCHAR(128);
ALTER TABLE users ADD COLUMN IF NOT EXISTS last_name  VARCHAR(128);

-- Заполняем из full_name для существующих записей (берём первое слово как фамилию)
UPDATE users
SET last_name = split_part(full_name, ' ', 1),
    first_name = TRIM(SUBSTRING(full_name FROM LENGTH(split_part(full_name,' ',1))+2))
WHERE full_name IS NOT NULL AND full_name != '';

-- Таблица резервных копий БД
CREATE TABLE IF NOT EXISTS backups (
    id          SERIAL PRIMARY KEY,
    filename    VARCHAR(256) NOT NULL,
    size_bytes  BIGINT,
    created_at  TIMESTAMP DEFAULT NOW(),
    created_by  VARCHAR(64) DEFAULT 'auto'
);

-- Индекс для сортировки по дате
CREATE INDEX IF NOT EXISTS idx_backups_created_at ON backups(created_at DESC);
