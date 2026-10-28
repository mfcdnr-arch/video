"""Показы по датам: предпросмотр, создание, список, правка, остановка, отмена, неделя.

Показ ставит плейлист на экраны (или на все экраны сети) на диапазон дат и
ежедневно на целые часы МСК. Пересечения с другими показами и с датовыми
переопределениями находит placements_logic.find_conflicts; решает их админ —
отдельно для каждого экрана («replace» / «alternate» / «yield» — «Оставить
«X» на это время»). Сохранить показ, пока хоть одно пересечение не решено,
нельзя (409). Правка (PUT) идёт тем же путём, что создание, но сам показ
пересечением не считается; предпросмотр правки отдаёт сохранённые решения
показа по нынешним пересечениям — панель подставляет их в выбор.

Остановить можно только идущий показ (status "active"), отменить (удалить) —
только запланированный; статус считает placements_logic.placement_status.
GET /placements/week отдаёт сетку 7 дней × 24 часа для одного экрана
(placements_logic.week_cells).

Чистые функции (parse_body, parse_decisions, unresolved, saved_decisions,
summary_text, overflow_notes, week_start, sort_listing, listing_row,
check_stoppable, check_deletable) проверяются в tests/test_placements_api.py.
SQL живёт в placements_store: разбор строк (развёртывание групп и «всех экранов»,
приоритет экрана над группой, наивные МСК-даты, запись saved_at и
stopped_at) — в tests/test_placements_store.py на фейковой БД. smoke_test.sh
на живом стенде проходит предпросмотр, создание, список, правку, неделю,
остановку и отмену на новом экране, а на текущем часе — одно пересечение:
409 без решения, «заменить», сохранённое решение в предпросмотре правки и
правку на «уступить» с проверкой ленты экрана.
"""
from datetime import date, datetime, timedelta
from typing import Callable

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.orm import Session

from admin_panel import ad_sequence
from deps import get_current_admin, get_db, require_write
from placements_logic import (ALTERNATE, KIND_OVERRIDE, KIND_PLACEMENT, REPLACE, YIELD, alternate_overlaps,
                              find_conflicts, placement_status, week_cells)
from placements_store import (all_screen_ids, delete_placement, get_placement, load_decisions,
                              load_overrides, load_placements, load_playlist_names, load_screen_slots,
                              now_msk, save_placement, stop_placement)
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
                or mode not in (REPLACE, ALTERNATE, YIELD):
            raise HTTPException(400, f"decisions: неверное решение {d!r}")
        out[(sid, kind, oid)] = mode
    return out


def _key(conflict: dict) -> tuple:
    return (conflict["screen_id"], conflict["other_kind"], conflict["other_id"])


def unresolved(conflicts: list[dict], decisions: dict) -> int:
    """Сколько пересечений осталось без решения."""
    return sum(1 for c in conflicts if _key(c) not in decisions)


def saved_decisions(placement_id: int, conflicts: list[dict], decisions: dict) -> list[dict]:
    """Сохранённые решения показа placement_id — только по пересечениям из conflicts.

    decisions — из load_decisions: {(placement_id, screen_id, other_kind,
    other_id): mode}. Решения по пересечениям, которых больше нет (экран
    убрали, другой показ отменили или сдвинули), не отдаются. Порядок — как у
    conflicts; [{"screen_id", "other_kind", "other_id", "mode"}].
    """
    out, seen = [], set()
    for c in conflicts:
        key = _key(c)
        mode = decisions.get((placement_id, *key))
        if mode is not None and key not in seen:
            seen.add(key)
            out.append({"screen_id": key[0], "other_kind": key[1], "other_id": key[2], "mode": mode})
    return out


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


# --- список, неделя, остановка и отмена (чистые функции) -----------------------

STOP_ONLY_ACTIVE = "Остановить можно только идущий показ"
CANCEL_ONLY_PLANNED = "Показ уже идёт или закончился — используйте «Остановить»"
EDIT_STOPPED = "Остановленный показ изменить нельзя — создайте новый"

# В списке сначала идущие, потом запланированные, потом завершённые.
_LISTING_ORDER = {"active": 0, "planned": 1, "done": 2, "stopped": 2}


def week_start(s: str) -> date:
    """Начало недели из query-параметра: дата ГГГГ-ММ-ДД, обязательно понедельник (иначе 400)."""
    d = _date(s, "Начало недели")
    if d.weekday() != 0:
        raise HTTPException(400, f"Начало недели {d:%d.%m.%Y} — не понедельник")
    return d


def sort_listing(rows: list[dict]) -> list[dict]:
    """Порядок списка показов: active, planned, затем done/stopped; внутри — дата начала по убыванию."""
    return sorted(rows, key=lambda r: (_LISTING_ORDER[r["status"]], -r["date_from"].toordinal(), -r["id"]))


def listing_row(p: dict, now: datetime, screen_names: dict[int, str], alternates: list[dict]) -> dict:
    """Строка GET /placements: показ из load_placements + статус на момент now.

    screen_names — {screen_id: название}; alternates — alternate_overlaps(...) для этого показа:
    строки «по очереди с «X»» (mode "alternate") и «уступает «X»» (mode "yield").
    """
    def name(screen_id: int) -> str:
        return screen_names.get(screen_id) or f"Экран #{screen_id}"

    screens = sorted(p["screens"], key=lambda s: (name(s).lower(), s))
    return {
        "id": p["id"], "playlist_id": p["playlist_id"], "all_screens": p["all_screens"],
        "screen_count": len(screens), "screens": [{"id": s, "name": name(s)} for s in screens],
        "date_from": p["date_from"], "date_to": p["date_to"],
        "hour_from": p["hour_from"], "hour_to": p["hour_to"],
        "status": placement_status(p, now),
        "alternates": [{"screen_id": a["screen_id"], "screen_name": name(a["screen_id"]), "mode": a["mode"],
                        "other_playlist_name": a["other_playlist_name"],
                        "date_from": a["date_from"], "date_to": a["date_to"],
                        "hour_from": a["hour_from"], "hour_to": a["hour_to"]} for a in alternates],
        "created_by": p["created_by"], "created_at": p["created_at"],
    }


def check_stoppable(status: str) -> None:
    """Остановить можно только идущий показ; иначе 409."""
    if status != "active":
        raise HTTPException(409, STOP_ONLY_ACTIVE)


def check_deletable(status: str) -> None:
    """Отменить (удалить) можно только запланированный показ; иначе 409."""
    if status != "planned":
        raise HTTPException(409, CANCEL_ONLY_PLANNED)


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
    прошедшую дату начала можно оставить как есть, а "decisions" — его
    сохранённые решения по нынешним пересечениям (панель подставляет их в
    выбор; без exclude_id — []). Ничего не пишет в БД.
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

    stored = saved_decisions(exclude_id, conflicts, load_decisions(db, [exclude_id])) \
        if exclude_id is not None else []

    return {"conflicts": conflicts, "off_days": off_days, "notices": notices, "decisions": stored,
            "overflow": overflow_notes(conflicts, data["playlist_id"], ad_total),
            "summary": summary_text(data, len(data["screens"])),
            "screen_count": len(data["screens"])}


def _save(db: Session, body: dict, actor: str, placement_id: int | None = None) -> int:
    """Создать (placement_id=None) или изменить показ по телу запроса; вернуть его id.

    Общая часть POST и PUT: разобрать решения, найти пересечения (при правке
    сам показ не в счёт), убедиться, что решены все, сохранить и закоммитить.
    """
    decisions = parse_decisions(body.get("decisions") if isinstance(body, dict) else None)
    data, conflicts, _ = _prepare(db, body, placement_id)
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

    placement_id = save_placement(db, data, keep, actor, placement_id)
    db.commit()
    return placement_id


@router.post("/placements")
def create_placement(body: dict = Body(...), admin: dict = Depends(require_write),
                     db: Session = Depends(get_db)):
    """
    Создать показ. Тело — как у предпросмотра плюс "decisions":
    [{"screen_id", "other_kind", "other_id", "mode"}] — по решению на каждое
    пересечение из предпросмотра. Не решено хоть одно — 409, ничего не пишем.
    """
    return {"id": _save(db, body, admin.get("username", "admin"))}


@router.get("/placements")
def list_placements(playlist_id: int = Query(..., description="Показы этого плейлиста"),
                    admin: dict = Depends(get_current_admin), db: Session = Depends(get_db)):
    """
    Показы плейлиста: идущие, затем запланированные, затем закончившиеся и
    остановленные (внутри — дата начала по убыванию). У каждого — экраны,
    статус (planned / active / done / stopped) и строки «по очереди» /
    «уступает» (mode alternate / yield): с каким плейлистом, на каком экране,
    в какие даты и часы (решения админа по пересечениям, см.
    placements_logic.alternate_overlaps).
    """
    now = now_msk()
    mine = load_placements(db, date.min, date.max, playlist_id)
    if not mine:
        return []

    # Чем показы пересекаются — по всей сети в границах дат этих показов.
    lo, hi = min(p["date_from"] for p in mine), max(p["date_to"] for p in mine)
    around = load_placements(db, lo, hi)
    overrides = load_overrides(db, lo, hi)
    decisions = load_decisions(db, [p["id"] for p in around])
    info = _screens_info(db, {sid for p in mine for sid in p["screens"]})
    screen_names = {sid: name for sid, (name, _) in info.items()}

    rows = [listing_row(p, now, screen_names, alternate_overlaps(p, around, overrides, decisions)) for p in mine]
    return sort_listing(rows)


@router.get("/placements/week")
def placements_week(screen_id: int = Query(...), start: str = Query(..., description="Понедельник, ГГГГ-ММ-ДД"),
                    admin: dict = Depends(get_current_admin), db: Session = Depends(get_db)):
    """
    Неделя экрана для сетки «По датам»: 7 дней × 24 часа = 168 клеток
    {"date", "hour", "kind", "playlists", "playlist_ids", "placement_id"}.
    kind: off | placement | alternate | override | template | empty
    (порядок источников — placements_logic.week_cells).
    """
    first = week_start(start)
    if not db.execute(text("SELECT 1 FROM screens WHERE id = :id"), {"id": screen_id}).first():
        raise HTTPException(404, "Экран не найден")

    days = [first + timedelta(days=i) for i in range(7)]
    placements = [p for p in load_placements(db, days[0], days[-1]) if screen_id in p["screens"]]
    overrides = [o for o in load_overrides(db, days[0], days[-1]) if o["screen_id"] == screen_id]
    decisions = load_decisions(db, [p["id"] for p in placements])
    slots = load_screen_slots(db, screen_id)
    names = load_playlist_names(db, {p["playlist_id"] for p in placements}
                                | {s["playlist_id"] for s in slots}
                                | {o["playlist_id"] for o in overrides if o["playlist_id"] is not None})
    return {"days": [d.isoformat() for d in days],
            "cells": week_cells(days, screen_id, placements, decisions, overrides, slots, names)}


@router.put("/placements/{placement_id}")
def update_placement(placement_id: int, body: dict = Body(...), admin: dict = Depends(require_write),
                     db: Session = Depends(get_db)):
    """
    Изменить показ: тело и ответ — как у создания. Прошедшая дата начала
    допустима, если её не меняли; сам показ пересечением не считается.
    Сохранённый показ становится новейшим (saved_at), его решения по
    пересечениям записываются заново. Остановленный показ не правится — 409.
    """
    current = get_placement(db, placement_id)
    if current is None:
        raise HTTPException(404, "Показ не найден")
    if current["stopped_at"] is not None:
        raise HTTPException(409, EDIT_STOPPED)
    return {"id": _save(db, body, admin.get("username", "admin"), placement_id)}


@router.post("/placements/{placement_id}/stop")
def stop_placement_now(placement_id: int, admin: dict = Depends(require_write),
                       db: Session = Depends(get_db)):
    """
    Остановить идущий показ с текущего момента: прошедшие часы остаются в
    истории, остальные не идут. Не идущий (запланирован, закончился, уже
    остановлен) — 409.
    """
    p = get_placement(db, placement_id)
    if p is None:
        raise HTTPException(404, "Показ не найден")
    now = now_msk()
    check_stoppable(placement_status(p, now))
    if not stop_placement(db, p, admin.get("username", "admin"), now):
        raise HTTPException(409, STOP_ONLY_ACTIVE)     # кто-то остановил между проверкой и записью
    db.commit()
    return {"status": "stopped", "stopped_at": now}


@router.delete("/placements/{placement_id}")
def cancel_placement(placement_id: int, admin: dict = Depends(require_write),
                     db: Session = Depends(get_db)):
    """Отменить запланированный показ — он удаляется. Идущий или закончившийся — 409."""
    p = get_placement(db, placement_id)
    if p is None:
        raise HTTPException(404, "Показ не найден")
    check_deletable(placement_status(p, now_msk()))
    delete_placement(db, p, admin.get("username", "admin"))
    db.commit()
    return {"status": "deleted"}
