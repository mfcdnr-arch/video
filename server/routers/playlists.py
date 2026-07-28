"""Плейлисты и их содержимое."""
from fastapi import APIRouter, Body, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.orm import Session

from deps import get_db, get_current_admin, require_write

router = APIRouter()


@router.post("/playlists")
def create_playlist(name: str = Query(...), admin: dict = Depends(require_write),
                    db: Session = Depends(get_db)):
    result = db.execute(text("INSERT INTO playlists (name) VALUES (:n) RETURNING id"), {"n": name})
    pid = result.fetchone()[0]
    db.commit()
    return {"id": pid, "name": name}


@router.get("/playlists")
def list_playlists(admin: dict = Depends(get_current_admin), db: Session = Depends(get_db)):
    rows = db.execute(text("SELECT * FROM playlists ORDER BY name")).fetchall()
    return [dict(r._mapping) for r in rows]


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
    # Файлы из папки «Документы» (сканы договоров и деклараций) хранятся со
    # status='document' и модерацию не проходят — в эфирный плейлист их нельзя.
    if m.status == "document":
        raise HTTPException(409, f"«{m.title}» — документ, а не ролик: в плейлист добавить нельзя")
    if m.review_status != "approved":
        status_ru = {"pending": "ещё на модерации", "rejected": "отклонён модератором"}.get(
            m.review_status, m.review_status)
        raise HTTPException(409, f"Ролик «{m.title}» {status_ru} — в плейлист можно добавлять только одобренные ролики")

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
