"""«Что идёт сейчас, до какого часа и что дальше» для страницы «Эфир сети».

Чистая логика на stdlib, без БД и без fastapi (как placements_logic): на вход —
клетки недельной сетки одного экрана, на выход — простой dict для ответа API.

Все даты и часы — наивное МСК. ISO-строки — `datetime.isoformat()` наивного
значения («2026-10-28T13:00:00»), как у `active_until` ленты экрана.
"""
from datetime import datetime, time

KIND_BROADCAST = "broadcast"

_EMPTY_CELL = {"kind": "empty", "playlist_ids": [], "playlists": []}


def _view(cell: dict) -> dict:
    """Клетка в виде {"kind", "playlist_ids", "playlists"} (списки — копии)."""
    return {"kind": cell["kind"], "playlist_ids": list(cell["playlist_ids"]),
            "playlists": list(cell["playlists"])}


def _same_source(a: dict, b: dict) -> bool:
    """Одинаковый источник и набор плейлистов — для экрана смены нет."""
    return a["kind"] == b["kind"] and list(a["playlist_ids"]) == list(b["playlist_ids"])


def screen_live(cells: list[dict], now: datetime, broadcast: dict | None, broadcast_playable: bool) -> dict:
    """Что идёт на одном экране сейчас, до какого часа и что будет дальше.

    cells — клетки `placements_logic.week_cells` этого экрана на сегодня и
    завтра (48 шт.); now — наивное МСК; broadcast — состояние общего эфира
    (`broadcast_store.get_broadcast`), если он действует, иначе None;
    broadcast_playable — есть ли у плейлиста эфира что играть на этом экране
    (площадка, модерация, срок роликов — как в ленте).

    Возвращает
      {"now":  {"kind", "playlist_ids", "playlists", "until"},
       "next": {"kind", "playlist_ids", "playlists", "from"} | None,
       "after_broadcast": {"kind", "playlist_ids", "playlists"} | None,
       "broadcast_skipped": bool}

    now.kind — "broadcast", если эфир действует и ему есть что играть на
    экране; иначе kind текущей клетки (час `now`). now.until: для эфира — его
    срок (None — «до отмены»); иначе начало первого часа СЕГОДНЯ, где источник
    или набор плейлистов (kind, playlist_ids) другой; None — до конца дня
    смены нет («до конца дня без изменений», текст пишет фронт). next — эта же
    первая отличающаяся клетка (from — её начало) или None; при действующем
    эфире next всегда None: расписание на это время перекрыто, вместо него —
    after_broadcast: клетка на момент срока эфира (час, в который попал
    срок; за пределами 48 клеток — None) либо текущая клетка, если «до отмены».
    broadcast_skipped — эфир действует, но на этом экране играть нечего: идёт
    расписание (now — его клетка с ближайшей сменой), экран эфир пропускает.
    """
    if broadcast is not None and not broadcast.get("is_on", True):
        broadcast = None                          # выключенный эфир в виде get_broadcast — это не эфир

    by_hour = {(c["date"], c["hour"]): c for c in cells}
    today = now.date()
    current = by_hour.get((today, now.hour), _EMPTY_CELL)

    change = None                                 # первая клетка сегодня, где источник/плейлисты другие
    for hour in range(now.hour + 1, 24):
        cell = by_hour.get((today, hour))
        if cell is not None and not _same_source(cell, current):
            change = cell
            break
    change_at = datetime.combine(change["date"], time(change["hour"])).isoformat() if change else None

    until_at = broadcast["until_at"] if broadcast is not None else None
    if broadcast is not None and broadcast_playable:
        pid = broadcast["playlist_id"]
        now_view = {"kind": KIND_BROADCAST, "playlist_ids": [pid],
                    "playlists": [broadcast.get("playlist_name") or f"Плейлист #{pid}"],
                    "until": until_at.isoformat() if until_at is not None else None}
    else:
        now_view = {**_view(current), "until": change_at}

    next_view = {**_view(change), "from": change_at} if change is not None and broadcast is None else None

    after = None
    if broadcast is not None:
        cell = current if until_at is None else by_hour.get((until_at.date(), until_at.hour))
        after = _view(cell) if cell is not None else None

    return {"now": now_view, "next": next_view, "after_broadcast": after,
            "broadcast_skipped": broadcast is not None and not broadcast_playable}
