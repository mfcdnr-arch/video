-- ============================================================
-- Digital Signage — Вариант Б (Мини ПК)
-- Миграция 022: рекламные кампании (план/факт показов)
-- ------------------------------------------------------------
-- Идемпотентна. Применяется ПОСЛЕ 021_schedule_levels_venue.sql.
--
-- Кампания = обязательство перед клиентом: «N показов в день
-- в период с … по …» для роликов рекламодателя (опционально —
-- только на экранах конкретной группы). Факт считается из play_log
-- на лету; недокрут виден в панели сразу, а не в конце месяца.
-- ============================================================

CREATE TABLE IF NOT EXISTS campaigns (
    id                   SERIAL PRIMARY KEY,
    advertiser_id        INTEGER NOT NULL REFERENCES advertisers(id) ON DELETE CASCADE,
    name                 VARCHAR(128) NOT NULL,
    date_from            DATE NOT NULL,
    date_to              DATE NOT NULL,
    target_plays_per_day INTEGER NOT NULL CHECK (target_plays_per_day > 0),
    group_id             INTEGER REFERENCES sync_groups(id) ON DELETE SET NULL,  -- NULL = вся сеть
    is_active            BOOLEAN DEFAULT TRUE,
    note                 TEXT,
    created_at           TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_campaigns_advertiser ON campaigns(advertiser_id);
CREATE INDEX IF NOT EXISTS idx_campaigns_dates ON campaigns(date_from, date_to);

-- ============================================================
-- Конец миграции 022
-- ============================================================
