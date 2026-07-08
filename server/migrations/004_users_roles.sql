-- ============================================================
-- Digital Signage — Вариант Б (Мини ПК)
-- Миграция 004: пользователи, роли и сессии входа
-- ------------------------------------------------------------
-- Идемпотентна. Применяется ПОСЛЕ 003_reports.sql.
-- Роли: superadmin | admin | auditor
--   superadmin — все права + управление пользователями и выдача роли admin
--   admin      — все права, кроме повышения до admin/superadmin
--   auditor    — просмотр и отчёты (никаких изменений)
-- ============================================================

-- Доп. поля пользователя
ALTER TABLE users ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT TRUE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS created_by VARCHAR(64);
ALTER TABLE users ADD COLUMN IF NOT EXISTS last_login TIMESTAMP;

-- Повышаем встроенного admin до superadmin (первый супер-админ).
UPDATE users SET role = 'superadmin' WHERE username = 'admin' AND role IN ('admin', 'superadmin');

-- Журнал сессий: вход/выход и длительность пребывания в панели.
CREATE TABLE IF NOT EXISTS user_sessions (
    id SERIAL PRIMARY KEY,
    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    username VARCHAR(64),
    login_at TIMESTAMP DEFAULT NOW(),
    last_seen TIMESTAMP DEFAULT NOW(),   -- обновляется активностью; косвенно = «находился»
    logout_at TIMESTAMP,
    ip_address VARCHAR(45)
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON user_sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_login ON user_sessions(login_at DESC);

-- ============================================================
-- Конец миграции 004
-- ============================================================
