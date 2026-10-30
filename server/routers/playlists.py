"""Плейлисты и их содержимое.

Окно сборки плейлиста работает тремя запросами с одним форматом списка
`items: [{"media_id", "repeat_count"}]` (порядок = порядок в эфире):
POST /playlists/check — живые мягкие предупреждения (playlist_rules.repeat_warnings)
и итоги, ничего не пишет; POST /playlists/full — создать плейлист с названием и
роликами одним запросом; PUT /playlists/{id}/items — переименовать и заменить
ролики целиком. Создание и замена идут в одной транзакции: любая ошибка проверки
(400/404/409) — в БД не записано ничего. Старые POST /playlists?name= и
POST /playlists/{id}/items остаются (smoke, E2E, внешние скрипты).

Разбор списка (normalize_items) и проверка ролика перед эфиром (_check_media)
— чистые функции, вместе с эндпоинтами проверяются в tests/test_playlists_api.py.
"""
import re

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.orm import Session

from deps import get_db, get_current_admin, require_write
from playable_store import LICENSED_VENUE, UNLICENSED_VENUE, load_playable_by_venue
from playlist_rules import repeat_warnings

router = APIRouter()

MAX_REPEAT = 99          # повторов одного ролика в плейлисте
MAX_NAME = 128           # playlists.name VARCHAR(128)
_MAX_INT4 = 2 ** 31 - 1  # media.id — SERIAL
_INT = re.compile(r"-?[0-9]{1,12}")


# --- чистые функции ----------------------------------------------------------

def _int(value) -> int | None:
    """Целое из JSON (int или строка ASCII-цифр); bool, дроби, «²», «1_0» — не целые."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and _INT.fullmatch(value.strip()):
        return int(value.strip())
    return None


def normalize_items(raw) -> list[dict]:
    """
    Список роликов из тела запроса → [{"media_id": int, "repeat_count": int}] в том
    же порядке. repeat_count не задан (или null) — 1; лишние ключи отбрасываются.

    400: не список / пустой список / элемент не объект / media_id не целое
    положительное / repeat_count не целое 1…99 / один media_id дважды (повтор
    ролика задаётся числом ×N, а не второй записью — на это стоит unique-индекс).
    """
    if not isinstance(raw, list):
        raise HTTPException(400, "items: ожидается список роликов")
    if not raw:
        raise HTTPException(400, "В плейлисте должен быть хотя бы один ролик")
    out, seen = [], set()
    for n, item in enumerate(raw, 1):
        if not isinstance(item, dict):
            raise HTTPException(400, f"Ролик №{n}: ожидается объект с media_id и repeat_count")
        mid = _int(item.get("media_id"))
        if mid is None or not (1 <= mid <= _MAX_INT4):
            raise HTTPException(400, f"Ролик №{n}: неверный media_id")
        rc_raw = item.get("repeat_count")
        rc = 1 if rc_raw is None else _int(rc_raw)
        if rc is None or not (1 <= rc <= MAX_REPEAT):
            raise HTTPException(400, f"Ролик №{n}: повторов должно быть целое число от 1 до {MAX_REPEAT}")
        if mid in seen:
            raise HTTPException(400, f"Ролик id={mid} указан в списке дважды — вместо второй записи поставьте повтор ×N")
        seen.add(mid)
        out.append({"media_id": mid, "repeat_count": rc})
    return out


def _check_media(m) -> None:
    """
    38-ФЗ: в эфирный плейлист — только одобренные ролики. m — строка media с
    title, status, review_status. 409 с понятным текстом, если нельзя.
    """
    # Файлы из папки «Документы» (сканы договоров и деклараций) хранятся со
    # status='document' и модерацию не проходят — в эфирный плейлист их нельзя.
    if m.status == "document":
        raise HTTPException(409, f"«{m.title}» — документ, а не ролик: в плейлист добавить нельзя")
    if m.review_status != "approved":
        status_ru = {"pending": "ещё на модерации", "rejected": "отклонён модератором"}.get(
            m.review_status, m.review_status)
        raise HTTPException(409, f"Ролик «{m.title}» {status_ru} — в плейлист можно добавлять только одобренные ролики")


def _body(body) -> dict:
    if not isinstance(body, dict):
        raise HTTPException(400, "Ожидается JSON-объект")
    return body


def _clean_name(value, required: bool) -> str | None:
    """Название без пробелов по краям; не задано (null) допустимо, только если required=False."""
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value.strip():
        raise HTTPException(400, "Название плейлиста не может быть пустым")
    name = value.strip()
    if len(name) > MAX_NAME:
        raise HTTPException(400, f"Название плейлиста длиннее {MAX_NAME} символов")
    return name


def _warning_items(items: list[dict], clips: list) -> list[dict]:
    """Вход playlist_rules.repeat_warnings: ролики плейлиста в порядке эфира."""
    return [{"media_id": it["media_id"], "title": c.title or c.filename,
             "duration": float(c.duration_seconds or 0), "repeat_count": it["repeat_count"],
             "md5": c.md5_hash}
            for it, c in zip(items, clips)]


# --- работа с БД ---------------------------------------------------------------

def _load_clips(db: Session, items: list[dict]) -> list:
    """Строки media для items в том же порядке; 404, если какого-то ролика нет."""
    ids = [it["media_id"] for it in items]
    rows = db.execute(text(
        "SELECT id, title, filename, status, review_status, duration_seconds, md5_hash "
        "FROM media WHERE id = ANY(:ids)"), {"ids": ids}).fetchall()
    by_id = {r.id: r for r in rows}
    missing = [str(i) for i in ids if i not in by_id]
    if missing:
        raise HTTPException(404, "Ролик не найден: id " + ", ".join(missing))
    return [by_id[i] for i in ids]


def _insert_items(db: Session, playlist_id: int, items: list[dict]) -> None:
    for pos, it in enumerate(items, 1):
        db.execute(text(
            "INSERT INTO playlist_items (playlist_id, media_id, position, repeat_count) "
            "VALUES (:pid, :mid, :pos, :rc)"
        ), {"pid": playlist_id, "mid": it["media_id"], "pos": pos, "rc": it["repeat_count"]})


@router.post("/playlists")
def create_playlist(name: str = Query(...), admin: dict = Depends(require_write),
                    db: Session = Depends(get_db)):
    result = db.execute(text("INSERT INTO playlists (name) VALUES (:n) RETURNING id"), {"n": name})
    pid = result.fetchone()[0]
    db.commit()
    return {"id": pid, "name": name}


@router.get("/playlists")
def list_playlists(admin: dict = Depends(get_current_admin), db: Session = Depends(get_db)):
    """Все плейлисты по названию. К полям таблицы добавлены два флага (фильтры роликов те же,
    что у ленты экрана и обзора сети — playable_store.load_playable_by_venue):
    `playable` — есть ли хоть один ролик, который может идти на экране (готов, одобрен, срок
    не вышел; реклама алкоголя считается: она идёт на площадке с алколицензией) — окно «Эфир
    сети» по нему показывает серыми плейлисты, которым нечего играть; `playable_unlicensed` —
    то же для площадки без алколицензии (плейлист только с рекламой алкоголя там играть нечего:
    окно считает такие экраны и предупреждает до включения эфира)."""
    rows = db.execute(text("SELECT * FROM playlists ORDER BY name")).fetchall()
    by_venue = load_playable_by_venue(db, {LICENSED_VENUE, UNLICENSED_VENUE})
    return [{**dict(r._mapping), "playable": r.id in by_venue[LICENSED_VENUE],
             "playable_unlicensed": r.id in by_venue[UNLICENSED_VENUE]} for r in rows]


@router.post("/playlists/check")
def check_playlist(body: dict = Body(...), admin: dict = Depends(get_current_admin),
                   db: Session = Depends(get_db)):
    """Живая проверка окна сборки: мягкие предупреждения (повторы подряд, доля,
    дубли файла) и итоги — total_seconds (с учётом повторов) и plays (показов за
    круг). Ничего не пишет и модерацию не проверяет: 409 «не одобрен» приходит
    при сохранении. Пустой список допустим — нули и []. 404 — ролика нет."""
    raw = _body(body).get("items")
    if raw == []:
        return {"warnings": [], "total_seconds": 0.0, "plays": 0}
    items = normalize_items(raw)
    wi = _warning_items(items, _load_clips(db, items))
    return {"warnings": repeat_warnings(wi),
            "total_seconds": round(sum(w["duration"] * w["repeat_count"] for w in wi), 1),
            "plays": sum(w["repeat_count"] for w in wi)}


@router.post("/playlists/full")
def create_playlist_full(body: dict = Body(...), admin: dict = Depends(require_write),
                         db: Session = Depends(get_db)):
    """Создать плейлист с названием и роликами (position 1…N, повторы ×N) одним
    запросом. 400 — пустое имя/список, дубль или мусор в items; 404 — ролика нет;
    409 — документ или не одобрен (те же тексты, что у POST /playlists/{id}/items).
    Одна транзакция: при любой ошибке плейлист не создаётся."""
    body = _body(body)
    name = _clean_name(body.get("name"), required=True)
    items = normalize_items(body.get("items"))
    clips = _load_clips(db, items)
    for c in clips:
        _check_media(c)
    try:
        pid = db.execute(text("INSERT INTO playlists (name) VALUES (:n) RETURNING id"),
                         {"n": name}).fetchone()[0]
        _insert_items(db, pid, items)
        db.commit()
    except Exception:
        db.rollback()
        raise
    return {"id": pid, "name": name, "warnings": repeat_warnings(_warning_items(items, clips))}


@router.put("/playlists/{playlist_id}/items")
def replace_playlist_items(playlist_id: int, body: dict = Body(...),
                           admin: dict = Depends(require_write), db: Session = Depends(get_db)):
    """Заменить ролики плейлиста целиком (и переименовать, если передан name):
    старые playlist_items удаляются, новые вставляются с position 1…N. Те же
    проверки, что у POST /playlists/full, плюс 404, если плейлиста нет. Одна
    транзакция: при ошибке остаются прежние название и ролики."""
    body = _body(body)
    name = _clean_name(body.get("name"), required=False)
    items = normalize_items(body.get("items"))
    # строка плейлиста под замком: две одновременные замены не перемешаются
    if not db.execute(text("SELECT id FROM playlists WHERE id = :id FOR UPDATE"),
                      {"id": playlist_id}).fetchone():
        raise HTTPException(404, "Плейлист не найден")
    clips = _load_clips(db, items)
    for c in clips:
        _check_media(c)
    try:
        if name is not None:
            db.execute(text("UPDATE playlists SET name = :n WHERE id = :id"),
                       {"n": name, "id": playlist_id})
        db.execute(text("DELETE FROM playlist_items WHERE playlist_id = :pid"), {"pid": playlist_id})
        _insert_items(db, playlist_id, items)
        db.commit()
    except Exception:
        db.rollback()
        raise
    return {"status": "ok", "warnings": repeat_warnings(_warning_items(items, clips))}


@router.post("/playlists/{playlist_id}/items")
def add_to_playlist(
    playlist_id: int,
    media_id: int = Query(...),
    repeat_count: int = Query(1),
    admin: dict = Depends(require_write),
    db: Session = Depends(get_db)
):
    # 38-ФЗ: неодобренный или отклонённый контент не попадает в эфирные плейлисты
    m = db.execute(text("SELECT title, review_status, status FROM media WHERE id = :id"),
                   {"id": media_id}).fetchone()
    if not m:
        raise HTTPException(404, "Ролик не найден")
    _check_media(m)

    # #1: один и тот же ролик нельзя добавить в плейлист дважды
    dup = db.execute(text(
        "SELECT 1 FROM playlist_items WHERE playlist_id = :pid AND media_id = :mid"
    ), {"pid": playlist_id, "mid": media_id}).fetchone()
    if dup:
        raise HTTPException(409, f"Ролик «{m.title}» уже есть в этом плейлисте")

    pos = db.execute(text(
        "SELECT COALESCE(MAX(position), 0) + 1 FROM playlist_items WHERE playlist_id = :pid"
    ), {"pid": playlist_id}).scalar()
    result = db.execute(text(
        "INSERT INTO playlist_items (playlist_id, media_id, position, repeat_count) "
        "VALUES (:pid, :mid, :pos, :rc) RETURNING id"
    ), {"pid": playlist_id, "mid": media_id, "pos": pos, "rc": repeat_count})
    db.commit()

    # #2: предупреждение, если ролик уже используется в других плейлистах
    others = db.execute(text(
        "SELECT p.name FROM playlist_items pi JOIN playlists p ON p.id = pi.playlist_id "
        "WHERE pi.media_id = :mid AND pi.playlist_id <> :pid ORDER BY p.name"
    ), {"mid": media_id, "pid": playlist_id}).fetchall()
    warning = None
    if others:
        names = ", ".join(f"«{o.name}»" for o in others)
        warning = f"Этот ролик уже используется в плейлисте(ах): {names}"

    return {"id": result.fetchone()[0], "playlist_id": playlist_id,
            "media_id": media_id, "position": pos, "warning": warning}


@router.get("/playlists/{playlist_id}/items")
def get_playlist_items(playlist_id: int, admin: dict = Depends(get_current_admin),
                       db: Session = Depends(get_db)):
    rows = db.execute(text("""
        SELECT pi.id, pi.position, pi.repeat_count,
               m.id as media_id, m.title, m.filename, m.filesize, m.duration_seconds
        FROM playlist_items pi JOIN media m ON m.id = pi.media_id
        WHERE pi.playlist_id = :pid ORDER BY pi.position
    """), {"pid": playlist_id}).fetchall()
    return [dict(r._mapping) for r in rows]


@router.patch("/playlists/{playlist_id}/fill")
def set_playlist_fill(playlist_id: int, body: dict = Body(...),
                      admin: dict = Depends(require_write), db: Session = Depends(get_db)):
    """Режим длительности плейлиста: fill_to_hour=true — эфирный блок
    фиксированной длины (target_minutes, по умолчанию 60), добивается
    заглушками; false — произвольный плейлист, играет как есть."""
    if not db.execute(text("SELECT 1 FROM playlists WHERE id=:id"), {"id": playlist_id}).fetchone():
        raise HTTPException(404, "Плейлист не найден")
    fill = bool(body.get("fill_to_hour", True))
    minutes = body.get("target_minutes", 60)
    try:
        minutes = int(minutes)
    except (TypeError, ValueError):
        raise HTTPException(400, "target_minutes: целое число минут")
    if not (1 <= minutes <= 24 * 60):
        raise HTTPException(400, "target_minutes: от 1 до 1440 минут")
    db.execute(text(
        "UPDATE playlists SET fill_to_hour=:f, target_seconds=:t WHERE id=:id"
    ), {"f": fill, "t": minutes * 60, "id": playlist_id})
    db.commit()
    return {"status": "ok", "fill_to_hour": fill, "target_minutes": minutes}


@router.get("/playlists/{playlist_id}/fill_info")
def playlist_fill_info(playlist_id: int, admin: dict = Depends(get_current_admin),
                       db: Session = Depends(get_db)):
    """Расчёт заполнения эфирного блока: сколько идут основные ролики, сколько
    заглушек добавится, и предупреждения (нет заглушек / не хватает / перебор)."""
    pl = db.execute(text(
        "SELECT COALESCE(fill_to_hour, TRUE) AS fill_to_hour, "
        "COALESCE(target_seconds, 3600) AS target_seconds "
        "FROM playlists WHERE id=:id"), {"id": playlist_id}).fetchone()
    if not pl:
        raise HTTPException(404, "Плейлист не найден")

    from admin_panel import build_hourly_detail
    d = build_hourly_detail(db, playlist_id, int(pl.target_seconds))

    warnings = []
    # Для пустого плейлиста агент не играет ничего (в т.ч. заглушки) —
    # предупреждать о нехватке заглушек бессмысленно
    if pl.fill_to_hour and d["main_plays"] > 0:
        target = float(pl.target_seconds)
        remaining = target - d["main_seconds"]
        if d["main_seconds"] > target:
            warnings.append(
                f"Основные ролики идут {d['main_seconds']/60:.1f} мин — это БОЛЬШЕ целевых "
                f"{target/60:.0f} мин. Блок будет длиннее цели, заглушки не добавляются. "
                "Уберите ролики или увеличьте целевую длительность.")
        elif remaining > 0 and d["fillers_available"] == 0:
            warnings.append(
                f"До {target/60:.0f} мин не хватает {remaining/60:.1f} мин, а заглушек в "
                "медиатеке нет — эфирный блок будет короче цели. Загрузите ролики в "
                "Медиатека → Заглушки.")
        elif remaining > 0 and d["fillers_available_seconds"] < remaining:
            warnings.append(
                f"Свободные {remaining/60:.1f} мин заполняются заглушками, но их суммарная "
                f"длительность всего {d['fillers_available_seconds']/60:.1f} мин — заглушки "
                "будут повторяться по кругу. Загрузите больше заглушек для разнообразия.")

    return {
        "fill_to_hour": bool(pl.fill_to_hour),
        "target_seconds": int(pl.target_seconds),
        "main_seconds": round(d["main_seconds"], 1),
        "main_plays": d["main_plays"],
        "filler_seconds": round(d["filler_seconds"], 1),
        "filler_plays": d["filler_plays"],
        "total_seconds": round(d["main_seconds"] + d["filler_seconds"], 1),
        "fillers_available": d["fillers_available"],
        "fillers_available_seconds": round(d["fillers_available_seconds"], 1),
        "warnings": warnings,
    }


@router.delete("/playlists/{playlist_id}")
def delete_playlist(playlist_id: int, admin: dict = Depends(require_write),
                    db: Session = Depends(get_db)):
    db.execute(text("DELETE FROM playlists WHERE id=:id"), {"id": playlist_id})
    db.commit()
    return {"status": "deleted"}


@router.delete("/playlists/{playlist_id}/items/{item_id}")
def delete_playlist_item(playlist_id: int, item_id: int,
                         admin: dict = Depends(require_write), db: Session = Depends(get_db)):
    db.execute(text("DELETE FROM playlist_items WHERE id=:id AND playlist_id=:pid"),
               {"id": item_id, "pid": playlist_id})
    db.commit()
    return {"status": "deleted"}
