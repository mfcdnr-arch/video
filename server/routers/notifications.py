"""Настройки уведомлений в мессенджер MAX + журнал срабатываний + тест."""
from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from deps import get_db, get_current_admin, require_superadmin

router = APIRouter()

_EDITABLE = {
    "enabled": bool, "max_token": str, "max_chat_id": str, "base_url": str,
    "offline_minutes": int, "disk_free_pct": int,
    "notify_offline": bool, "notify_disk": bool, "notify_broken": bool,
    "notify_display": bool,
}


def _load(db):
    return db.execute(text("SELECT * FROM notification_settings WHERE id = 1")).mappings().first()


@router.get("/notifications/settings")
def get_settings(admin: dict = Depends(require_superadmin), db: Session = Depends(get_db)):
    """Текущие настройки. Токен маскируется — наружу не отдаём целиком."""
    s = dict(_load(db))
    if s.get("max_token"):
        s["max_token_set"] = True
        s["max_token"] = "••••••" + s["max_token"][-4:]
    else:
        s["max_token_set"] = False
    return s


@router.patch("/notifications/settings")
def update_settings(body: dict = Body(...), admin: dict = Depends(require_superadmin),
                    db: Session = Depends(get_db)):
    sets, params = [], {}
    for field, caster in _EDITABLE.items():
        if field not in body:
            continue
        val = body[field]
        # пустой токен в PATCH означает «не менять» (чтобы маска не затёрла реальный)
        if field == "max_token" and (val is None or str(val).strip() == "" or str(val).startswith("••")):
            continue
        if field in ("offline_minutes", "disk_free_pct"):
            try:
                val = int(val)
            except (TypeError, ValueError):
                raise HTTPException(400, f"{field}: ожидается число")
            if val < 1:
                raise HTTPException(400, f"{field}: должно быть больше 0")
        if caster is bool:
            val = bool(val)
        sets.append(f"{field} = :{field}")
        params[field] = val
    if not sets:
        raise HTTPException(400, "Нечего обновлять")
    sets.append("updated_at = NOW()")
    db.execute(text(f"UPDATE notification_settings SET {', '.join(sets)} WHERE id = 1"), params)
    db.commit()
    return {"status": "ok"}


@router.post("/notifications/test")
def send_test(admin: dict = Depends(require_superadmin), db: Session = Depends(get_db)):
    """Отправить пробное сообщение по текущим настройкам."""
    from ds_notify import send_max_message
    s = _load(db)
    if not (s["max_token"] and s["max_chat_id"]):
        raise HTTPException(400, "Сначала задайте токен бота и chat_id")
    ok, detail = send_max_message(s["base_url"], s["max_token"], s["max_chat_id"],
                                  "🔔 Digital Signage: проверка связи. Уведомления настроены верно.")
    if not ok:
        raise HTTPException(502, f"MAX не принял сообщение: {detail}")
    return {"status": "ok", "detail": "Сообщение отправлено"}


@router.get("/notifications/log")
def get_log(admin: dict = Depends(get_current_admin), db: Session = Depends(get_db)):
    rows = db.execute(text("""
        SELECT alert_key, type, message, is_active, sent_ok, created_at, resolved_at
        FROM notification_alerts
        ORDER BY created_at DESC LIMIT 50
    """)).fetchall()
    return [dict(r._mapping) for r in rows]
