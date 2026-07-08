-- ============================================================
-- Digital Signage — Вариант Б (Мини ПК)
-- Миграция 007: срок действия видеоролика (дата и время)
-- ------------------------------------------------------------
-- Идемпотентна. Применяется ПОСЛЕ 006_superadmin_block.sql.
-- valid_from / valid_until — период, в течение которого ролик
-- разрешён к показу (с точностью до времени). NULL = без ограничения.
-- ============================================================

ALTER TABLE media ADD COLUMN IF NOT EXISTS valid_from  TIMESTAMP;
ALTER TABLE media ADD COLUMN IF NOT EXISTS valid_until TIMESTAMP;

-- Перенесём ранее заданные даты кампании (если были) в новые поля.
UPDATE media SET valid_from  = campaign_start::timestamp
    WHERE valid_from IS NULL AND campaign_start IS NOT NULL;
UPDATE media SET valid_until = (campaign_end::timestamp + INTERVAL '23 hours 59 minutes')
    WHERE valid_until IS NULL AND campaign_end IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_media_valid_until ON media(valid_until);

-- ============================================================
-- Конец миграции 007
-- ============================================================
