"""Управление пользователями панели (только супер-админ), смена своего пароля, журнал аудита."""
from fastapi import APIRouter, Body, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.orm import Session

from deps import get_db, get_current_admin, require_superadmin, pwd_context

router = APIRouter()

ROLES = ("admin", "auditor", "moderator", "advertiser")


def _ensure_advertiser(db: Session, name: str, actor: str) -> int:
    """
    Кабинет рекламодателя под учётную запись: одноимённый рекламодатель уже
    есть — привязываемся к нему, нет — заводим вместе с папками медиатеки.

    Так «кабинет» не дублирует рекламодателя, который уже ведётся в медиатеке
    (иначе ролики и счета остались бы у одной записи, а кабинет смотрел бы в
    другую, пустую).
    """
    from routers.media import ensure_owner_folders

    name = (name or "").strip()
    if not name:
        raise HTTPException(400, "Укажите наименование рекламодателя")
    row = db.execute(text("SELECT id FROM advertisers WHERE lower(name) = lower(:n)"),
                     {"n": name}).fetchone()
    if row:
        # Кабинет уже был — папки могли не создаваться (запись из старой
        # версии), поэтому добиваем их и здесь.
        ensure_owner_folders(db, row.id)
        return row.id
    new_id = db.execute(text(
        "INSERT INTO advertisers (name) VALUES (:n) RETURNING id"), {"n": name}).fetchone()[0]
    ensure_owner_folders(db, new_id)
    db.execute(text("""
        INSERT INTO audit_log (event_type, title, detail, actor)
        VALUES ('user', 'Создан кабинет рекламодателя', :d, :who)
    """), {"d": name, "who": actor})
    return new_id


# ─── Пользователи панели (управление, только супер-админ) ───────────────────

@router.get("/users")
def get_users(admin: dict = Depends(require_superadmin), db: Session = Depends(get_db)):
    rows = db.execute(text("""
        SELECT u.id, u.username, u.role, u.is_blocked, u.last_login,
               u.first_name, u.last_name, u.full_name,
               u.advertiser_id, a.name AS advertiser_name
        FROM users u
        LEFT JOIN advertisers a ON a.id = u.advertiser_id
        ORDER BY u.username
    """)).fetchall()
    return [dict(r._mapping) for r in rows]


@router.post("/users")
def create_user(body: dict = Body(...), admin: dict = Depends(require_superadmin),
                 db: Session = Depends(get_db)):
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    role = body.get("role") or "auditor"
    first_name = body.get("first_name") or None
    last_name = body.get("last_name") or None

    if not username or len(password) < 6:
        raise HTTPException(status_code=400, detail="Логин обязателен, пароль — минимум 6 символов")
    if role not in ROLES:
        raise HTTPException(status_code=400,
                            detail="Роль должна быть admin, auditor, moderator или advertiser")

    exists = db.execute(text("SELECT id FROM users WHERE username = :u"), {"u": username}).fetchone()
    if exists:
        raise HTTPException(status_code=400, detail="Пользователь с таким логином уже существует")

    # Роль «рекламодатель» = кабинет. Наименование берём из формы, а если его
    # не заполнили — из имени учётной записи, чтобы кабинет не остался
    # безымянным (в документах это наименование пойдёт в акт и справку).
    advertiser_id = None
    if role == "advertiser":
        name = (body.get("advertiser_name") or "").strip() \
            or " ".join(x for x in (last_name, first_name) if x).strip() \
            or username
        advertiser_id = _ensure_advertiser(db, name, admin["username"])

    row = db.execute(text("""
        INSERT INTO users (username, password_hash, role, first_name, last_name, created_by,
                           advertiser_id)
        VALUES (:u, :ph, :r, :fn, :ln, :who, :aid)
        RETURNING id, username, role, advertiser_id
    """), {"u": username, "ph": pwd_context.hash(password), "r": role,
           "fn": first_name, "ln": last_name, "who": admin["username"],
           "aid": advertiser_id}).fetchone()

    db.execute(text("""
        INSERT INTO audit_log (event_type, title, detail, actor)
        VALUES ('user', 'Пользователь создан', :detail, :who)
    """), {"detail": username, "who": admin["username"]})
    db.commit()
    return dict(row._mapping)


def _get_target_user(db: Session, user_id: int):
    user = db.execute(text("SELECT id, username, role FROM users WHERE id = :id"),
                       {"id": user_id}).fetchone()
    if not user:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    if user.role == "superadmin":
        raise HTTPException(status_code=403, detail="Нельзя изменять супер-админа")
    return user


@router.patch("/users/{user_id}/role")
def change_user_role(user_id: int, body: dict = Body(...),
                      admin: dict = Depends(require_superadmin), db: Session = Depends(get_db)):
    target = _get_target_user(db, user_id)
    role = body.get("role")
    if role not in ROLES:
        raise HTTPException(status_code=400,
                            detail="Роль должна быть admin, auditor, moderator или advertiser")
    # Стал рекламодателем — заводим/находим кабинет; перестал им быть — рвём
    # привязку, иначе учётка сохранит доступ к чужому теперь кабинету.
    aid = None
    if role == "advertiser":
        name = (body.get("advertiser_name") or "").strip() or target.username
        aid = _ensure_advertiser(db, name, admin["username"])
    db.execute(text("UPDATE users SET role = :r, advertiser_id = :aid WHERE id = :id"),
               {"r": role, "aid": aid, "id": user_id})
    db.execute(text("""
        INSERT INTO audit_log (event_type, title, detail, actor)
        VALUES ('user', 'Роль изменена', :detail, :who)
    """), {"detail": f"{target.username} -> {role}", "who": admin["username"]})
    db.commit()
    return {"status": "ok"}


@router.patch("/users/{user_id}/block")
def toggle_user_block(user_id: int, body: dict = Body(...),
                       admin: dict = Depends(require_superadmin), db: Session = Depends(get_db)):
    target = _get_target_user(db, user_id)
    if target.username == admin["username"]:
        raise HTTPException(status_code=400, detail="Нельзя заблокировать самого себя")
    blocked = bool(body.get("blocked"))
    db.execute(text("UPDATE users SET is_blocked = :b WHERE id = :id"), {"b": blocked, "id": user_id})
    db.execute(text("""
        INSERT INTO audit_log (event_type, title, detail, actor)
        VALUES ('user', :title, :detail, :who)
    """), {"title": "Пользователь заблокирован" if blocked else "Пользователь разблокирован",
           "detail": target.username, "who": admin["username"]})
    db.commit()
    return {"status": "ok"}


@router.patch("/users/{user_id}/reset-password")
def reset_user_password(user_id: int, body: dict = Body(...),
                         admin: dict = Depends(require_superadmin), db: Session = Depends(get_db)):
    target = _get_target_user(db, user_id)
    new_password = body.get("new_password") or ""
    if len(new_password) < 6:
        raise HTTPException(status_code=400, detail="Пароль — минимум 6 символов")
    db.execute(text("UPDATE users SET password_hash = :ph WHERE id = :id"),
               {"ph": pwd_context.hash(new_password), "id": user_id})
    db.execute(text("""
        INSERT INTO audit_log (event_type, title, detail, actor)
        VALUES ('user', 'Пароль сброшен администратором', :detail, :who)
    """), {"detail": target.username, "who": admin["username"]})
    db.commit()
    return {"status": "ok"}


@router.patch("/users/{user_id}")
def update_user(user_id: int, body: dict = Body(...),
                 admin: dict = Depends(require_superadmin), db: Session = Depends(get_db)):
    _get_target_user(db, user_id)
    username = (body.get("username") or "").strip()
    if not username:
        raise HTTPException(status_code=400, detail="Логин не может быть пустым")

    dup = db.execute(text("SELECT id FROM users WHERE username = :u AND id != :id"),
                      {"u": username, "id": user_id}).fetchone()
    if dup:
        raise HTTPException(status_code=400, detail="Пользователь с таким логином уже существует")

    db.execute(text("""
        UPDATE users SET username = :u, first_name = :fn, last_name = :ln WHERE id = :id
    """), {"u": username, "fn": body.get("first_name"), "ln": body.get("last_name"), "id": user_id})
    db.commit()
    return {"status": "ok"}


@router.delete("/users/{user_id}")
def delete_user(user_id: int, admin: dict = Depends(require_superadmin), db: Session = Depends(get_db)):
    target = _get_target_user(db, user_id)
    if target.username == admin["username"]:
        raise HTTPException(status_code=400, detail="Нельзя удалить самого себя")
    db.execute(text("DELETE FROM users WHERE id = :id"), {"id": user_id})
    db.execute(text("""
        INSERT INTO audit_log (event_type, title, detail, actor)
        VALUES ('user', 'Пользователь удалён', :detail, :who)
    """), {"detail": target.username, "who": admin["username"]})
    db.commit()
    return {"status": "ok"}


@router.post("/me/password")
def change_my_password(body: dict = Body(...), current_admin: dict = Depends(get_current_admin),
                        db: Session = Depends(get_db)):
    old_password = body.get("old_password") or ""
    new_password = body.get("new_password") or ""
    if len(new_password) < 6:
        raise HTTPException(status_code=400, detail="Новый пароль — минимум 6 символов")

    row = db.execute(text("SELECT password_hash FROM users WHERE username = :u"),
                      {"u": current_admin["username"]}).fetchone()
    if not row or not pwd_context.verify(old_password, row.password_hash):
        raise HTTPException(status_code=400, detail="Неверный текущий пароль")

    # Смена пароля снимает флаг обязательной смены (закрывает форс-экран входа).
    db.execute(text("UPDATE users SET password_hash = :ph, must_change_password = FALSE "
                    "WHERE username = :u"),
               {"ph": pwd_context.hash(new_password), "u": current_admin["username"]})
    db.commit()
    return {"status": "ok"}


# ─── Аудит ────────────────────────────────────────────────────────────────────

@router.get("/audit")
def get_audit(limit: int = Query(20), admin: dict = Depends(get_current_admin),
              db: Session = Depends(get_db)):
    rows = db.execute(text("""
        SELECT event_type, title, detail, actor, created_at
        FROM audit_log ORDER BY created_at DESC LIMIT :limit
    """), {"limit": limit}).fetchall()
    return [dict(r._mapping) for r in rows]
