"""Показы по датам: предпросмотр пересечений и создание.

Показ ставит плейлист на экраны (или на все экраны сети) на диапазон дат и
ежедневно на целые часы МСК. Пересечения с другими показами и с датовыми
переопределениями находит placements_logic.find_conflicts; решает их админ —
отдельно для каждого экрана («replace» / «alternate»). Сохранить показ, пока
хоть одно пересечение не решено, нельзя (409).

Чистые функции (parse_body, parse_decisions, unresolved, summary_text,
overflow_notes) проверяются в tests/test_placements_api.py; SQL — в
placements_store, его проверяет smoke_test.sh на живом стенде.
"""
from datetime import date
from typing import Callable

from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from admin_panel import ad_sequence
from deps import get_db, require_write
from placements_logic import ALTERNATE, KIND_OVERRIDE, KIND_PLACEMENT, REPLACE, find_conflicts
from placements_store import all_screen_ids, load_overrides, load_placements, now_msk, save_placement
from playlist_rules import overflow_minutes

router = APIRouter()

BROADCAST_NOTICE = "Сейчас включён «Эфир сети» — пока он включён, показы на экранах не идут"


# --- чистые функции ----------------------------------------------------------

def _int(value) -> int | None:
    """Целое из JSON (int или строка цифр); bool и дроби — не целые."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:          # «²», «9.5», пусто — не целое
            return None
    return None


def _date(value, label: str) -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        raise HTTPException(400, f"{label}: ожидается дата ГГГГ-ММ-ДД")


def parse_body(body: dict, today: date, existing_date_from: date | None = None) -> dict:
    """Проверить тело показа и привести его к формату placements_logic.

    400 — нет плейлиста, нет экранов (если не «все экраны»), кривые даты,
    начало в прошлом (кроме правки, где начало не менялось —
    existing_date_from), конец раньше начала, часы вне 0–23 / 1–24 или
    конец не позже начала.

    Возвращает {playlist_id, all_screens, screens, date_from, date_to,
    hour_from, hour_to}. Для «все экраны» screens = set(): разворачивает
    роутер (all_screen_ids), у этой функции нет БД.
    """
    if not isinstance(body, dict):
        raise HTTPException(400, "Ожидается JSON-объект показа")

    playlist_id = _int(body.get("playlist_id"))
    if not playlist_id or playlist_id <= 0:
        raise HTTPException(400, "Не выбран плейлист")

    all_screens = body.get("all_screens") is True
    screens: set[int] = set()
    if not all_screens:
        raw = body.get("screen_ids")
        if not isinstance(raw, list) or not raw:
            raise HTTPException(400, "Не выбраны экраны")
        for item in raw:
            sid = _int(item)
            if sid is None or sid <= 0:
                raise HTTPException(400, f"Неверный id экрана: {item!r}")
            screens.add(sid)

    date_from = _date(body.get("date_from"), "Дата начала")
    date_to = _date(body.get("date_to"), "Дата окончания")
    if date_from < today and date_from != existing_date_from:
        raise HTTPException(400, f"Дата начала {date_from:%d.%m.%Y} уже прошла")
    if date_to < date_from:
        raise HTTPException(400, "Дата окончания раньше даты начала")

    hour_from, hour_to = _int(body.get("hour_from")), _int(body.get("hour_to"))
    if hour_from is None or hour_to is None or not (0 <= hour_from <= 23) \
            or not (1 <= hour_to <= 24) or hour_to <= hour_from:
        raise HTTPException(400, "Часы показа: начало 0–23, конец 1–24 и позже начала")

    return {"playlist_id": playlist_id, "all_screens": all_screens, "screens": screens,
            "date_from": date_from, "date_to": date_to,
            "hour_from": hour_from, "hour_to": hour_to}


def parse_decisions(raw) -> dict:
    """[{"screen_id","other_kind","other_id","mode"}] → {(screen_id, other_kind, other_id): mode}; 400 на мусор."""
    if raw is None:
        return {}
    if not isinstance(raw, list):
        raise HTTPException(400, "decisions: ожидается список решений")
    out = {}
    for d in raw:
        if not isinstance(d, dict):
            raise HTTPException(400, "decisions: ожидается список объектов")
        sid, oid = _int(d.get("screen_id")), _int(d.get("other_id"))
        kind, mode = d.get("other_kind"), d.get("mode")
        if sid is None or oid is None or kind not in (KIND_PLACEMENT, KIND_OVERRIDE) \
                or mode not in (REPLACE, ALTERNATE):
            raise HTTPException(400, f"decisions: неверное решение {d!r}")
        out[(sid, kind, oid)] = mode
    return out


def _key(conflict: dict) -> tuple:
    return (conflict["screen_id"], conflict["other_kind"], conflict["other_id"])


def unresolved(conflicts: list[dict], decisions: dict) -> int:
    """Сколько пересечений осталось без решения."""
    return sum(1 for c in conflicts if _key(c) not in decisions)


def _hh(hour: int) -> str:
    return f"{hour:02d}:00"


def summary_text(data: dict, screen_count: int) -> str:
    """Итоговая фраза предпросмотра: куда, когда и в какие часы пойдёт плейлист."""
    where = (f"на всех экранах сети ({screen_count})" if data["all_screens"]
             else f"на {screen_count} экран(ах)")
    d1, d2 = data["date_from"], data["date_to"]
    start = f"{d1:%d.%m}" if d1.year == d2.year else f"{d1:%d.%m.%Y}"
    return (f"Плейлист пойдёт {where} с {start} по {d2:%d.%m.%Y}, "
            f"ежедневно {_hh(data['hour_from'])}–{_hh(data['hour_to'])}. "
            "В остальное время экраны показывают своё обычное расписание.")


def overflow_notes(conflicts: list[dict], playlist_id: int,
                   ad_total: Callable[[int, int], float]) -> list[str]:
    """Предупреждения «реклама двух плейлистов не помещается в час».

    ad_total(playlist_id, screen_id) — секунды рекламы плейлиста на экране
    (с повторами). Пересечение с тем же плейлистом пропускается: по очереди
    он сам с собой не играет. Одинаковые фразы (то же пересечение на других
    экранах) — один раз.
    """
    notes: list[str] = []
    for c in conflicts:
        other = c.get("other_playlist_id")
        if other is None or other == playlist_id:
            continue
        minutes = overflow_minutes(ad_total(playlist_id, c["screen_id"]) + ad_total(other, c["screen_id"]))
        if minutes is not None:
            notes.append(f"в {c['hour_from']}:00 ролики двух плейлистов занимают {minutes} мин — "
                         "в час не помещаются, часть выйдет реже")
    return list(dict.fromkeys(notes))


# --- общая часть предпросмотра и создания -------------------------------------

def _prepare(db: Session, body: dict, exclude_id: int | None = None) -> tuple[dict, list, list]:
    """Разобрать тело, проверить плейлист и экраны, найти пересечения.

    Возвращает (данные показа с развёрнутыми screens и id, пересечения,
    выключенные дни).
    """
    existing_date_from = None
    if exclude_id is not None:
        existing_date_from = db.execute(text("SELECT date_from FROM placements WHERE id = :id"),
                                        {"id": exclude_id}).scalar()
        if existing_date_from is None:
            raise HTTPException(404, "Показ не найден")

    data = parse_body(body, now_msk().date(), existing_date_from)

    if not db.execute(text("SELECT 1 FROM playlists WHERE id = :p"), {"p": data["playlist_id"]}).first():
        raise HTTPException(404, "Плейлист не найден")

    every = all_screen_ids(db)
    if data["all_screens"]:
        data["screens"] = every
        if not every:
            raise HTTPException(400, "В сети нет ни одного экрана")
    else:
        missing = sorted(data["screens"] - every)
        if missing:
            raise HTTPException(400, "Экран не найден: " + ", ".join(f"#{s}" for s in missing))

    new = {**data, "id": exclude_id}
    others = load_placements(db, data["date_from"], data["date_to"])
    overrides = load_overrides(db, data["date_from"], data["date_to"])
    conflicts, off_days = find_conflicts(new, others, overrides)
    return new, conflicts, off_days


def _screens_info(db: Session, ids: set[int]) -> dict:
    """{screen_id: (name, venue_type)} для указанных экранов."""
    if not ids:
        return {}
    rows = db.execute(text("""
        SELECT id, name, COALESCE(venue_type, 'other') AS venue_type
        FROM screens WHERE id = ANY(:ids)
    """), {"ids": sorted(ids)}).fetchall()
    return {r.id: (r.name, r.venue_type) for r in rows}


# --- эндпоинты ---------------------------------------------------------------

@router.post("/placements/preview")
def preview_placement(body: dict = Body(...), admin: dict = Depends(require_write),
                      db: Session = Depends(get_db)):
    """
    Что будет, если сохранить показ: пересечения (их решает админ), дни, где
    экран выключен переопределением, предупреждения и итоговая фраза.
    Для правки показа — "exclude_id": сам показ пересечением не считается,
    а прошедшую дату начала можно оставить как есть. Ничего не пишет в БД.
    """
    exclude_id = body.get("exclude_id") if isinstance(body, dict) else None
    if exclude_id is not None:
        exclude_id = _int(exclude_id)
        if exclude_id is None:
            raise HTTPException(400, "exclude_id: ожидается id показа")
    data, conflicts, off_days = _prepare(db, body, exclude_id)

    info = _screens_info(db, {c["screen_id"] for c in conflicts} | {d["screen_id"] for d in off_days})
    for item in conflicts + off_days:
        item["screen_name"] = info.get(item["screen_id"], (f"Экран #{item['screen_id']}", "other"))[0]

    notices = []
    if db.execute(text("SELECT is_on FROM network_broadcast WHERE id = 1")).scalar():
        notices.append(BROADCAST_NOTICE)

    cache: dict[tuple[int, str], float] = {}

    def ad_total(playlist_id: int, screen_id: int) -> float:
        venue = info.get(screen_id, ("", "other"))[1]
        if (playlist_id, venue) not in cache:
            cache[(playlist_id, venue)] = sum(sec for _, sec in ad_sequence(db, playlist_id, venue))
        return cache[(playlist_id, venue)]

    return {"conflicts": conflicts, "off_days": off_days, "notices": notices,
            "overflow": overflow_notes(conflicts, data["playlist_id"], ad_total),
            "summary": summary_text(data, len(data["screens"])),
            "screen_count": len(data["screens"])}


@router.post("/placements")
def create_placement(body: dict = Body(...), admin: dict = Depends(require_write),
                     db: Session = Depends(get_db)):
    """
    Создать показ. Тело — как у предпросмотра плюс "decisions":
    [{"screen_id", "other_kind", "other_id", "mode"}] — по решению на каждое
    пересечение из предпросмотра. Не решено хоть одно — 409, ничего не пишем.
    """
    decisions = parse_decisions(body.get("decisions") if isinstance(body, dict) else None)
    data, conflicts, _ = _prepare(db, body)
    left = unresolved(conflicts, decisions)
    if left:
        raise HTTPException(409, f"Не решено пересечений: {left}")

    # Храним решения только по действительным пересечениям — лишние (экран
    # не из показа, давно удалённый показ) в таблицу не попадают.
    keep = []
    for c in conflicts:
        k = _key(c)
        keep.append({"screen_id": k[0], "other_kind": k[1], "other_id": k[2], "mode": decisions[k]})
    keep = list({_key(d): d for d in keep}.values())

    placement_id = save_placement(db, data, keep, admin.get("username", "admin"))
    db.commit()
    return {"id": placement_id}
