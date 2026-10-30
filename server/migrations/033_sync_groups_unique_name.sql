-- ============================================================
-- Digital Signage — Вариант Б (Мини ПК)
-- Миграция 033: одно имя — одна группа экранов (sync_groups)
-- ------------------------------------------------------------
-- Идемпотентна. Применяется ПОСЛЕ 032_broadcast_until.sql.
--
-- Откуда дубли: initdb не вёл учёт в schema_migrations, и первый
-- migrate.sh применял все миграции второй раз, а сиды «Зал 1» и
-- «Все экраны» стояли на ON CONFLICT DO NOTHING без уникального ключа
-- на name. Так на свежей установке появлялось по две такие группы.
--
-- Что делаем:
--   1) дубли по имени сливаем в группу с минимальным id: на неё
--      переводятся screens, schedule_slots, schedule_overrides, campaigns
--      (все ссылки на sync_groups), лишние группы удаляются;
--   2) если после слияния у группового слота (день недели + час) или
--      датовой замены (дата) оказывается две строки, остаётся строка
--      группы с меньшим id (у оставшейся группы — её собственная), при
--      равенстве — с меньшим id строки; остальные удаляются;
--   3) уникальный индекс uq_sync_groups_name — новых дублей не будет.
--
-- Проверить заранее, что сольётся (только чтение):
--   SELECT name, count(*), array_agg(id ORDER BY id)
--     FROM sync_groups GROUP BY name HAVING count(*) > 1;
-- ============================================================

BEGIN;

-- Карта слияния: лишняя группа → группа с минимальным id того же имени
CREATE TEMP TABLE grp_merge ON COMMIT DROP AS
SELECT g.id AS dup_id, k.keep_id
FROM sync_groups g
JOIN (SELECT name, MIN(id) AS keep_id
        FROM sync_groups GROUP BY name HAVING COUNT(*) > 1) k
  ON k.name = g.name AND g.id <> k.keep_id;

-- Групповые недельные слоты: после слияния уникальность uq_slots_group
-- (группа, день, час) не должна нарушиться
DELETE FROM schedule_slots s
USING (
    SELECT s2.id,
           ROW_NUMBER() OVER (
               PARTITION BY COALESCE(m.keep_id, s2.group_id), s2.day_of_week, COALESCE(s2.hour, -1)
               ORDER BY s2.group_id, s2.id) AS rn
    FROM schedule_slots s2
    LEFT JOIN grp_merge m ON m.dup_id = s2.group_id
    WHERE s2.screen_id IS NULL
      AND COALESCE(m.keep_id, s2.group_id) IN (SELECT keep_id FROM grp_merge)
) r
WHERE s.id = r.id AND r.rn > 1;

-- Датовые замены группы: то же для uq_overrides_group_date (группа, дата)
DELETE FROM schedule_overrides o
USING (
    SELECT o2.id,
           ROW_NUMBER() OVER (
               PARTITION BY COALESCE(m.keep_id, o2.group_id), o2.on_date
               ORDER BY o2.group_id, o2.id) AS rn
    FROM schedule_overrides o2
    LEFT JOIN grp_merge m ON m.dup_id = o2.group_id
    WHERE COALESCE(m.keep_id, o2.group_id) IN (SELECT keep_id FROM grp_merge)
) r
WHERE o.id = r.id AND r.rn > 1;

UPDATE screens            t SET group_id = m.keep_id FROM grp_merge m WHERE t.group_id = m.dup_id;
UPDATE schedule_slots     t SET group_id = m.keep_id FROM grp_merge m WHERE t.group_id = m.dup_id;
UPDATE schedule_overrides t SET group_id = m.keep_id FROM grp_merge m WHERE t.group_id = m.dup_id;
UPDATE campaigns          t SET group_id = m.keep_id FROM grp_merge m WHERE t.group_id = m.dup_id;

DELETE FROM sync_groups g USING grp_merge m WHERE g.id = m.dup_id;

CREATE UNIQUE INDEX IF NOT EXISTS uq_sync_groups_name ON sync_groups(name);

COMMIT;

-- ============================================================
-- Конец миграции 033
-- ============================================================
