"""Показы в БД: чтение в формат placements_logic и сохранение.

Здесь только SQL. Решения «что пересекается» и «что играет» принимает
placements_logic (чистая логика); этот модуль отдаёт ей простые dict:

  показ           id, playlist_id, playlist_name, date_from, date_to, hour_from,
                  hour_to, stopped_at, saved_at, screens (set[int]) + all_screens,
                  created_by, created_at
  переопределение id, screen_id, on_date, playlist_id, playlist_name, is_off
  решения         {(placement_id, screen_id, other_kind, other_id): mode}

Все отметки времени — наивное МСК. Пишем их из Python (now_msk), а не NOW()
Postgres: контейнер БД живёт в UTC, и приоритет «последний сохранённый»
поехал бы на три часа относительно stopped_at и часов эфира.

Функции не делают commit — транзакцией управляет роутер.
"""
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import text

# Как в routers/screens.py: вся сеть живёт по Москве, сезонного перевода нет.
MSK = timezone(timedelta(hours=3))


def now_msk() -> datetime:
    """Текущий момент, наивное МСК (как хранится в TIMESTAMP-колонках показов)."""
    return datetime.now(MSK).replace(tzinfo=None)


def all_screen_ids(db) -> set[int]:
    """id всех экранов сети — так разворачивается показ «на все экраны»."""
    return {r.id for r in db.execute(text("SELECT id FROM screens")).fetchall()}


def load_placements(db, d_from: date, d_to: date, playlist_id: int | None = None) -> list[dict]:
    """Показы, у которых хотя бы один день попадает в [d_from .. d_to].

    Остановленные тоже возвращаются (их отсеивает логика). Экраны развёрнуты:
    для all_screens — все текущие экраны сети. Порядок: по date_from, затем id.
    """
    where = "pl.date_from <= :dt AND pl.date_to >= :df"
    params: dict = {"df": d_from, "dt": d_to}
    if playlist_id is not None:
        where += " AND pl.playlist_id = :pid"
        params["pid"] = playlist_id
    rows = db.execute(text(f"""
        SELECT pl.id, pl.playlist_id, p.name AS playlist_name, pl.all_screens,
               pl.date_from, pl.date_to, pl.hour_from, pl.hour_to,
               pl.stopped_at, pl.saved_at, pl.created_by, pl.created_at
        FROM placements pl JOIN playlists p ON p.id = pl.playlist_id
        WHERE {where}
        ORDER BY pl.date_from, pl.id
    """), params).fetchall()
    if not rows:
        return []

    ids = [r.id for r in rows]
    screens_by_id: dict[int, set[int]] = {i: set() for i in ids}
    for r in db.execute(text("""
        SELECT placement_id, screen_id FROM placement_screens
        WHERE placement_id = ANY(:ids)
    """), {"ids": ids}).fetchall():
        screens_by_id[r.placement_id].add(r.screen_id)

    every = all_screen_ids(db) if any(r.all_screens for r in rows) else set()
    out = []
    for r in rows:
        out.append({
            "id": r.id, "playlist_id": r.playlist_id, "playlist_name": r.playlist_name,
            "date_from": r.date_from, "date_to": r.date_to,
            "hour_from": r.hour_from, "hour_to": r.hour_to,
            "stopped_at": r.stopped_at, "saved_at": r.saved_at,
            "screens": set(every) if r.all_screens else screens_by_id[r.id],
            "all_screens": bool(r.all_screens),
            "created_by": r.created_by, "created_at": r.created_at,
        })
    return out


def load_overrides(db, d_from: date, d_to: date) -> list[dict]:
    """Датовые переопределения в [d_from .. d_to], развёрнутые по экранам.

    Групповое переопределение раскладывается на все экраны группы. Если у
    экрана на ту же дату есть своё переопределение, групповое для него не
    действует (так же выбирает лента экрана: экран приоритетнее группы).
    Порядок: по screen_id, затем по дате.
    """
    rows = db.execute(text("""
        SELECT so.id, so.screen_id, so.group_id, so.on_date, so.playlist_id,
               p.name AS playlist_name, COALESCE(so.is_off, FALSE) AS is_off
        FROM schedule_overrides so
        LEFT JOIN playlists p ON p.id = so.playlist_id
        WHERE so.on_date BETWEEN :df AND :dt
        ORDER BY so.id
    """), {"df": d_from, "dt": d_to}).fetchall()
    if not rows:
        return []

    members: dict[int, list[int]] = {}
    if any(r.screen_id is None for r in rows):
        for s in db.execute(text("SELECT id, group_id FROM screens WHERE group_id IS NOT NULL")).fetchall():
            members.setdefault(s.group_id, []).append(s.id)

    def as_dict(r, screen_id):
        return {"id": r.id, "screen_id": screen_id, "on_date": r.on_date,
                "playlist_id": r.playlist_id, "playlist_name": r.playlist_name,
                "is_off": bool(r.is_off)}

    by_key: dict[tuple[int, date], dict] = {}
    for r in rows:                               # сначала групповые…
        if r.screen_id is None:
            for sid in members.get(r.group_id, []):
                by_key[(sid, r.on_date)] = as_dict(r, sid)
    for r in rows:                               # …потом экранные их перекрывают
        if r.screen_id is not None:
            by_key[(r.screen_id, r.on_date)] = as_dict(r, r.screen_id)
    return [by_key[k] for k in sorted(by_key)]


def load_decisions(db, placement_ids: list[int]) -> dict:
    """Решения по пересечениям для показов placement_ids."""
    if not placement_ids:
        return {}
    rows = db.execute(text("""
        SELECT placement_id, screen_id, other_kind, other_id, mode
        FROM placement_decisions WHERE placement_id = ANY(:ids)
    """), {"ids": list(placement_ids)}).fetchall()
    return {(r.placement_id, r.screen_id, r.other_kind, r.other_id): r.mode for r in rows}


def get_placement(db, placement_id: int) -> dict | None:
    """Один показ без экранов (id, плейлист, даты, часы, stopped_at) или None.

    Хватает для проверки статуса (placements_logic.placement_status) и для
    записи в журнал при остановке и отмене.
    """
    rows = db.execute(text("""
        SELECT pl.id, pl.playlist_id, p.name AS playlist_name, pl.date_from, pl.date_to,
               pl.hour_from, pl.hour_to, pl.stopped_at
        FROM placements pl JOIN playlists p ON p.id = pl.playlist_id
        WHERE pl.id = :id
    """), {"id": placement_id}).fetchall()
    if not rows:
        return None
    r = rows[0]
    return {"id": r.id, "playlist_id": r.playlist_id, "playlist_name": r.playlist_name,
            "date_from": r.date_from, "date_to": r.date_to,
            "hour_from": r.hour_from, "hour_to": r.hour_to, "stopped_at": r.stopped_at}


def load_screen_slots(db, screen_id: int) -> list[dict]:
    """Слоты недельного шаблона, которые действуют на экране: его собственные,
    слоты его группы и общесетевые, с уровнем prio (экран 3 > группа 2 > сеть 1).

    Тот же отбор, что у ленты экрана (routers/screens.py) и симулятора
    (routers/schedule.py); какой из слотов выбрать на (день, час) решает
    placements_logic.template_playlist.
    """
    rows = db.execute(text("""
        SELECT ss.day_of_week, ss.hour, ss.playlist_id,
               CASE WHEN ss.screen_id IS NOT NULL THEN 3
                    WHEN ss.group_id IS NOT NULL THEN 2 ELSE 1 END AS prio
        FROM schedule_slots ss
        WHERE ss.screen_id = :sid
           OR (ss.screen_id IS NULL AND ss.group_id IS NOT NULL
               AND ss.group_id = (SELECT group_id FROM screens WHERE id = :sid))
           OR (ss.screen_id IS NULL AND ss.group_id IS NULL)
    """), {"sid": screen_id}).fetchall()
    return [{"day_of_week": r.day_of_week, "hour": r.hour, "playlist_id": r.playlist_id, "prio": r.prio}
            for r in rows]


def load_playlist_names(db, ids) -> dict[int, str]:
    """{playlist_id: название} для указанных плейлистов."""
    ids = sorted(set(ids))
    if not ids:
        return {}
    rows = db.execute(text("SELECT id, name FROM playlists WHERE id = ANY(:ids)"), {"ids": ids}).fetchall()
    return {r.id: r.name for r in rows}


def _hh(hour: int) -> str:
    return f"{hour:02d}:00"


def _period(date_from: date, date_to: date, hour_from: int, hour_to: int) -> str:
    return f"{date_from:%d.%m.%Y}–{date_to:%d.%m.%Y}, {_hh(hour_from)}–{_hh(hour_to)}"


def _audit(db, title: str, detail: str, actor: str) -> None:
    db.execute(text("""
        INSERT INTO audit_log (event_type, title, detail, actor)
        VALUES ('schedule', :t, :d, :a)
    """), {"t": title, "d": detail, "a": actor})


def _describe(p: dict) -> str:
    return (f"#{p['id']} «{p['playlist_name']}», "
            f"{_period(p['date_from'], p['date_to'], p['hour_from'], p['hour_to'])}")


def stop_placement(db, p: dict, actor: str, now: datetime | None = None) -> bool:
    """Остановить показ `p` (из get_placement) с момента `now` (наивное МСК).

    Пишем момент из Python, не NOW() Postgres (см. шапку модуля). Условие
    «stopped_at IS NULL» не даёт второй остановке (двойной клик, второй
    админ) сдвинуть уже записанный момент: тогда False и ничего не пишем.
    Без commit.
    """
    now = now or now_msk()
    updated = db.execute(text("""
        UPDATE placements SET stopped_at = :now WHERE id = :id AND stopped_at IS NULL
    """), {"now": now, "id": p["id"]})
    if not updated.rowcount:
        return False
    _audit(db, "Показ: остановлен", f"{_describe(p)}, остановлен {now:%d.%m.%Y %H:%M}", actor)
    return True


def delete_placement(db, p: dict, actor: str) -> None:
    """Удалить (отменить) показ `p` из get_placement; экраны и решения уходят каскадом. Без commit."""
    db.execute(text("DELETE FROM placements WHERE id = :id"), {"id": p["id"]})
    _audit(db, "Показ: отменён", _describe(p), actor)


def save_placement(db, data: dict, decisions: list[dict], actor: str,
                   placement_id: int | None = None) -> int:
    """Создать (placement_id=None) или изменить показ; вернуть его id.

    data — как из routers.placements.parse_body (+ развёрнутые screens);
    decisions — [{"screen_id", "other_kind", "other_id", "mode"}], уже
    проверенные роутером. Экраны и решения показа перезаписываются целиком.
    saved_at ставится на «сейчас» и при создании, и при каждой правке —
    сохранённый показ становится новейшим. Без commit.
    """
    now = now_msk()
    fields = {"pl": data["playlist_id"], "all": bool(data["all_screens"]),
              "df": data["date_from"], "dt": data["date_to"],
              "hf": data["hour_from"], "ht": data["hour_to"], "now": now}
    if placement_id is None:
        placement_id = db.execute(text("""
            INSERT INTO placements (playlist_id, all_screens, date_from, date_to,
                                    hour_from, hour_to, saved_at, created_by, created_at)
            VALUES (:pl, :all, :df, :dt, :hf, :ht, :now, :by, :now)
            RETURNING id
        """), {**fields, "by": actor}).scalar()
        title = "Показ: создан"
    else:
        db.execute(text("""
            UPDATE placements
            SET playlist_id = :pl, all_screens = :all, date_from = :df, date_to = :dt,
                hour_from = :hf, hour_to = :ht, saved_at = :now
            WHERE id = :id
        """), {**fields, "id": placement_id})
        title = "Показ: изменён"

    db.execute(text("DELETE FROM placement_screens WHERE placement_id = :id"), {"id": placement_id})
    if not data["all_screens"]:
        for sid in sorted(data["screens"]):
            db.execute(text("INSERT INTO placement_screens (placement_id, screen_id) VALUES (:id, :sid)"),
                       {"id": placement_id, "sid": sid})

    db.execute(text("DELETE FROM placement_decisions WHERE placement_id = :id"), {"id": placement_id})
    for d in decisions:
        db.execute(text("""
            INSERT INTO placement_decisions (placement_id, screen_id, other_kind, other_id, mode)
            VALUES (:id, :sid, :kind, :oid, :mode)
        """), {"id": placement_id, "sid": d["screen_id"], "kind": d["other_kind"],
               "oid": d["other_id"], "mode": d["mode"]})

    name = db.execute(text("SELECT name FROM playlists WHERE id = :p"),
                      {"p": data["playlist_id"]}).scalar()
    where = ("на всех экранах сети" if data["all_screens"]
             else f"на {len(data['screens'])} экран(ах)")
    detail = (f"#{placement_id} «{name}» {where}, "
              f"{_period(data['date_from'], data['date_to'], data['hour_from'], data['hour_to'])}")
    if decisions:
        detail += f", решений по пересечениям: {len(decisions)}"
    _audit(db, title, detail, actor)
    return placement_id
