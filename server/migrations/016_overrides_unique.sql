-- Миграция 016: UNIQUE-ограничения для schedule_overrides
-- Нужны для ON CONFLICT в POST /schedule/overrides

ALTER TABLE schedule_overrides
  DROP CONSTRAINT IF EXISTS uq_overrides_screen_date;

ALTER TABLE schedule_overrides
  DROP CONSTRAINT IF EXISTS uq_overrides_group_date;

-- Удаляем дубликаты перед добавлением ограничений (оставляем последний по id)
DELETE FROM schedule_overrides a
USING schedule_overrides b
WHERE a.id < b.id
  AND a.screen_id IS NOT DISTINCT FROM b.screen_id
  AND a.group_id  IS NOT DISTINCT FROM b.group_id
  AND a.on_date = b.on_date;

CREATE UNIQUE INDEX IF NOT EXISTS uq_overrides_screen_date
  ON schedule_overrides (screen_id, on_date)
  WHERE screen_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_overrides_group_date
  ON schedule_overrides (group_id, on_date)
  WHERE group_id IS NOT NULL;
