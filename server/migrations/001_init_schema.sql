-- ============================================================
-- Миграция 001: Базовая схема Digital Signage (Вариант Б — Мини ПК)
-- Версия: v14 | Дата: 2026-06-28
-- Идемпотентна (все таблицы — IF NOT EXISTS)
-- Применяется автоматически при первом запуске контейнера Postgres
-- (смонтирована в docker-entrypoint-initdb.d как 01_init.sql).
-- ============================================================

-- Таблица пользователей (администраторы CMS)
CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    username VARCHAR(64) UNIQUE NOT NULL,
    password_hash VARCHAR(256) NOT NULL,
    role VARCHAR(32) DEFAULT 'admin',
    created_at TIMESTAMP DEFAULT NOW()
);

-- Группы синхронизации (экраны в одном зале)
CREATE TABLE IF NOT EXISTS sync_groups (
    id SERIAL PRIMARY KEY,
    name VARCHAR(128) NOT NULL,
    description TEXT,
    created_at TIMESTAMP DEFAULT NOW()
);

-- Экраны / мини ПК
CREATE TABLE IF NOT EXISTS screens (
    id SERIAL PRIMARY KEY,
    name VARCHAR(128) NOT NULL,
    city VARCHAR(64),
    location VARCHAR(128),
    group_id INTEGER REFERENCES sync_groups(id) ON DELETE SET NULL,
    token VARCHAR(64) UNIQUE NOT NULL,
    last_seen TIMESTAMP,
    status VARCHAR(32) DEFAULT 'offline',
    playing_file VARCHAR(256),
    agent_version VARCHAR(32),
    disk_free_gb FLOAT,
    ip_address VARCHAR(45),
    created_at TIMESTAMP DEFAULT NOW()
);

-- Медиабиблиотека (видеоролики)
CREATE TABLE IF NOT EXISTS media (
    id SERIAL PRIMARY KEY,
    title VARCHAR(256) NOT NULL,
    filename VARCHAR(256) NOT NULL,
    filesize BIGINT DEFAULT 0,
    md5_hash VARCHAR(32),
    duration_seconds FLOAT DEFAULT 0,
    status VARCHAR(32) DEFAULT 'ready',
    created_at TIMESTAMP DEFAULT NOW()
);

-- Плейлисты
CREATE TABLE IF NOT EXISTS playlists (
    id SERIAL PRIMARY KEY,
    name VARCHAR(128) NOT NULL,
    description TEXT,
    created_at TIMESTAMP DEFAULT NOW()
);

-- Ролики в плейлисте
CREATE TABLE IF NOT EXISTS playlist_items (
    id SERIAL PRIMARY KEY,
    playlist_id INTEGER REFERENCES playlists(id) ON DELETE CASCADE,
    media_id INTEGER REFERENCES media(id) ON DELETE CASCADE,
    position INTEGER NOT NULL DEFAULT 0,
    repeat_count INTEGER DEFAULT 1
);

-- Расписание (плейлист на экране в конкретное время)
CREATE TABLE IF NOT EXISTS schedule_slots (
    id SERIAL PRIMARY KEY,
    screen_id INTEGER REFERENCES screens(id) ON DELETE CASCADE,
    day_of_week INTEGER NOT NULL CHECK (day_of_week BETWEEN 0 AND 6),
    hour INTEGER NOT NULL CHECK (hour BETWEEN 0 AND 23),
    playlist_id INTEGER REFERENCES playlists(id) ON DELETE CASCADE,
    UNIQUE (screen_id, day_of_week, hour)
);

-- Статусы мини ПК (история heartbeat)
CREATE TABLE IF NOT EXISTS minipc_status (
    id SERIAL PRIMARY KEY,
    screen_id INTEGER REFERENCES screens(id) ON DELETE CASCADE,
    timestamp TIMESTAMP DEFAULT NOW(),
    status VARCHAR(32),
    playing_file VARCHAR(256),
    disk_free_gb FLOAT,
    agent_version VARCHAR(32),
    ip_address VARCHAR(45)
);

-- Лог воспроизведения
CREATE TABLE IF NOT EXISTS play_log (
    id SERIAL PRIMARY KEY,
    screen_id INTEGER REFERENCES screens(id) ON DELETE CASCADE,
    media_id INTEGER REFERENCES media(id) ON DELETE SET NULL,
    started_at TIMESTAMP NOT NULL,
    ended_at TIMESTAMP,
    filename VARCHAR(256)
);

-- Очередь команд (sync, play, stop, update_agent)
CREATE TABLE IF NOT EXISTS commands (
    id SERIAL PRIMARY KEY,
    screen_id INTEGER REFERENCES screens(id) ON DELETE CASCADE,
    type VARCHAR(32) NOT NULL,
    payload JSONB,
    created_at TIMESTAMP DEFAULT NOW(),
    executed_at TIMESTAMP
);

-- Индексы
CREATE INDEX IF NOT EXISTS idx_schedule_screen    ON schedule_slots(screen_id);
CREATE INDEX IF NOT EXISTS idx_schedule_day_hour  ON schedule_slots(day_of_week, hour);
CREATE INDEX IF NOT EXISTS idx_play_log_screen    ON play_log(screen_id);
CREATE INDEX IF NOT EXISTS idx_commands_screen    ON commands(screen_id);
CREATE INDEX IF NOT EXISTS idx_commands_executed  ON commands(executed_at) WHERE executed_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_minipc_status_screen ON minipc_status(screen_id, timestamp DESC);

-- Администратор по умолчанию (пароль: admin123 — сменить после первого входа!)
INSERT INTO users (username, password_hash, role) VALUES
  ('admin', '$2b$12$1rFNE5wroo/gieqz2mQYce4d.Dy6nVQv0pQS.kefuruC6Q7ma9VRG', 'superadmin')
ON CONFLICT (username) DO NOTHING;

-- Группа синхронизации по умолчанию
INSERT INTO sync_groups (name, description) VALUES
  ('Зал 1', 'Основная группа синхронизации')
ON CONFLICT DO NOTHING;
