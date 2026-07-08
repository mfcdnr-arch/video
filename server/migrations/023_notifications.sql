-- ============================================================
-- Digital Signage — Вариант Б (Мини ПК)
-- Миграция 023: уведомления оператору через мессенджер MAX
-- ------------------------------------------------------------
-- Идемпотентна. Применяется ПОСЛЕ 022_campaigns.sql.
--
-- Канал уведомлений — мессенджер MAX (dev.max.ru Bot API), не Telegram
-- (решение пользователя). Celery-задача раз в минуту проверяет условия
-- (экран офлайн, мало места, битый ролик) и шлёт сообщение боту MAX.
-- Дедупликация: одно событие = одно сообщение, повтор только после того,
-- как проблема исчезла и возникла снова (таблица notification_alerts).
-- ============================================================

CREATE TABLE IF NOT EXISTS notification_settings (
    id                SMALLINT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    enabled           BOOLEAN NOT NULL DEFAULT FALSE,
    max_token         TEXT,                 -- токен бота MAX (Authorization)
    max_chat_id       TEXT,                 -- chat_id или user_id получателя
    base_url          TEXT NOT NULL DEFAULT 'https://platform-api.max.ru',
    offline_minutes   INTEGER NOT NULL DEFAULT 10,   -- экран молчит дольше — офлайн
    disk_free_pct     INTEGER NOT NULL DEFAULT 10,   -- свободно меньше — тревога
    notify_offline    BOOLEAN NOT NULL DEFAULT TRUE,
    notify_disk       BOOLEAN NOT NULL DEFAULT TRUE,
    notify_broken     BOOLEAN NOT NULL DEFAULT TRUE,
    updated_at        TIMESTAMP DEFAULT NOW()
);

INSERT INTO notification_settings (id) VALUES (1)
ON CONFLICT (id) DO NOTHING;

-- Активные и исторические срабатывания. alert_key уникально описывает
-- конкретную проблему (например 'offline:5', 'disk:5', 'broken:12'),
-- чтобы не слать одно и то же повторно, пока оно «активно».
CREATE TABLE IF NOT EXISTS notification_alerts (
    id           SERIAL PRIMARY KEY,
    alert_key    VARCHAR(64) NOT NULL,
    type         VARCHAR(24) NOT NULL,       -- offline | disk | broken
    message      TEXT,
    is_active    BOOLEAN NOT NULL DEFAULT TRUE,
    sent_ok      BOOLEAN,                    -- удалось ли доставить в MAX
    created_at   TIMESTAMP DEFAULT NOW(),
    resolved_at  TIMESTAMP
);

-- Быстрый поиск активной записи по ключу.
CREATE UNIQUE INDEX IF NOT EXISTS uq_alerts_active
    ON notification_alerts(alert_key) WHERE is_active;
CREATE INDEX IF NOT EXISTS idx_alerts_created ON notification_alerts(created_at);

-- ============================================================
-- Конец миграции 023
-- ============================================================
