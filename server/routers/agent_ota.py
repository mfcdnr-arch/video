"""
OTA-обновление агентов на мини ПК + целевые версии ПО для сравнения.
Не путать с server/agent_updater.py — тот отдельный старый файл-заглушка
с единственным /api/ota/health, не связан с этим модулем.
"""
import hashlib
import json
import os

from fastapi import APIRouter, Body, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from deps import get_db, get_current_admin, require_write, verify_any_device_token

router = APIRouter()

AGENT_UPDATE_PATH = os.getenv("AGENT_UPDATE_PATH", "/data/agent_updates")
os.makedirs(AGENT_UPDATE_PATH, exist_ok=True)
ALLOWED_AGENT_FILES = {"ds_agent.py", "ds_player.py", "ds_sync.py", "ds_heartbeat.py",
                       "ds_downloader.py", "ds_cleanup.py", "ds_ws_client.py"}


# ─── Обновление агентов (OTA) ────────────────────────────────────────────────

@router.get("/agent/files")
def list_agent_files(admin: dict = Depends(get_current_admin)):
    result = []
    for fname in sorted(ALLOWED_AGENT_FILES):
        fpath = os.path.join(AGENT_UPDATE_PATH, fname)
        if os.path.exists(fpath):
            with open(fpath, "rb") as f:
                content = f.read()
            stat = os.stat(fpath)
            result.append({
                "name": fname,
                "size": stat.st_size,
                "md5": hashlib.md5(content).hexdigest(),
                "modified": stat.st_mtime,
            })
    return result


@router.post("/agent/files/upload")
async def upload_agent_file(file: UploadFile = File(...),
                             current_admin: dict = Depends(require_write)):
    if file.filename not in ALLOWED_AGENT_FILES:
        raise HTTPException(status_code=400, detail=f"Недопустимое имя файла: {file.filename}")

    content = await file.read()
    fpath = os.path.join(AGENT_UPDATE_PATH, file.filename)
    with open(fpath, "wb") as f:
        f.write(content)

    return {"name": file.filename, "size": len(content), "md5": hashlib.md5(content).hexdigest()}


@router.get("/agent/files/download/{filename}")
def download_agent_file(filename: str, _dev=Depends(verify_any_device_token)):
    """
    Скачивание файла агентом при OTA (agent/ds_ota_updater.py). Защищено
    проверкой токена устройства: агент шлёт заголовок X-Token (см.
    ds_ota_updater.py), сервер сверяет его с токенами зарегистрированных
    экранов (verify_any_device_token). Неизвестный/отсутствующий токен → 401.
    """
    if filename not in ALLOWED_AGENT_FILES:
        raise HTTPException(status_code=404, detail="Файл не найден")
    fpath = os.path.join(AGENT_UPDATE_PATH, filename)
    if not os.path.exists(fpath):
        raise HTTPException(status_code=404, detail="Файл отсутствует на диске")
    return FileResponse(fpath, media_type="text/x-python", filename=filename)


@router.get("/agent/updates")
def get_agent_updates(admin: dict = Depends(get_current_admin), db: Session = Depends(get_db)):
    rows = db.execute(text("""
        SELECT version, files, changelog, created_by, created_at, screens_updated, screens_total
        FROM agent_updates ORDER BY created_at DESC
    """)).fetchall()
    return [dict(r._mapping) for r in rows]


@router.post("/agent/push")
def push_agent_update(body: dict = Body(...), current_admin: dict = Depends(require_write),
                       db: Session = Depends(get_db)):
    screen_ids = body.get("screen_ids") or []
    version = (body.get("version") or "auto").strip() or "auto"
    changelog = body.get("changelog") or ""
    filenames = body.get("files") or []
    if not filenames:
        raise HTTPException(status_code=400, detail="Не выбраны файлы для обновления")

    files_payload = []
    for fname in filenames:
        if fname not in ALLOWED_AGENT_FILES:
            raise HTTPException(status_code=400, detail=f"Недопустимое имя файла: {fname}")
        fpath = os.path.join(AGENT_UPDATE_PATH, fname)
        if not os.path.exists(fpath):
            raise HTTPException(status_code=400, detail=f"Файл не загружен на сервер: {fname}")
        with open(fpath, "rb") as f:
            content = f.read()
        files_payload.append({"name": fname, "md5": hashlib.md5(content).hexdigest(), "size": len(content)})

    if screen_ids:
        targets = [int(i) for i in screen_ids]
    else:
        targets = [r.id for r in db.execute(text("SELECT id FROM screens")).fetchall()]

    payload = {"version": version, "changelog": changelog, "files": files_payload}
    for sid in targets:
        db.execute(text(
            "INSERT INTO commands (screen_id, type, payload) VALUES (:sid, 'update_agent', CAST(:p AS jsonb))"
        ), {"sid": sid, "p": json.dumps(payload)})

    db.execute(text("""
        INSERT INTO agent_updates (version, files, changelog, created_by, screens_total)
        VALUES (:v, CAST(:f AS jsonb), :cl, :who, :st)
    """), {"v": version, "f": json.dumps(files_payload), "cl": changelog,
           "who": current_admin["username"], "st": len(targets)})

    db.execute(text("""
        INSERT INTO audit_log (event_type, title, detail, actor)
        VALUES ('ota', 'OTA-обновление отправлено', :detail, :who)
    """), {"detail": f"v{version} на {len(targets)} экран(ов): {', '.join(filenames)}",
           "who": current_admin["username"]})
    db.commit()
    return {"screens_count": len(targets), "version": version, "files": files_payload}


# ─── Целевые версии ПО ───────────────────────────────────────────────────────

@router.get("/versions/target")
def get_target_versions(admin: dict = Depends(get_current_admin), db: Session = Depends(get_db)):
    row = db.execute(text(
        "SELECT os_version, vlc_version, updated_by, updated_at FROM target_versions WHERE id = 1"
    )).fetchone()
    if not row:
        return {"os_version": None, "vlc_version": None, "updated_by": None, "updated_at": None}
    return dict(row._mapping)


@router.post("/versions/target")
def save_target_versions(body: dict = Body(...), current_admin: dict = Depends(require_write),
                          db: Session = Depends(get_db)):
    os_version = (body.get("os_version") or "").strip() or None
    vlc_version = (body.get("vlc_version") or "").strip() or None
    db.execute(text("""
        UPDATE target_versions
        SET os_version = :os, vlc_version = :vlc, updated_by = :who, updated_at = NOW()
        WHERE id = 1
    """), {"os": os_version, "vlc": vlc_version, "who": current_admin["username"]})
    db.execute(text("""
        INSERT INTO audit_log (event_type, title, detail, actor)
        VALUES ('version', 'Целевые версии обновлены', :detail, :who)
    """), {"detail": f"ОС: {os_version or '—'}, плеер (mpv): {vlc_version or '—'}", "who": current_admin["username"]})
    db.commit()
    return {"status": "ok"}
