-- ============================================================
-- Digital Signage — Вариант Б (Мини ПК)
-- Миграция 034: бегущая строка — срочные объявления на сегодня
-- ------------------------------------------------------------
-- Идемпотентна. Применяется ПОСЛЕ 033_sync_groups_unique_name.sql.
--
-- Что меняется:
--   1) tickers: срок (until_at), «где» (target: all | groups | screens),
--      кто и когда запустил / правил. Все моменты — наивное МСК, их
--      пишет Python (now_msk), а не NOW() Postgres (контейнер БД в UTC).
--      NULL в until_at = «до снятия».
--   2) Старые строки: неактивные и истёкшие удаляются, остальным
--      переносятся цель (is_all → all | screens), срок и время запуска.
--      Старые expires_at / created_at писал NOW() Postgres, то есть
--      это UTC — переводим в МСК. Строка без срока остаётся «до снятия».
--   3) ticker_groups: строка на группы экранов (sync_groups). Состав
--      групп смотрится в момент показа, а не запуска.
--   4) ticker_templates: шаблоны объявлений (текст, вид, срок). Три
--      стартовых вставляются, только если таблица пуста.
--   5) screens: последний отчёт агента о строках — какие id бегут,
--      остановлен ли показ («Стоп»), выключен ли экран по питанию.
--      NULL — агент (< 1.4.0) такого отчёта не присылал.
--
-- Проверить заранее, что будет удалено (только чтение):
--   SELECT id, message, is_active, expires_at FROM tickers
--    WHERE is_active = FALSE OR (expires_at IS NOT NULL AND expires_at <= NOW());
-- ============================================================

BEGIN;

ALTER TABLE tickers ADD COLUMN IF NOT EXISTS until_at TIMESTAMP;
ALTER TABLE tickers ADD COLUMN IF NOT EXISTS target VARCHAR(8) NOT NULL DEFAULT 'all';
ALTER TABLE tickers ADD COLUMN IF NOT EXISTS started_at TIMESTAMP;
ALTER TABLE tickers ADD COLUMN IF NOT EXISTS started_by VARCHAR(64);
ALTER TABLE tickers ADD COLUMN IF NOT EXISTS changed_at TIMESTAMP;
ALTER TABLE tickers ADD COLUMN IF NOT EXISTS changed_by VARCHAR(64);

-- Переносим только строки, которых ещё не касались (started_at пуст):
-- повторный запуск не перезапишет цель и срок строк, созданных новым кодом.
DELETE FROM tickers
WHERE started_at IS NULL
  AND (is_active = FALSE OR (expires_at IS NOT NULL AND expires_at <= NOW()));

UPDATE tickers
SET target     = CASE WHEN is_all THEN 'all' ELSE 'screens' END,
    until_at   = COALESCE(until_at, CASE WHEN expires_at IS NOT NULL
                                         THEN (expires_at AT TIME ZONE 'UTC') AT TIME ZONE 'Europe/Moscow' END),
    started_at = (created_at AT TIME ZONE 'UTC') AT TIME ZONE 'Europe/Moscow',
    started_by = created_by
WHERE started_at IS NULL;

CREATE TABLE IF NOT EXISTS ticker_groups (
    ticker_id INTEGER NOT NULL REFERENCES tickers(id)     ON DELETE CASCADE,
    group_id  INTEGER NOT NULL REFERENCES sync_groups(id) ON DELETE CASCADE,
    PRIMARY KEY (ticker_id, group_id)
);

CREATE TABLE IF NOT EXISTS ticker_templates (
    id         SERIAL PRIMARY KEY,
    message    TEXT NOT NULL,
    color      VARCHAR(9)  NOT NULL DEFAULT '#ffd34d',
    speed      VARCHAR(16) NOT NULL DEFAULT 'medium',          -- slow | medium | fast
    term_kind  VARCHAR(8)  NOT NULL DEFAULT 'cancel'
               CHECK (term_kind IN ('cancel', 'minutes', 'hour')),   -- до снятия | на N минут | до ЧЧ:00
    term_value INTEGER,                                        -- минуты или час; NULL для «до снятия»
    created_by VARCHAR(64),
    created_at TIMESTAMP DEFAULT NOW()
);

INSERT INTO ticker_templates (message, color, speed, term_kind, term_value)
SELECT v.message, v.color, v.speed, v.term_kind, v.term_value
FROM (VALUES
    ('Магазин закрывается через 15 минут. Спасибо за покупки!', '#ffd34d', 'medium', 'minutes', 15),
    ('Технический перерыв. Приносим извинения за неудобства', '#ff6b5e', 'medium', 'cancel', NULL::INTEGER),
    ('Касса временно не работает. Пожалуйста, пройдите к соседней кассе', '#ffffff', 'medium', 'minutes', 30)
) AS v(message, color, speed, term_kind, term_value)
WHERE NOT EXISTS (SELECT 1 FROM ticker_templates);

ALTER TABLE screens ADD COLUMN IF NOT EXISTS ticker_ids INTEGER[];
ALTER TABLE screens ADD COLUMN IF NOT EXISTS ticker_stopped BOOLEAN;
ALTER TABLE screens ADD COLUMN IF NOT EXISTS ticker_screen_off BOOLEAN;

COMMIT;
