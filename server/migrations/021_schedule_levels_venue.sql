-- ============================================================
-- Digital Signage — Вариант Б (Мини ПК)
-- Миграция 021: трёхуровневое недельное расписание + тип площадки
-- ------------------------------------------------------------
-- Идемпотентна. Применяется ПОСЛЕ 020_ad_compliance.sql.
--
-- 1) Недельные слоты теперь трёх уровней (приоритет сверху вниз):
--      экран  (screen_id задан)                — только этот экран;
--      группа (group_id задан)                 — экраны группы без своего слота;
--      сеть   (оба NULL)                       — все остальные экраны.
--    Колонка group_id существовала с ранних версий, но не использовалась
--    ни сервером, ни панелью — теперь она в деле.
--    Поверх шаблона, как и раньше: датовые переопределения и «Эфир сети».
--
-- 2) Тип площадки экрана (venue_type) — дозакрытие 38-ФЗ: рекламу алкоголя
--    (ст. 21) сервер выдаёт только на площадки «магазин с алколицензией».
-- ============================================================

-- Уникальность слотов на каждом уровне (частичные индексы, т.к. базовый
-- UNIQUE(screen_id, dow, hour) не ловит NULL-комбинации).
CREATE UNIQUE INDEX IF NOT EXISTS uq_slots_group
    ON schedule_slots (group_id, day_of_week, COALESCE(hour, -1))
    WHERE screen_id IS NULL AND group_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_slots_network
    ON schedule_slots (day_of_week, COALESCE(hour, -1))
    WHERE screen_id IS NULL AND group_id IS NULL;

CREATE INDEX IF NOT EXISTS idx_slots_group ON schedule_slots(group_id);

-- Тип площадки: store_alcohol (магазин с лицензией на алкоголь),
-- store (магазин), mall (ТЦ), office (офис), other.
ALTER TABLE screens ADD COLUMN IF NOT EXISTS venue_type VARCHAR(32) DEFAULT 'other';

-- ============================================================
-- Конец миграции 021
-- ============================================================
