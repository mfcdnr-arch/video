-- ============================================================
-- Digital Signage — Вариант Б (Мини ПК)
-- Миграция 025: один и тот же ролик не может быть в плейлисте дважды
-- ------------------------------------------------------------
-- Идемпотентна. Сначала убираем уже существующие дубли (оставляем
-- запись с наименьшим id = добавленную раньше), затем ставим UNIQUE.
-- ============================================================

DELETE FROM playlist_items a
USING playlist_items b
WHERE a.playlist_id = b.playlist_id
  AND a.media_id   = b.media_id
  AND a.id > b.id;

CREATE UNIQUE INDEX IF NOT EXISTS ux_playlist_items_pl_media
    ON playlist_items (playlist_id, media_id);
