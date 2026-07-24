"""Недельное расписание и датовые переопределения."""
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.orm import Session

from deps import get_db, get_current_admin, require_write

router = APIRouter()


def _slot_target(screen_id, group_id, network):
    """Ровно один уровень: экран / группа / вся сеть. Возвращает (where, params, label)."""
    chosen = [x for x in (screen_id is not None, group_id is not None, bool(network)) if x]
    if len(chosen) != 1:
        raise HTTPException(400, "Укажите ровно одну цель: screen_id, group_id или network=true")
    if screen_id is not None:
        return "screen_id = :sid", {"sid": screen_id}, f"экран #{screen_id}"
    if group_id is not None:
        return "screen_id IS NULL AND group_id = :gid", {"gid": group_id}, f"группа #{group_id}"
    return "screen_id IS NULL AND group_id IS NULL", {}, "вся сеть"


@router.post("/schedule")
def set_schedule(
    day_of_week: int = Query(..., ge=0, le=6, description="0=Пн, 6=Вс"),
    playlist_id: int = Query(...),
    hour: Optional[int] = Query(None, ge=0, le=23, description="Пусто = слот на весь день"),
    screen_id: Optional[int] = Query(None),
    group_id: Optional[int] = Query(None),
    network: bool = Query(False, description="Слот всей сети (по умолчанию для всех экранов)"),
    admin: dict = Depends(require_write),
    db: Session = Depends(get_db)
):
    """
    Задать слот недельного шаблона на одном из трёх уровней.
    Приоритет при воспроизведении: экран > группа > сеть
    (резолюция выполняется сервером в /schedule/minipc/{screen_id}).
    """
    where, params, label = _slot_target(screen_id, group_id, network)
    pl = db.execute(text("SELECT id, name FROM playlists WHERE id = :p"), {"p": playlist_id}).fetchone()
    if not pl:
        raise HTTPException(404, "Плейлист не найден")

    # заменяем слот этого уровня на этот день/час (hour может быть NULL)
    db.execute(text(f"""
        DELETE FROM schedule_slots
        WHERE {where} AND day_of_week = :d AND hour IS NOT DISTINCT FROM :h
    """), {**params, "d": day_of_week, "h": hour})
    db.execute(text("""
        INSERT INTO schedule_slots (screen_id, group_id, day_of_week, hour, playlist_id)
        VALUES (:sid, :gid, :d, :h, :p)
    """), {"sid": screen_id, "gid": group_id if screen_id is None else None,
           "d": day_of_week, "h": hour, "p": playlist_id})
    try:
        db.execute(text("""
            INSERT INTO audit_log (event_type, title, detail, actor, screen_id)
            VALUES ('schedule', :t, :dt, :a, :sid)
        """), {"t": f"Слот расписания: {label}",
               "dt": f"день {day_of_week}, час {'весь день' if hour is None else hour} → «{pl.name}»",
               "a": admin.get("username", "admin"), "sid": screen_id})
    except Exception:
        pass
    db.commit()
    return {"status": "ok", "target": label, "day_of_week": day_of_week, "hour": hour,
            "playlist_id": playlist_id}


@router.delete("/schedule/slot")
def delete_slot(
    day_of_week: int = Query(..., ge=0, le=6),
    hour: Optional[int] = Query(None, ge=0, le=23),
    screen_id: Optional[int] = Query(None),
    group_id: Optional[int] = Query(None),
    network: bool = Query(False),
    admin: dict = Depends(require_write),
    db: Session = Depends(get_db)
):
    """Убрать слот недельного шаблона на выбранном уровне."""
    where, params, label = _slot_target(screen_id, group_id, network)
    db.execute(text(f"""
        DELETE FROM schedule_slots
        WHERE {where} AND day_of_week = :d AND hour IS NOT DISTINCT FROM :h
    """), {**params, "d": day_of_week, "h": hour})
    try:
        db.execute(text("""
            INSERT INTO audit_log (event_type, title, detail, actor, screen_id)
            VALUES ('schedule', :t, :dt, :a, :sid)
        """), {"t": f"Слот удалён: {label}",
               "dt": f"день {day_of_week}, час {'весь день' if hour is None else hour}",
               "a": admin.get("username", "admin"), "sid": screen_id})
    except Exception:
        pass
    db.commit()
    return {"status": "deleted", "target": label}


@router.delete("/schedule/slots/clear")
def clear_slots(
    day_of_week: Optional[int] = Query(None, ge=0, le=6),
    screen_id: Optional[int] = Query(None),
    group_id: Optional[int] = Query(None),
    network: bool = Query(False),
    admin: dict = Depends(require_write),
    db: Session = Depends(get_db)
):
    """Массово убрать слоты недельного шаблона на выбранном уровне: все сразу
    либо только за указанный день недели (day_of_week)."""
    where, params, label = _slot_target(screen_id, group_id, network)
    day_sql = ""
    if day_of_week is not None:
        day_sql = " AND day_of_week = :d"
        params["d"] = day_of_week
    res = db.execute(text(f"DELETE FROM schedule_slots WHERE {where}{day_sql}"), params)
    removed = res.rowcount if res.rowcount is not None else 0
    try:
        db.execute(text("""
            INSERT INTO audit_log (event_type, title, detail, actor, screen_id)
            VALUES ('schedule', :t, :dt, :a, :sid)
        """), {"t": f"Расписание очищено: {label}",
               "dt": (f"день {day_of_week}" if day_of_week is not None else "весь шаблон")
                     + f", удалено слотов: {removed}",
               "a": admin.get("username", "admin"), "sid": screen_id})
    except Exception:
        pass
    db.commit()
    return {"status": "cleared", "removed": removed, "target": label}


@router.get("/schedule/slots")
def get_slots(
    screen_id: Optional[int] = Query(None),
    group_id: Optional[int] = Query(None),
    network: bool = Query(False),
    admin: dict = Depends(get_current_admin),
    db: Session = Depends(get_db)
):
    """Слоты ровно одного уровня (для редактора недельной сетки в панели)."""
    where, params, _ = _slot_target(screen_id, group_id, network)
    rows = db.execute(text(f"""
        SELECT ss.day_of_week, ss.hour, p.id AS playlist_id, p.name AS playlist_name
        FROM schedule_slots ss JOIN playlists p ON p.id = ss.playlist_id
        WHERE {where}
        ORDER BY ss.day_of_week, ss.hour NULLS FIRST
    """), params).fetchall()
    return [dict(r._mapping) for r in rows]


@router.get("/schedule/overrides")
def get_overrides(
    month: str = Query(..., description="YYYY-MM"),
    screen_id: Optional[int] = Query(None),
    group_id: Optional[int] = Query(None),
    admin: dict = Depends(get_current_admin),
    db: Session = Depends(get_db)
):
    """Возвращает переопределения за указанный месяц для экрана или группы."""
    try:
        year, mon = map(int, month.split("-"))
    except Exception:
        raise HTTPException(status_code=400, detail="month must be YYYY-MM")
    date_from = date(year, mon, 1)
    if mon == 12:
        date_to = date(year + 1, 1, 1)
    else:
        date_to = date(year, mon + 1, 1)

    if screen_id is not None:
        rows = db.execute(text("""
            SELECT so.id, so.on_date, so.playlist_id, p.name AS playlist_name, so.is_off
            FROM schedule_overrides so
            LEFT JOIN playlists p ON p.id = so.playlist_id
            WHERE so.screen_id = :sid AND so.on_date >= :df AND so.on_date < :dt
            ORDER BY so.on_date
        """), {"sid": screen_id, "df": date_from, "dt": date_to}).fetchall()
    elif group_id is not None:
        rows = db.execute(text("""
            SELECT so.id, so.on_date, so.playlist_id, p.name AS playlist_name, so.is_off
            FROM schedule_overrides so
            LEFT JOIN playlists p ON p.id = so.playlist_id
            WHERE so.group_id = :gid AND so.on_date >= :df AND so.on_date < :dt
            ORDER BY so.on_date
        """), {"gid": group_id, "df": date_from, "dt": date_to}).fetchall()
    else:
        raise HTTPException(status_code=400, detail="screen_id or group_id required")
    return [dict(r._mapping) for r in rows]


@router.post("/schedule/overrides")
def set_override(
    on_date: str = Query(..., description="YYYY-MM-DD"),
    screen_id: Optional[int] = Query(None),
    group_id: Optional[int] = Query(None),
    playlist_id: Optional[int] = Query(None),
    is_off: bool = Query(False),
    admin: dict = Depends(require_write),
    db: Session = Depends(get_db)
):
    """Создаёт или обновляет переопределение расписания на конкретную дату."""
    if screen_id is None and group_id is None:
        raise HTTPException(status_code=400, detail="screen_id or group_id required")
    try:
        override_date = date.fromisoformat(on_date)
    except Exception:
        raise HTTPException(status_code=400, detail="on_date must be YYYY-MM-DD")

    if screen_id is not None:
        db.execute(text("""
            DELETE FROM schedule_overrides WHERE screen_id = :sid AND on_date = :d
        """), {"sid": screen_id, "d": override_date})
        db.execute(text("""
            INSERT INTO schedule_overrides (screen_id, on_date, playlist_id, is_off, created_by)
            VALUES (:sid, :d, :pid, :off, :by)
        """), {"sid": screen_id, "d": override_date, "pid": playlist_id,
               "off": is_off, "by": admin.get("sub", "")})
    else:
        db.execute(text("""
            DELETE FROM schedule_overrides WHERE group_id = :gid AND on_date = :d
        """), {"gid": group_id, "d": override_date})
        db.execute(text("""
            INSERT INTO schedule_overrides (group_id, on_date, playlist_id, is_off, created_by)
            VALUES (:gid, :d, :pid, :off, :by)
        """), {"gid": group_id, "d": override_date, "pid": playlist_id,
               "off": is_off, "by": admin.get("sub", "")})
    try:
        target = f"экран #{screen_id}" if screen_id is not None else f"группа #{group_id}"
        what = "показ выключен" if is_off else f"плейлист #{playlist_id}"
        db.execute(text("""
            INSERT INTO audit_log (event_type, title, detail, actor, screen_id)
            VALUES ('schedule', :t, :dt, :a, :sid)
        """), {"t": f"Переопределение даты: {target}",
               "dt": f"{override_date}: {what}",
               "a": admin.get("username", "admin"), "sid": screen_id})
    except Exception:
        pass
    db.commit()
    return {"status": "ok"}


@router.delete("/schedule/overrides")
def delete_overrides(
    month: str = Query(..., description="YYYY-MM"),
    screen_id: Optional[int] = Query(None),
    group_id: Optional[int] = Query(None),
    admin: dict = Depends(require_write),
    db: Session = Depends(get_db)
):
    """Удаляет все переопределения за месяц для экрана или группы."""
    try:
        year, mon = map(int, month.split("-"))
    except Exception:
        raise HTTPException(status_code=400, detail="month must be YYYY-MM")
    date_from = date(year, mon, 1)
    if mon == 12:
        date_to = date(year + 1, 1, 1)
    else:
        date_to = date(year, mon + 1, 1)

    if screen_id is not None:
        db.execute(text("""
            DELETE FROM schedule_overrides
            WHERE screen_id = :sid AND on_date >= :df AND on_date < :dt
        """), {"sid": screen_id, "df": date_from, "dt": date_to})
    elif group_id is not None:
        db.execute(text("""
            DELETE FROM schedule_overrides
            WHERE group_id = :gid AND on_date >= :df AND on_date < :dt
        """), {"gid": group_id, "df": date_from, "dt": date_to})
    else:
        raise HTTPException(status_code=400, detail="screen_id or group_id required")
    try:
        target = f"экран #{screen_id}" if screen_id is not None else f"группа #{group_id}"
        db.execute(text("""
            INSERT INTO audit_log (event_type, title, detail, actor, screen_id)
            VALUES ('schedule', :t, :dt2, :a, :sid)
        """), {"t": f"Сброшены переопределения месяца: {target}",
               "dt2": f"месяц {month}", "a": admin.get("username", "admin"),
               "sid": screen_id})
    except Exception:
        pass
    db.commit()
    return {"status": "ok"}


@router.post("/schedule/clone")
def clone_schedule(
    from_screen_id: int = Query(...),
    to_screen_id: Optional[int] = Query(None),
    to_group_id: Optional[int] = Query(None),
    admin: dict = Depends(require_write),
    db: Session = Depends(get_db)
):
    """
    Скопировать недельное расписание экрана на другой экран или на все
    экраны группы. Существующее расписание целевых экранов ЗАМЕНЯЕТСЯ.
    """
    if to_screen_id is None and to_group_id is None:
        raise HTTPException(400, "Укажите to_screen_id или to_group_id")

    src = db.execute(text("SELECT id, name FROM screens WHERE id = :id"),
                     {"id": from_screen_id}).fetchone()
    if not src:
        raise HTTPException(404, "Экран-источник не найден")

    slots = db.execute(text("""
        SELECT day_of_week, hour, playlist_id FROM schedule_slots
        WHERE screen_id = :sid
    """), {"sid": from_screen_id}).fetchall()
    if not slots:
        raise HTTPException(400, "У экрана-источника нет расписания")

    if to_screen_id is not None:
        if to_screen_id == from_screen_id:
            raise HTTPException(400, "Источник и цель совпадают")
        tgt = db.execute(text("SELECT id FROM screens WHERE id = :id"),
                         {"id": to_screen_id}).fetchone()
        if not tgt:
            raise HTTPException(404, "Целевой экран не найден")
        targets = [to_screen_id]
    else:
        rows = db.execute(text(
            "SELECT id FROM screens WHERE group_id = :gid AND id <> :src"
        ), {"gid": to_group_id, "src": from_screen_id}).fetchall()
        targets = [r.id for r in rows]
        if not targets:
            raise HTTPException(400, "В группе нет других экранов")

    for tid in targets:
        db.execute(text("DELETE FROM schedule_slots WHERE screen_id = :sid"), {"sid": tid})
        for s in slots:
            db.execute(text("""
                INSERT INTO schedule_slots (screen_id, day_of_week, hour, playlist_id)
                VALUES (:sid, :d, :h, :p)
            """), {"sid": tid, "d": s.day_of_week, "h": s.hour, "p": s.playlist_id})

    try:
        db.execute(text("""
            INSERT INTO audit_log (event_type, title, detail, actor, screen_id)
            VALUES ('schedule', :ti, :d, :a, :sid)
        """), {"ti": f"Расписание скопировано с «{src.name}»",
               "d": f"на {len(targets)} экран(ов), слотов: {len(slots)}",
               "a": admin.get("username", "admin"), "sid": from_screen_id})
    except Exception:
        pass
    db.commit()
    return {"status": "ok", "targets": targets, "slots_copied": len(slots)}


@router.get("/schedule/simulate")
def simulate_schedule(
    screen_id: int = Query(...),
    on_date: str = Query(..., description="YYYY-MM-DD"),
    hour: int = Query(..., ge=0, le=23, description="Час МСК"),
    admin: dict = Depends(get_current_admin),
    db: Session = Depends(get_db)
):
    """
    Симулятор эфира: что реально будет играть на экране в указанный час,
    с учётом всех приоритетов (переопределение даты → недельные слоты
    экран/группа/сеть → заглушки) и всех фильтров (модерация, тип площадки,
    срок действия ролика). «Эфир сети» — ручной рубильник текущего момента,
    для будущих дат он не предсказуем; если он включён СЕЙЧАС, отмечаем.
    """
    try:
        sim_date = date.fromisoformat(on_date)
    except ValueError:
        raise HTTPException(400, "on_date: ожидается YYYY-MM-DD")

    scr = db.execute(text("""
        SELECT s.id, s.name, s.group_id, COALESCE(s.venue_type,'other') AS venue_type
        FROM screens s WHERE s.id = :sid
    """), {"sid": screen_id}).fetchone()
    if not scr:
        raise HTTPException(404, "Экран не найден")
    venue = scr.venue_type
    dow = sim_date.weekday()
    notes = []

    MEDIA_FILTER = """
        m.status = 'ready' AND m.review_status = 'approved'
        AND (m.category IS NULL OR m.category <> 'alcohol' OR :venue = 'store_alcohol')
        AND (m.valid_from  IS NULL OR m.valid_from  <= CAST(:simd AS date) + INTERVAL '23 hours')
        AND (m.valid_until IS NULL OR m.valid_until >= CAST(:simd AS date))
    """

    def playlist_items(pid):
        rows = db.execute(text(f"""
            SELECT m.filename, COALESCE(m.title, m.filename) AS title
            FROM playlist_items pi JOIN media m ON m.id = pi.media_id
            WHERE pi.playlist_id = :p AND {MEDIA_FILTER}
            ORDER BY pi.position
        """), {"p": pid, "venue": venue, "simd": sim_date}).fetchall()
        return [{"filename": r.filename, "title": r.title} for r in rows]

    # эфир сети (только пометка — он живёт «сейчас», а не по датам)
    bc = db.execute(text("""
        SELECT b.is_on, p.name FROM network_broadcast b
        LEFT JOIN playlists p ON p.id = b.playlist_id WHERE b.id = 1
    """)).fetchone()
    if bc and bc.is_on:
        notes.append(f"Сейчас включён «Эфир сети» ({bc.name or 'плейлист'}) — пока он включён, "
                     "он перекрывает всё расписание на всех экранах")

    # 1) переопределение даты (экран приоритетнее группы)
    ov = db.execute(text("""
        SELECT so.playlist_id, p.name, so.is_off
        FROM schedule_overrides so LEFT JOIN playlists p ON p.id = so.playlist_id
        WHERE so.on_date = :d
          AND (so.screen_id = :sid OR so.group_id = :gid)
        ORDER BY so.screen_id NULLS LAST LIMIT 1
    """), {"d": sim_date, "sid": screen_id, "gid": scr.group_id}).fetchone()
    if ov:
        if ov.is_off:
            return {"screen": scr.name, "date": on_date, "hour": hour,
                    "source": "override_off",
                    "source_label": "Переопределение даты: показ выключен",
                    "playlist": None, "items": [], "notes": notes}
        return {"screen": scr.name, "date": on_date, "hour": hour,
                "source": "override", "source_label": "Переопределение даты",
                "playlist": ov.name, "items": playlist_items(ov.playlist_id),
                "notes": notes}

    # 2) недельные слоты: экран > группа > сеть; почасовой > «весь день»
    slot = db.execute(text("""
        SELECT ss.playlist_id, p.name, ss.hour,
               CASE WHEN ss.screen_id IS NOT NULL THEN 3
                    WHEN ss.group_id IS NOT NULL THEN 2 ELSE 1 END AS prio
        FROM schedule_slots ss JOIN playlists p ON p.id = ss.playlist_id
        WHERE ss.day_of_week = :dow
          AND (ss.hour = :h OR ss.hour IS NULL)
          AND (ss.screen_id = :sid
               OR (ss.screen_id IS NULL AND ss.group_id IS NOT NULL AND ss.group_id = :gid)
               OR (ss.screen_id IS NULL AND ss.group_id IS NULL))
        ORDER BY (ss.hour IS NOT NULL) DESC, prio DESC
        LIMIT 1
    """), {"dow": dow, "h": hour, "sid": screen_id, "gid": scr.group_id}).fetchone()
    if slot:
        level = {3: "слот экрана", 2: "слот группы", 1: "слот всей сети"}[slot.prio]
        kind = "почасовой" if slot.hour is not None else "на весь день"
        items = playlist_items(slot.playlist_id)
        if not items:
            notes.append("Плейлист слота пуст после фильтров (модерация/площадка/срок) — "
                         "экран уйдёт на заглушки")
        else:
            return {"screen": scr.name, "date": on_date, "hour": hour,
                    "source": "slot", "source_label": f"Недельный шаблон ({level}, {kind})",
                    "playlist": slot.name, "items": items, "notes": notes}

    # 3) заглушки (фолбэк)
    fillers = db.execute(text(f"""
        SELECT m.filename, COALESCE(m.title, m.filename) AS title FROM media m
        WHERE m.is_filler = TRUE AND {MEDIA_FILTER} ORDER BY m.id
    """), {"venue": venue, "simd": sim_date}).fetchall()
    if fillers:
        return {"screen": scr.name, "date": on_date, "hour": hour,
                "source": "fallback", "source_label": "Дыра в расписании — играют заглушки",
                "playlist": None,
                "items": [{"filename": r.filename, "title": r.title} for r in fillers],
                "notes": notes}

    notes.append("Нет ни расписания, ни заглушек — экран будет ЧЁРНЫМ. "
                 "Загрузите заглушки (пометка «заглушка» в медиатеке) или задайте слот.")
    return {"screen": scr.name, "date": on_date, "hour": hour,
            "source": "black", "source_label": "Пустой эфир",
            "playlist": None, "items": [], "notes": notes}


@router.get("/schedule/{screen_id}")
def get_schedule(screen_id: int, admin: dict = Depends(get_current_admin),
                 db: Session = Depends(get_db)):
    rows = db.execute(text("""
        SELECT ss.day_of_week, ss.hour, p.id as playlist_id, p.name as playlist_name
        FROM schedule_slots ss JOIN playlists p ON p.id = ss.playlist_id
        WHERE ss.screen_id = :sid ORDER BY ss.day_of_week, ss.hour
    """), {"sid": screen_id}).fetchall()
    return [dict(r._mapping) for r in rows]
