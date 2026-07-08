-- ============================================================
-- Digital Signage — Вариант Б (Мини ПК)
-- Миграция 020: соответствие закону о рекламе (38-ФЗ)
-- ------------------------------------------------------------
-- Идемпотентна. Применяется ПОСЛЕ 019_ops_hardening.sql.
--
-- Схема работы (согласована 2026-07-05, реализована 2026-07-07):
--   1) При загрузке ролика обязательно декларируется категория товара.
--      Табак/никотин блокируется на входе (ст. 7 38-ФЗ — реклама
--      запрещена полностью). Регулируемые категории (медицина, финансы,
--      алкоголь, азартные игры) требуют доп. полей: возрастная
--      маркировка, текст обязательного предупреждения, № лицензии.
--   2) Новый контент попадает в review_status='pending' и НЕ выдаётся
--      агентам, пока модератор не переведёт его в 'approved'.
--      Категория 'service' (заглушки, собственный служебный контент —
--      не реклама) одобряется автоматически.
--   3) Кто/когда/с какой категорией одобрил — фиксируется здесь и в
--      audit_log: вместе с play_log это доказательная база для ФАС.
-- ============================================================

ALTER TABLE media ADD COLUMN IF NOT EXISTS category VARCHAR(32) DEFAULT 'other';
ALTER TABLE media ADD COLUMN IF NOT EXISTS age_rating VARCHAR(4);          -- 0+ 6+ 12+ 16+ 18+
ALTER TABLE media ADD COLUMN IF NOT EXISTS disclaimer_text TEXT;           -- обязательное предупреждение
ALTER TABLE media ADD COLUMN IF NOT EXISTS license_number VARCHAR(128);    -- для финансовых услуг

ALTER TABLE media ADD COLUMN IF NOT EXISTS review_status VARCHAR(16) DEFAULT 'pending';
ALTER TABLE media ADD COLUMN IF NOT EXISTS reviewed_by VARCHAR(64);
ALTER TABLE media ADD COLUMN IF NOT EXISTS reviewed_at TIMESTAMP;
ALTER TABLE media ADD COLUMN IF NOT EXISTS reject_reason TEXT;

-- Всё, что уже было в системе до внедрения модерации, считается
-- одобренным (иначе работающая сеть мгновенно погасла бы).
UPDATE media SET review_status = 'approved'
WHERE review_status = 'pending' AND created_at < NOW();

CREATE INDEX IF NOT EXISTS idx_media_review ON media(review_status);

-- ============================================================
-- Конец миграции 020
-- ============================================================
