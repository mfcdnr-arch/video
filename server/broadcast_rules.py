"""Правила общего эфира («Эфир сети»): срок и «идёт ли эфир сейчас».

Чистая логика на stdlib, без БД и без fastapi. Все моменты — наивное
московское время (МСК), как и в показах: срок эфира хранится и сравнивается
в МСК, а не в UTC контейнера Postgres.

Срок эфира — целый час «сегодня»: «до 16:00» значит «до 16:00:00 МСК, не
включая». Конец суток — 24 (граница 00:00 следующих суток). Отсутствие срока
(None) — «до отмены»: эфир идёт, пока его не выключили вручную.
"""
from datetime import datetime, time, timedelta

STALE_WINDOW_TEXT = "Время изменилось — проверьте час окончания"   # тот же текст показывает и окно в панели


def parse_until_hour(value, now: datetime) -> datetime | None:
    """Час окончания эфира → момент окончания (наивное МСК); None — «до отмены».

    Допустимо только целое от 1 до 24, строго позже текущего часа (в 14:00:00
    «до 14:00» уже прошло, ближайший — 15). 24 — полночь следующих суток.
    Что угодно другое — ValueError с русским текстом для пользователя: мусор
    нельзя тихо превращать в «до отмены», поэтому строки, дроби и bool отвергаются.
    """
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 24:
        raise ValueError("Час окончания — целое от 1 до 24")
    if value <= now.hour:
        raise ValueError("Час окончания должен быть позже текущего")
    if value == 24:
        return datetime.combine(now.date() + timedelta(days=1), time(0, 0))
    return datetime.combine(now.date(), time(value, 0))


def until_text(until_at: datetime | None) -> str:
    """Срок эфира словами: «до 16:00» / «до 24:00» (полночь следующих суток) / «до отмены»."""
    if until_at is None:
        return "до отмены"
    if until_at.hour == 0 and until_at.minute == 0:
        return "до 24:00"
    return f"до {until_at:%H:%M}"


def effective_on(row: dict | None, now: datetime) -> bool:
    """Идёт ли общий эфир в момент `now`.

    `row` — строка network_broadcast (is_on, playlist_id, until_at) или None.
    Эфир идёт, если он включён, у него есть плейлист (удалённый плейлист
    обнуляет playlist_id) и срок не вышел: в момент until_at эфир уже
    выключен. Так «до 15:00» перестаёт действовать ровно в 15:00 МСК, не
    дожидаясь, пока Celery-задача сбросит строку.
    """
    if not row or not row.get("is_on") or row.get("playlist_id") is None:
        return False
    until_at = row.get("until_at")
    return until_at is None or now < until_at
