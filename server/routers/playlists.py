"""Плейлисты и их содержимое."""
from fastapi import APIRouter, Depends, HTTPException, Query
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
    m = db.execute(text("SELECT title, review_status FROM media WHERE id = :id"),
                   {"id": media_id}).fetchone()
    if not m:
        raise HTTPException(404, "Ролик не найден")
    if m.review_status != "approved":
        status_ru = {"pending": "ещё на модерации", "rejected": "отклонён модератором"}.get(
            m.review_status, m.review_status)
        raise HTTPException(409, f"Ролик «{m.title}» {status_ru} — в плейлист можно добавлять только одобренные ролики")

    pos = db.execute(text(
        "SELECT COALESCE(MAX(position), 0) + 1 FROM playlist_items WHERE playlist_id = :pid"
    ), {"pid": playlist_id}).scalar()
    result = db.execute(text(
        "INSERT INTO playlist_items (playlist_id, media_id, position, repeat_count) "
        "VALUES (:pid, :mid, :pos, :rc) RETURNING id"
    ), {"pid": playlist_id, "mid": media_id, "pos": pos, "rc": repeat_count})
    db.commit()
    return {"id": result.fetchone()[0], "playlist_id": playlist_id,
            "media_id": media_id, "position": pos}


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
