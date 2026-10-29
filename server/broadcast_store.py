"""Общий эфир в БД: чтение, включение, выключение, истечение по времени.

Здесь только SQL (одна строка-состояние network_broadcast, id = 1). Решение
«идёт ли эфир сейчас» принимает broadcast_rules.effective_on — чистая логика;
строка БД может ещё хранить is_on = TRUE, хотя срок уже вышел (пока Celery-задача
expire_broadcast не дошла), поэтому читать состояние нужно через get_broadcast.

Все отметки времени — наивное МСК. Пишем их из Python (now_msk), а не NOW()
Postgres: контейнер БД живёт в UTC, и «до 15:00» поехало бы на три часа.

Функции не делают commit — транзакцией управляет вызывающий (роутер или задача).
"""
from datetime import datetime

from sqlalchemy import text

from broadcast_rules import effective_on, until_text
from placements_store import now_msk

AUDIT_TYPE = "broadcast"
TITLE_ON = "Общий эфир включён"
TITLE_CHANGED = "Общий эфир сменён"
TITLE_OFF = "Общий эфир выключен"
TITLE_EXPIRED = "Общий эфир завершён по времени"
SYSTEM_ACTOR = "система"


def _load(db, lock: bool = False) -> dict | None:
    """Строка состояния эфира (с названием плейлиста) или None, если строки нет.

    lock=True — `FOR UPDATE OF b`: пока транзакция не закончена, другой
    администратор не сможет включить эфир заново между чтением и сбросом
    (его запрос подождёт, а мы, подождав его, увидим уже новый срок).
    """
    r = db.execute(text(f"""
        SELECT b.is_on, b.playlist_id, p.name AS playlist_name, b.until_at,
               b.enabled_by, b.enabled_at
        FROM network_broadcast b LEFT JOIN playlists p ON p.id = b.playlist_id
        WHERE b.id = 1{" FOR UPDATE OF b" if lock else ""}
    """)).fetchone()
    if r is None:
        return None
    return {"is_on": bool(r.is_on), "playlist_id": r.playlist_id, "playlist_name": r.playlist_name,
            "until_at": r.until_at, "enabled_by": r.enabled_by, "enabled_at": r.enabled_at}


def _audit(db, title: str, detail: str | None, actor: str) -> None:
    db.execute(text("""
        INSERT INTO audit_log (event_type, title, detail, actor)
        VALUES (:type, :t, :d, :a)
    """), {"type": AUDIT_TYPE, "t": title, "d": detail, "a": actor})


def get_broadcast(db, now: datetime) -> dict:
    """Состояние эфира в момент `now` (наивное МСК).

    {"is_on", "playlist_id", "playlist_name", "until_at", "enabled_by", "enabled_at"}.
    Эфир, у которого срок вышел или плейлист удалён, считается выключенным:
    все остальные поля тогда None, как у никогда не включавшегося.
    """
    row = _load(db)
    if not effective_on(row, now):
        return {"is_on": False, "playlist_id": None, "playlist_name": None, "until_at": None,
                "enabled_by": None, "enabled_at": None}
    return {**row, "is_on": True}


def enable(db, playlist_id: int, until_at: datetime | None, actor: str,
           now: datetime | None = None) -> None:
    """Включить общий эфир с плейлистом `playlist_id` до `until_at` (None — до отмены).

    Если эфир уже идёт — это смена плейлиста/срока: в журнале «Общий эфир
    сменён», иначе «Общий эфир включён». Нет такого плейлиста — LookupError,
    ничего не записано. `now` — для тестов, по умолчанию текущее МСК.
    """
    now = now or now_msk()
    pl = db.execute(text("SELECT name FROM playlists WHERE id = :pid"), {"pid": playlist_id}).fetchone()
    if pl is None:
        raise LookupError("Плейлист не найден")
    running = get_broadcast(db, now)["is_on"]

    db.execute(text("""
        INSERT INTO network_broadcast (id, is_on, playlist_id, until_at, enabled_by, enabled_at)
        VALUES (1, TRUE, :pid, :until, :who, :now)
        ON CONFLICT (id) DO UPDATE
        SET is_on = TRUE, playlist_id = EXCLUDED.playlist_id, until_at = EXCLUDED.until_at,
            enabled_by = EXCLUDED.enabled_by, enabled_at = EXCLUDED.enabled_at
    """), {"pid": playlist_id, "until": until_at, "who": actor, "now": now})
    _audit(db, TITLE_CHANGED if running else TITLE_ON,
           f"Плейлист: «{pl.name}» · {until_text(until_at)}", actor)


def disable(db, actor: str, title: str = TITLE_OFF) -> None:
    """Выключить общий эфир (is_on = FALSE, плейлист и срок сброшены) и записать в журнал.

    В детали журнала — плейлист, который шёл. Все читатели (лента экрана,
    список закачки, симулятор, дашборд, предпросмотр показа) смотрят состояние
    через get_broadcast, напрямую таблицу никто не читает.
    """
    row = _load(db)
    detail = f"Плейлист: «{row['playlist_name']}»" if row and row["is_on"] and row["playlist_name"] else None
    db.execute(text("""
        UPDATE network_broadcast
        SET is_on = FALSE, playlist_id = NULL, until_at = NULL
        WHERE id = 1
    """))
    _audit(db, title, detail, actor)


def expire_due(db, now: datetime) -> bool:
    """Выключить эфир, у которого срок вышел (until_at <= now). True — выключили.

    Строка читается под блокировкой (см. _load): новый эфир, включённый в тот же
    миг, не будет сброшен по старому сроку. «До отмены» (until_at IS NULL) не трогаем.
    """
    row = _load(db, lock=True)
    if not row or not row["is_on"] or row["until_at"] is None or row["until_at"] > now:
        return False
    disable(db, SYSTEM_ACTOR, TITLE_EXPIRED)
    return True
