"""Обзор сети («Эфир сети»): что сейчас идёт на каждом экране.

GET /network/live — одним запросом данные для таблицы страницы: все экраны (в сети /
не в сети по единому правилу screen_online), что идёт сейчас и до какого часа, что
будет дальше, что пойдёт после общего эфира, какой ролик по сообщению агента играет,
плюс состояние общего эфира и счётчики. GET /network/playlist/{id}/usage — сколько
слотов недельного шаблона занимает плейлист (панель «О плейлисте»).

Сборку ответа делает чистая build_live (без SQL); что идёт на экране — network_live
.screen_live поверх клеток placements_logic.week_cells (та же логика приоритетов, что
у ленты экрана и «Расписания → По датам»). SQL здесь только пакетный: показы,
переопределения и решения — один раз на сегодня–завтра, «проигрываемые плейлисты» — один
раз на тип площадки (venue_type), а не на экран. Слоты недельного шаблона — на экран
(load_screen_slots). Все даты и часы — наивное МСК; весь расчёт идёт от одного now.
"""
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from broadcast_store import get_broadcast
from deps import get_current_admin, get_db
from network_live import screen_live
from placements_logic import week_cells
from placements_store import (load_decisions, load_overrides, load_placements, load_playlist_names,
                              load_screen_slots, now_msk)
from routers.broadcast import broadcast_json
from screen_online import ONLINE_MINUTES, offline_for_sql, online_sql

router = APIRouter()

DEFAULT_VENUE = "other"


def playing_of(playing_file: str | None, media_id: int | None, media_title: str | None) -> dict | None:
    """«Играет ролик» экрана по последнему сообщению агента (screens.playing_file).

    None — агент ролика не сообщал. Ролик, которого нет в медиатеке (удалён), всё
    равно показывается именем файла: media_id и title тогда None.
    """
    if not playing_file:
        return None
    return {"media_id": media_id, "title": media_title, "filename": playing_file}


def load_screens(db) -> list[dict]:
    """Все экраны: адрес, тип площадки, «в сети» (единое правило) и «играет ролик».

    Ролик — media по имени файла из последнего сообщения агента; при дубле имени
    берётся новейший. Порядок — по названию.
    """
    rows = db.execute(text(f"""
        SELECT s.id, s.name, s.city, s.location,
               COALESCE(s.venue_type, '{DEFAULT_VENUE}') AS venue_type,
               {online_sql("s")} AS online,
               {offline_for_sql("s")} AS offline_for_seconds,
               s.playing_file, m.id AS media_id, m.title AS media_title
        FROM screens s
        LEFT JOIN LATERAL (
            SELECT id, title FROM media WHERE filename = s.playing_file ORDER BY id DESC LIMIT 1
        ) m ON TRUE
        ORDER BY s.name, s.id
    """)).fetchall()
    return [{"id": r.id, "name": r.name, "city": r.city, "location": r.location,
             "venue_type": r.venue_type, "online": bool(r.online),
             "offline_for_seconds": r.offline_for_seconds,
             "playing": playing_of(r.playing_file, r.media_id, r.media_title)} for r in rows]


def load_playable_by_venue(db, venues) -> dict[str, set[int]]:
    """{venue_type: id плейлистов, у которых на такой площадке есть что играть}.

    Фильтры роликов те же, что у ленты экрана (routers/screens.py): готов и одобрен
    модерацией, реклама алкоголя (ст. 21 38-ФЗ) только на площадке «магазин с
    алколицензией», срок показа не вышел. Один запрос на тип площадки.
    """
    out: dict[str, set[int]] = {}
    for venue in sorted(set(venues)):
        rows = db.execute(text("""
            SELECT DISTINCT pi.playlist_id
            FROM playlist_items pi
            JOIN media m ON m.id = pi.media_id
            WHERE m.status = 'ready' AND m.review_status = 'approved'
              AND (m.category IS NULL OR m.category <> 'alcohol' OR :venue = 'store_alcohol')
              AND (m.valid_from  IS NULL OR m.valid_from  <= NOW())
              AND (m.valid_until IS NULL OR m.valid_until >= NOW())
        """), {"venue": venue}).fetchall()
        out[venue] = {r.playlist_id for r in rows}
    return out


def _sort_key(name: str | None) -> str:
    """Ключ сортировки по названию: без учёта регистра, «ё» как «е» (Ёлка между Дом и Жук)."""
    return (name or "").casefold().replace("ё", "е")


def build_live(screens: list[dict], cells_by_screen: dict, now: datetime, broadcast: dict | None,
               playable_by_venue: dict) -> dict:
    """Ответ GET /network/live из готовых данных (без SQL).

    screens — [{"id", "name", "city", "location", "venue_type", "online",
    "offline_for_seconds", "playing"}]; cells_by_screen — {screen_id: клетки
    week_cells на сегодня и завтра}; now — наивное МСК; broadcast — состояние эфира
    (broadcast_store.get_broadcast); playable_by_venue — {venue_type: set id
    плейлистов с проигрываемыми роликами}. Эфир «проигрываем» на экране, если id его
    плейлиста есть в наборе площадки экрана (нет набора — не проигрываем).

    Экраны отсортированы по названию (без учёта регистра, «ё» как «е»; затем по id). В
    broadcast к состоянию добавлены screens_total, offline_screens (экраны не в
    сети, пока эфир действует; иначе 0) и unplayable_screens (экраны, где эфиру
    нечего играть). playlists — {"<id>": {"name"}} для всех плейлистов из now /
    next / after_broadcast экранов (ключи — строки, как в JSON).
    """
    effective = broadcast if broadcast and broadcast.get("is_on") else None
    playlist_id = effective["playlist_id"] if effective else None

    rows: list[dict] = []
    playlists: dict[str, dict] = {}
    for s in sorted(screens, key=lambda s: (_sort_key(s["name"]), s["id"])):
        venue = s.get("venue_type") or DEFAULT_VENUE
        playable = effective is not None and playlist_id in playable_by_venue.get(venue, ())
        live = screen_live(cells_by_screen.get(s["id"], []), now, effective, playable)
        for part in ("now", "next", "after_broadcast"):
            view = live[part]
            for pid, name in zip(view["playlist_ids"], view["playlists"]) if view else ():
                playlists.setdefault(str(pid), {"name": name})
        rows.append({"id": s["id"], "name": s["name"], "city": s.get("city"), "location": s.get("location"),
                     "online": bool(s["online"]), "offline_for_seconds": s.get("offline_for_seconds"),
                     "playing": s.get("playing"), **live})

    online = sum(1 for r in rows if r["online"])
    offline = len(rows) - online
    state = broadcast_json(broadcast) if broadcast else broadcast_json(
        {"is_on": False, "playlist_id": None, "playlist_name": None, "until_at": None,
         "enabled_by": None, "enabled_at": None})
    return {
        "generated_at": now.isoformat(),
        "online_minutes": ONLINE_MINUTES,
        "counts": {"total": len(rows), "online": online, "offline": offline},
        "broadcast": {**state, "screens_total": len(rows),
                      "offline_screens": offline if effective else 0,
                      "unplayable_screens": sum(1 for r in rows if r["broadcast_skipped"])},
        "screens": rows,
        "playlists": playlists,
    }


def live_snapshot(db) -> dict:
    """Загрузить всё одним пакетом и собрать ответ /network/live (см. build_live).

    Окно клеток — сегодня и завтра от одного now: час `now` всегда в клетках, а
    «после эфира» достаёт до срока эфира в завтрашнем дне.
    """
    now = now_msk()
    days = [now.date(), now.date() + timedelta(days=1)]
    broadcast = get_broadcast(db, now)
    screens = load_screens(db)
    playable = load_playable_by_venue(db, {s["venue_type"] for s in screens})

    placements = load_placements(db, days[0], days[-1])
    overrides = load_overrides(db, days[0], days[-1])
    decisions = load_decisions(db, [p["id"] for p in placements])
    slots = {s["id"]: load_screen_slots(db, s["id"]) for s in screens}
    names = load_playlist_names(
        db, {p["playlist_id"] for p in placements}
        | {sl["playlist_id"] for ss in slots.values() for sl in ss}
        | {o["playlist_id"] for o in overrides if o["playlist_id"] is not None})

    cells = {s["id"]: week_cells(days, s["id"], placements, decisions, overrides, slots[s["id"]], names,
                                 playable.get(s["venue_type"], set()))
             for s in screens}
    return build_live(screens, cells, now, broadcast, playable)


@router.get("/network/live")
def network_live(db: Session = Depends(get_db), admin: dict = Depends(get_current_admin)):
    """Обзор сети: экраны, что идёт сейчас / дальше / после эфира, общий эфир, счётчики."""
    return live_snapshot(db)


@router.get("/network/playlist/{playlist_id}/usage")
def playlist_usage(playlist_id: int, db: Session = Depends(get_db), admin: dict = Depends(get_current_admin)):
    """Сколько слотов недельного шаблона занимает плейлист (экранные, групповые и общесетевые)."""
    if not db.execute(text("SELECT 1 FROM playlists WHERE id = :id"), {"id": playlist_id}).first():
        raise HTTPException(404, "Плейлист не найден")
    n = db.execute(text("SELECT COUNT(*) FROM schedule_slots WHERE playlist_id = :id"),
                   {"id": playlist_id}).scalar()
    return {"template_slots": int(n or 0)}
