-- ============================================================
-- Digital Signage — Вариант Б (Мини ПК)
-- Миграция 017: биллинг — способ расчёта, тариф за показ, счета
-- ------------------------------------------------------------
-- Идемпотентна. Применяется ПОСЛЕ 016_overrides_unique.sql.
--
-- Модель (решение от 2026-07-06): у каждого рекламодателя свой
-- способ расчёта — ЛИБО по минутам, ЛИБО по показам (не сумма
-- обеих метрик), и свои индивидуальные цены.
--   per_minute: сумма = минуты эфира × price_per_minute
--   per_play:   сумма = количество показов × price_per_play
-- Счёт фиксирует тариф и суммы на момент закрытия периода —
-- последующие изменения цен на выставленные счета не влияют.
-- ============================================================

ALTER TABLE advertisers ADD COLUMN IF NOT EXISTS price_per_play NUMERIC(12,2) DEFAULT 0;
ALTER TABLE advertisers ADD COLUMN IF NOT EXISTS billing_mode VARCHAR(16) DEFAULT 'per_minute';

CREATE TABLE IF NOT EXISTS invoices (
    id            SERIAL PRIMARY KEY,
    advertiser_id INTEGER NOT NULL REFERENCES advertisers(id) ON DELETE CASCADE,
    period_start  DATE NOT NULL,
    period_end    DATE NOT NULL,
    billing_mode  VARCHAR(16) NOT NULL,             -- снимок на момент выставления
    price         NUMERIC(12,2) NOT NULL,           -- снимок тарифа (за минуту или за показ)
    plays_total   INTEGER NOT NULL DEFAULT 0,
    minutes_total NUMERIC(12,1) NOT NULL DEFAULT 0,
    amount        NUMERIC(14,2) NOT NULL DEFAULT 0,
    status        VARCHAR(16) NOT NULL DEFAULT 'issued',  -- issued | paid | canceled
    note          TEXT,
    created_at    TIMESTAMP DEFAULT NOW(),
    paid_at       TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_invoices_advertiser ON invoices(advertiser_id);

-- Защита от двойного выставления: на один и тот же период одного
-- рекламодателя может существовать только один неотменённый счёт.
CREATE UNIQUE INDEX IF NOT EXISTS uq_invoices_active_period
    ON invoices(advertiser_id, period_start, period_end)
    WHERE status <> 'canceled';

-- ============================================================
-- Конец миграции 017
-- ============================================================
