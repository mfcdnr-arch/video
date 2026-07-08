"""Общий эфир сети — один плейлист на все экраны, перекрывает индивидуальные расписания."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from deps import get_db, get_current_admin, require_write

router = APIRouter()


@router.get("/broadcast")
def get_broadcast(db: Session = Depends(get_db), current_admin=Depends(get_current_admin)):
    row = db.execute(text("""
        SELECT b.is_on, b.playlist_id, p.name AS playlist_name
        FROM network_broadcast b
        LEFT JOIN playlists p ON p.id = b.playlist_id
        WHERE b.id = 1
    """)).fetchone()
    if not row:
        return {"is_on": False, "playlist_id": None, "playlist_name": None}
    return dict(row._mapping)


@router.post("/broadcast/on")
def broadcast_on(playlist_id: int, db: Session = Depends(get_db),
                  current_admin=Depends(require_write)):
    playlist = db.execute(text("SELECT name FROM playlists WHERE id = :pid"),
                           {"pid": playlist_id}).fetchone()
    if not playlist:
        raise HTTPException(status_code=404, detail="Плейлист не найден")

    db.execute(text("""
        UPDATE network_broadcast
        SET is_on = TRUE, playlist_id = :pid, enabled_by = :who, enabled_at = NOW()
        WHERE id = 1
    """), {"pid": playlist_id, "who": current_admin["username"]})
    db.execute(text("""
        INSERT INTO audit_log (event_type, title, detail, actor)
        VALUES ('broadcast', 'Общий эфир включён', :detail, :who)
    """), {"detail": f"Плейлист: {playlist.name}", "who": current_admin["username"]})
    db.commit()
    return {"status": "ok", "is_on": True, "playlist_id": playlist_id, "playlist_name": playlist.name}


@router.post("/broadcast/off")
def broadcast_off(db: Session = Depends(get_db), current_admin=Depends(require_write)):
    db.execute(text("""
        UPDATE network_broadcast
        SET is_on = FALSE, playlist_id = NULL, enabled_by = :who, enabled_at = NOW()
        WHERE id = 1
    """), {"who": current_admin["username"]})
    db.execute(text("""
        INSERT INTO audit_log (event_type, title, actor)
        VALUES ('broadcast', 'Общий эфир выключен — возврат к индивидуальным расписаниям', :who)
    """), {"who": current_admin["username"]})
    db.commit()
    return {"status": "ok", "is_on": False}
