"""Какие ролики можно играть на экране — одно правило для всех выборок эфира.

Ролик проигрываемый на площадке экрана, если:
  • он готов (status 'ready') и одобрен модерацией (review_status 'approved', 38-ФЗ);
  • это не реклама алкоголя (category 'alcohol') — она идёт только на площадке
    «магазин с алколицензией» (ст. 21 38-ФЗ);
  • срок показа не вышел: valid_from ≤ сейчас ≤ valid_until (пустая граница не
    ограничивает). «Сейчас» — часы БД (NOW()).

Правило — SQL-фрагмент playable_sql с параметром :venue (тип площадки экрана). Его
берут лента экрана (load_playable_items), часовой блок (admin_panel.ad_sequence) и
«проигрываемые плейлисты» обзора сети и списка плейлистов (load_playable_by_venue).
Симулятор (routers/schedule.py) проверяет срок на выбранную дату, а не на «сейчас»:
у него свой вариант, с той же частью про площадку (venue_sql).

Здесь только SQL, без commit.
"""
from sqlalchemy import text

LICENSED_VENUE = "store_alcohol"      # площадка с алколицензией: ей разрешена реклама алкоголя
UNLICENSED_VENUE = "other"            # любая другая площадка (store / mall / office / other) — без неё
DEFAULT_VENUE = "other"               # тип площадки экрана, у которого он не задан (venue_type NULL)


def venue_sql(alias: str = "m") -> str:
    """SQL-условие «ролик допустим на площадке :venue» (реклама алкоголя — только с алколицензией)."""
    return (f"({alias}.category IS NULL OR {alias}.category <> 'alcohol' "
            f"OR :venue = '{LICENSED_VENUE}')")


def playable_sql(alias: str = "m") -> str:
    """SQL-условие «ролик можно играть сейчас на площадке :venue» для таблицы media под алиасом alias."""
    a = alias
    return (f"{a}.status = 'ready' AND {a}.review_status = 'approved' AND {venue_sql(a)} "
            f"AND ({a}.valid_from IS NULL OR {a}.valid_from <= NOW()) "
            f"AND ({a}.valid_until IS NULL OR {a}.valid_until >= NOW())")


def load_playable_items(db, playlist_id: int, venue: str) -> list[dict]:
    """Проигрываемые ролики плейлиста на площадке venue по порядку (повторы не развёрнуты).

    [{"media_id", "filename", "filesize", "md5_hash", "repeat_count", "position"}] —
    так их отдаёт агенту лента экрана (routers/screens.py).
    """
    return [dict(r._mapping) for r in db.execute(text(f"""
        SELECT m.id AS media_id, m.filename, m.filesize, m.md5_hash,
               pi.repeat_count, pi.position
        FROM playlist_items pi
        JOIN media m ON m.id = pi.media_id
        WHERE pi.playlist_id = :p AND {playable_sql("m")}
        ORDER BY pi.position
    """), {"p": playlist_id, "venue": venue}).fetchall()]


def load_playable_by_venue(db, venues) -> dict[str, set[int]]:
    """{venue_type: id плейлистов, у которых на такой площадке есть что играть}.

    Фильтр роликов — тот же playable_sql, что у ленты экрана. Один запрос на тип площадки.
    """
    out: dict[str, set[int]] = {}
    for venue in sorted(set(venues)):
        rows = db.execute(text(f"""
            SELECT DISTINCT pi.playlist_id
            FROM playlist_items pi
            JOIN media m ON m.id = pi.media_id
            WHERE {playable_sql("m")}
        """), {"venue": venue}).fetchall()
        out[venue] = {r.playlist_id for r in rows}
    return out
