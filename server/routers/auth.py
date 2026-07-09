"""Авторизация: получение JWT-токена и данные текущего пользователя."""
from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy import text
from sqlalchemy.orm import Session

from deps import get_db, get_current_admin, create_access_token, pwd_context

router = APIRouter()


@router.post("/token")
def login(form_data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    """Получить JWT токен (для администраторов)"""
    row = db.execute(
        text("SELECT username, password_hash, "
             "COALESCE(is_blocked, FALSE) AS is_blocked, "
             "COALESCE(is_active, TRUE) AS is_active "
             "FROM users WHERE username = :u"),
        {"u": form_data.username}
    ).fetchone()
    if not row or not pwd_context.verify(form_data.password, row.password_hash):
        raise HTTPException(status_code=401, detail="Неверный логин или пароль")
    if row.is_blocked:
        raise HTTPException(status_code=403, detail="Пользователь заблокирован")
    if not row.is_active:
        raise HTTPException(status_code=403, detail="Пользователь деактивирован")
    db.execute(text("UPDATE users SET last_login = NOW() WHERE username = :u"), {"u": form_data.username})
    db.commit()
    token = create_access_token({"sub": form_data.username})
    return {"access_token": token, "token_type": "bearer"}


@router.get("/me")
def get_me(current_admin=Depends(get_current_admin), db: Session = Depends(get_db)):
    """Текущий авторизованный пользователь (для фронтенда — роль, права записи,
    признак обязательной смены пароля по умолчанию)."""
    row = db.execute(
        text("SELECT COALESCE(must_change_password, FALSE) AS mcp "
             "FROM users WHERE username = :u"),
        {"u": current_admin["username"]},
    ).fetchone()
    return {
        "username": current_admin["username"],
        "role": current_admin["role"],
        "must_change_password": bool(row.mcp) if row else False,
    }
