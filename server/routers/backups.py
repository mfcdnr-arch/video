"""Резервное копирование БД (db_dump.pg_dump_gz: pg_dump без пароля в командной строке → gzip)."""
import os
from datetime import datetime, timezone, timedelta

# Имена бэкапов — в московском времени (вся система живёт по МСК),
# иначе «backup_…_1009» в 13:09 по Москве путает при разборе инцидентов
MSK = timezone(timedelta(hours=3))

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from db_dump import pg_dump_gz
from deps import get_db, get_current_admin, require_write, DATABASE_URL

router = APIRouter()

BACKUP_DIR = os.getenv("BACKUP_DIR", "/data/backups")
os.makedirs(BACKUP_DIR, exist_ok=True)


@router.get("/backups")
def get_backups(admin: dict = Depends(get_current_admin), db: Session = Depends(get_db)):
    rows = db.execute(text(
        "SELECT id, filename, size_bytes, created_at FROM backups ORDER BY created_at DESC"
    )).fetchall()
    return [dict(r._mapping) for r in rows]


@router.post("/backups/create")
def create_backup(current_admin: dict = Depends(require_write), db: Session = Depends(get_db)):
    """Снять дамп БД и сохранить сжатым в BACKUP_DIR (как ночной: db_dump.pg_dump_gz)."""
    timestamp = datetime.now(MSK).strftime("%Y%m%d_%H%M%S")
    filename = f"backup_{timestamp}.sql.gz"
    filepath = os.path.join(BACKUP_DIR, filename)

    try:
        pg_dump_gz(DATABASE_URL, filepath)
    except RuntimeError as e:          # текст короткий и без пароля БД, недописанный файл уже удалён
        raise HTTPException(status_code=500, detail=str(e))

    size = os.path.getsize(filepath)
    row = db.execute(text("""
        INSERT INTO backups (filename, size_bytes, created_by)
        VALUES (:fn, :sz, :who) RETURNING id, filename, size_bytes, created_at
    """), {"fn": filename, "sz": size, "who": current_admin["username"]}).fetchone()
    db.execute(text("""
        INSERT INTO audit_log (event_type, title, detail, actor)
        VALUES ('backup', 'Резервная копия создана', :detail, :who)
    """), {"detail": filename, "who": current_admin["username"]})
    db.commit()
    return dict(row._mapping)


@router.get("/backups/{backup_id}/download")
def download_backup(backup_id: int, current_admin: dict = Depends(require_write),
                    db: Session = Depends(get_db)):
    """Отдать дамп. Только пишущему админу: внутри вся БД — хеши паролей,
    X-Token каждого экрана, рекламодатели, счета. Номера бэкапов идут подряд,
    так что без проверки любой в сети забрал бы дамп перебором id. Панель
    скачивает через fetch с заголовком Authorization (apiDownload в api.js)."""
    row = db.execute(text("SELECT filename FROM backups WHERE id = :id"), {"id": backup_id}).fetchone()
    if not row:
        raise HTTPException(404, "Бэкап не найден")
    filepath = os.path.join(BACKUP_DIR, row.filename)
    if not os.path.exists(filepath):
        raise HTTPException(404, "Файл бэкапа отсутствует на диске")
    return FileResponse(filepath, media_type="application/gzip", filename=row.filename)


@router.delete("/backups/{backup_id}")
def delete_backup(backup_id: int, current_admin: dict = Depends(require_write),
                   db: Session = Depends(get_db)):
    row = db.execute(text("SELECT filename FROM backups WHERE id = :id"), {"id": backup_id}).fetchone()
    if not row:
        raise HTTPException(404, "Бэкап не найден")
    filepath = os.path.join(BACKUP_DIR, row.filename)
    if os.path.exists(filepath):
        os.remove(filepath)
    db.execute(text("DELETE FROM backups WHERE id = :id"), {"id": backup_id})
    db.commit()
    return {"status": "deleted"}
