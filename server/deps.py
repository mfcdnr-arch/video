import os
from datetime import datetime, timedelta, timezone
from typing import Generator, Optional

from fastapi import Depends, HTTPException, Header, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    os.getenv("POSTGRES_DSN", "postgresql://postgres:postgres@postgres:5432/postgres"),
)
MEDIA_PATH = os.getenv("MEDIA_PATH", "/data/media")
os.makedirs(MEDIA_PATH, exist_ok=True)

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

SECRET_KEY = os.getenv("SECRET_KEY", "change-me-super-secret-key")
ALGORITHM = os.getenv("ALGORITHM", "HS256")
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "60"))

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/token")


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + (
        expires_delta or timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def _fetch_user_by_username(db: Session, username: str):
    try:
        row = db.execute(
            text(
                """
                SELECT username,
                       COALESCE(role, 'admin') AS role,
                       COALESCE(is_active, TRUE) AS is_active,
                       COALESCE(is_blocked, FALSE) AS is_blocked
                FROM users
                WHERE username = :username
                LIMIT 1
                """
            ),
            {"username": username},
        ).mappings().first()
        return row
    except Exception:
        return {
            "username": username,
            "role": "admin",
            "is_active": True,
            "is_blocked": False,
        }


def get_current_admin(
    token: str = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
):
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )

    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username = payload.get("sub")
        if not username:
            raise credentials_exception
    except JWTError:
        raise credentials_exception

    user = _fetch_user_by_username(db, username)
    if not user:
        raise credentials_exception

    if not user.get("is_active", True):
        raise HTTPException(status_code=403, detail="User is inactive")
    if user.get("is_blocked", False):
        raise HTTPException(status_code=403, detail="User is blocked")

    return user


def verify_device_token(
    screen_id: int,
    x_token: Optional[str] = Header(default=None, alias="X-Token"),
    authorization: Optional[str] = Header(default=None),
    db: Session = Depends(get_db),
):
    """
    Проверяет, что заголовок X-Token принадлежит именно экрану screen_id
    из пути запроса. Агент шлёт X-Token на все свои запросы (см.
    agent/ds_agent.py, ds_heartbeat.py, ds_downloader.py, ds_sync.py —
    везде `session.headers["X-Token"] = token`), поэтому X-Token — основной
    заголовок; Authorization: Bearer оставлен как запасной вариант.

    ИСПРАВЛЕНО: раньше эта функция проверяла токен по несуществующей таблице
    `devices` (запрос падал в except и ВСЕГДА пропускал любой токен), и её
    вообще никто не подключал к эндпоинтам через Depends(). Реальные токены
    экранов лежат в screens.token (выдаются в /minipc/register).
    """
    token = x_token
    if not token and authorization and authorization.lower().startswith("bearer "):
        token = authorization.split(" ", 1)[1].strip()

    if not token:
        raise HTTPException(status_code=401, detail="Device token missing")

    row = db.execute(
        text("SELECT id, name, token FROM screens WHERE id = :sid"),
        {"sid": screen_id},
    ).mappings().first()

    if not row or row["token"] != token:
        raise HTTPException(status_code=401, detail="Invalid device token")

    return row


def require_write(current_admin=Depends(get_current_admin)):
    role = str(current_admin.get("role", "")).lower()
    # moderator — специализированная роль 38-ФЗ: может только одобрять/отклонять
    # рекламу (см. require_moderator), контент и настройки не меняет.
    if role in {"viewer", "observer", "auditor", "read_only", "readonly", "moderator"}:
        raise HTTPException(status_code=403, detail="Write access required")
    return current_admin


def require_moderator(current_admin=Depends(get_current_admin)):
    """Право модерировать рекламу (38-ФЗ): модератор и любой пишущий админ."""
    role = str(current_admin.get("role", "")).lower()
    if role in {"viewer", "observer", "auditor", "read_only", "readonly"}:
        raise HTTPException(status_code=403, detail="Требуется право модерации")
    return current_admin


def require_superadmin(current_admin: dict = Depends(get_current_admin)):
    if current_admin.get("role") != "superadmin":
        raise HTTPException(status_code=403, detail="Доступно только супер-админу")
    return current_admin
