"""Кто может читать файлы медиатеки: ролики, баннеры, документы, миниатюры.

Одно правило для трёх путей, которые отдают эти файлы:
  • GET /files/download/{id}  — агент мини-ПК (X-Token, с докачкой) и кнопка
    «Скачать» в панели;
  • GET /media/{id}/thumbnail — миниатюры в панели (<img src>);
  • nginx /media/<имя>         — просмотр ролика в панели (<video src>); nginx
    перед отдачей спрашивает GET /files/auth (auth_request).

Раньше все три отдавали файл без входа, а номера роликов идут подряд: перебором
выкачивалась вся медиатека — и ролики на модерации, и отклонённые, и сканы
договоров из папки «Документы».

Кто проходит:
  • экран — X-Token любого зарегистрированного экрана (так агент качает всегда,
    в том числе старые версии), и только готовый одобренный ролик — то, что
    экрану вообще могут выдать в эфир; остальное для него «не найдено»;
  • вошедший в панель — JWT в заголовке Authorization (curl, fetch) или cookie
    ds_media. Cookie нужна, потому что <img>, <video> и ссылка заголовок с
    токеном не несут, а видео в сотни мегабайт через blob не скачать. Роль
    «рекламодатель» медиатеку не видит, заблокированный — тоже (403).

Cookie ds_media — не токен входа: она подписана отдельным ключом (производным
от SECRET_KEY), поэтому как Bearer в API её не предъявить — открывает только
файлы. httpOnly (скрипт страницы её не прочитает), SameSite=Strict (чужой сайт
её не пошлёт), живёт не дольше JWT, из которого выдана (POST /session/media), и
в адрес не попадает — значит, не оседает ни в логах nginx, ни в истории
браузера. Пользователь проверяется в БД на каждый запрос: блокировка действует
сразу, а не по истечении cookie.
"""
import hashlib
import mimetypes
import os
import re
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import quote

from fastapi import Depends, HTTPException, Request
from fastapi.responses import FileResponse, Response
from jose import JWTError, jwt
from sqlalchemy import text
from sqlalchemy.orm import Session

from deps import ADVERTISER_ROLE, ALGORITHM, SECRET_KEY, _fetch_user_by_username, get_db

MEDIA_COOKIE = "ds_media"
_SCOPE = "media"
# Свой ключ подписи: cookie файлов не годится как токен входа, и наоборот
_MEDIA_KEY = hashlib.sha256(("ds-media-cookie:" + SECRET_KEY).encode()).hexdigest()

PANEL = "panel"     # вошедший в панель
DEVICE = "device"   # экран по X-Token

# nginx (location /api/) ставит этот заголовок: значит, ответ можно отдать
# через X-Accel-Redirect — файл выдаст сам nginx из internal-локации, с Range
# (докачка агента, перемотка) и sendfile. Без nginx (прямо в API, тесты) —
# обычный FileResponse.
ACCEL_HEADER = "x-ds-accel"
ACCEL_PREFIX = "/_media_files/"

_RANGE_START = re.compile(r"^\s*bytes\s*=\s*(\d+)\s*-")


def make_media_cookie(username: str, expires_at: datetime) -> str:
    """Значение cookie ds_media для пользователя username до момента expires_at."""
    return jwt.encode({"sub": username, "scope": _SCOPE, "exp": expires_at},
                      _MEDIA_KEY, algorithm=ALGORITHM)


def media_cookie_user(value: Optional[str]) -> Optional[str]:
    """Имя пользователя из cookie ds_media; None — нет, подделка, чужой ключ, истекла."""
    if not value:
        return None
    try:
        payload = jwt.decode(value, _MEDIA_KEY, algorithms=[ALGORITHM])
    except JWTError:
        return None
    if payload.get("scope") != _SCOPE:
        return None
    return payload.get("sub") or None


def set_media_cookie(response: Response, request: Request, username: str,
                     expires_at: datetime) -> int:
    """Поставить cookie ds_media до expires_at. Возвращает срок жизни в секундах."""
    max_age = max(1, int((expires_at - datetime.now(timezone.utc)).total_seconds()))
    response.set_cookie(MEDIA_COOKIE, make_media_cookie(username, expires_at),
                        max_age=max_age, path="/", httponly=True, samesite="strict",
                        secure=request.url.scheme == "https")
    return max_age


def clear_media_cookie(response: Response, request: Request) -> None:
    response.delete_cookie(MEDIA_COOKIE, path="/", httponly=True, samesite="strict",
                           secure=request.url.scheme == "https")


def _login_user(token: str) -> Optional[str]:
    """Имя пользователя из JWT входа (заголовок Authorization)."""
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM]).get("sub") or None
    except JWTError:
        return None


def _panel_status(db: Session, username: str) -> Optional[str]:
    """PANEL — пускать; "denied" — пользователь есть, но файлы ему нельзя; None — нет такого."""
    user = _fetch_user_by_username(db, username)
    if not user:
        return None
    if not user.get("is_active", True) or user.get("is_blocked", False):
        return "denied"
    if str(user.get("role", "")).lower() == ADVERTISER_ROLE:
        return "denied"
    return PANEL


def _resolve(request: Request, db: Session, allow_device: bool) -> str:
    auth = request.headers.get("authorization") or ""
    bearer = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    login = _login_user(bearer) if bearer else None
    denied = False
    for username in (login, media_cookie_user(request.cookies.get(MEDIA_COOKIE))):
        if not username:
            continue
        status = _panel_status(db, username)
        if status == PANEL:
            return PANEL
        denied = denied or status == "denied"
    if allow_device:
        # Агент шлёт X-Token; Authorization: Bearer <токен экрана> — запасной
        # вариант, как в deps.verify_any_device_token
        device = request.headers.get("x-token") or (bearer if bearer and not login else "")
        if device and db.execute(text("SELECT id FROM screens WHERE token = :t LIMIT 1"),
                                 {"t": device}).first():
            return DEVICE
    if denied:
        raise HTTPException(403, "Файлы медиатеки этой учётной записи недоступны")
    raise HTTPException(401, "Файлы медиатеки — только по входу в панель",
                        headers={"WWW-Authenticate": "Bearer"})


def media_reader(request: Request, db: Session = Depends(get_db)) -> str:
    """Зависимость: панель (JWT или cookie) или экран (X-Token). Без входа — 401."""
    return _resolve(request, db, allow_device=True)


def panel_media_reader(request: Request, db: Session = Depends(get_db)) -> str:
    """Зависимость: только панель (JWT или cookie). Токен экрана сюда не годится — 401."""
    return _resolve(request, db, allow_device=False)


def device_may_read(row) -> bool:
    """Экрану — только то, что можно выдать в эфир: готовый и одобренный файл."""
    return row.status == "ready" and row.review_status == "approved"


def _content_disposition(filename: str) -> str:
    # Как в starlette FileResponse: не-ASCII имя — через filename* (RFC 5987)
    quoted = quote(filename)
    if quoted != filename:
        return f"attachment; filename*=utf-8''{quoted}"
    return f'attachment; filename="{filename}"'


def _range_past_end(request: Request, size: int) -> bool:
    m = _RANGE_START.match(request.headers.get("range") or "")
    return bool(m) and int(m.group(1)) >= size


def media_file_response(request: Request, filepath: str, filename: str) -> Response:
    """Отдать файл медиатеки (вызывающий уже проверил, кому и что можно).

    За nginx — пустой ответ с X-Accel-Redirect: файл отдаёт nginx с Range, так
    что у агента работает докачка (FileResponse этой версии starlette Range не
    умеет и отдаёт файл целиком). Исключение — Range, начинающийся за концом
    файла: обрывок у агента длиннее самого файла (так бывало, пока докачка не
    работала), и nginx ответил бы 416 на каждой синхронизации. Тогда файл
    отдаётся целиком (200) — агент сверит MD5, сотрёт обрывок и скачает начисто.
    """
    media_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    headers = {"Cache-Control": "private, no-cache"}
    if request.headers.get(ACCEL_HEADER) == "1" \
            and not _range_past_end(request, os.path.getsize(filepath)):
        headers["X-Accel-Redirect"] = ACCEL_PREFIX + quote(filename)
        headers["Content-Disposition"] = _content_disposition(filename)
        return Response(status_code=200, media_type=media_type, headers=headers)
    return FileResponse(filepath, media_type=media_type, filename=filename, headers=headers)
