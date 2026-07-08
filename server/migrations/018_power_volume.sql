-- ============================================================
-- Digital Signage — Вариант Б (Мини ПК)
-- Миграция 018: питание экрана и громкость по расписанию
-- ------------------------------------------------------------
-- Идемпотентна. Применяется ПОСЛЕ 017_billing.sql.
--
-- Питание: агент гасит монитор (DPMS) вне окна power_on..power_off
-- и останавливает воспроизведение (чтобы ночные «показы» не попадали
-- в play_log и биллинг). NULL в обоих полях = экран работает всегда.
--
-- Громкость: volume_day днём, volume_night в окне night_from..night_to.
-- night_from/night_to NULL = всегда volume_day.
-- Все времена — московские (вся сеть в одном часовом поясе, решение
-- пользователя от 2026-07-06).
-- ============================================================

ALTER TABLE screens ADD COLUMN IF NOT EXISTS power_on_time  TIME;
ALTER TABLE screens ADD COLUMN IF NOT EXISTS power_off_time TIME;
ALTER TABLE screens ADD COLUMN IF NOT EXISTS volume_day   INTEGER DEFAULT 100;
ALTER TABLE screens ADD COLUMN IF NOT EXISTS volume_night INTEGER DEFAULT 100;
ALTER TABLE screens ADD COLUMN IF NOT EXISTS night_from TIME;
ALTER TABLE screens ADD COLUMN IF NOT EXISTS night_to   TIME;

-- ============================================================
-- Конец миграции 018
-- ============================================================
