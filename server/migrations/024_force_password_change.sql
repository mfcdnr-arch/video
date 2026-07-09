-- ============================================================
-- Миграция 024: форс-смена пароля по умолчанию
-- Добавляет флаг must_change_password. У встроенного admin он
-- выставлен в TRUE — при первом входе панель ЗАСТАВИТ сменить
-- пароль (admin123) до начала работы.
-- Идемпотентна.
-- ============================================================

ALTER TABLE users
    ADD COLUMN IF NOT EXISTS must_change_password BOOLEAN NOT NULL DEFAULT FALSE;

-- Дефолтный администратор обязан сменить пароль при первом входе.
UPDATE users SET must_change_password = TRUE WHERE username = 'admin';
