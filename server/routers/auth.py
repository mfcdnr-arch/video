"""Авторизация: получение JWT-токена, данные текущего пользователя, cookie
для файлов медиатеки."""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.security import OAuth2PasswordRequestForm
from jose import jwt
from sqlalchemy import text
from sqlalchemy.orm import Session

from deps import (ADVERTISER_ROLE, ALGORITHM, SECRET_KEY, get_db, get_current_admin,
                  create_access_token, oauth2_scheme, pwd_context)
from media_access import clear_media_cookie, set_media_cookie

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


@router.post("/session/media")
def media_session(request: Request, response: Response,
                  token: str = Depends(oauth2_scheme),
                  current_admin=Depends(get_current_admin)):
    """Cookie ds_media для файлов медиатеки (миниатюры, просмотр, «Скачать»):
    <img>, <video> и ссылка не несут заголовок с токеном. Живёт ровно до конца
    текущего входа (exp JWT) — панель берёт её при старте, перед просмотром и
    скачиванием. Подробно — media_access.py."""
    if str(current_admin.get("role", "")).lower() == ADVERTISER_ROLE:
        raise HTTPException(403, "Файлы медиатеки этой учётной записи недоступны")
    exp = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM]).get("exp")
    if not exp:   # create_access_token срок ставит всегда; бессрочной cookie не будет
        raise HTTPException(401, "Вход без срока действия — войдите заново")
    expires_at = datetime.fromtimestamp(int(exp), timezone.utc)
    max_age = set_media_cookie(response, request, current_admin["username"], expires_at)
    return {"status": "ok", "expires_in": max_age}


@router.delete("/session/media")
def drop_media_session(request: Request, response: Response):
    """Снять cookie ds_media (выход из панели). Без входа: снимает только её."""
    clear_media_cookie(response, request)
    return {"status": "ok"}
