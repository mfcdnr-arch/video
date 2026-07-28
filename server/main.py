"""
Digital Signage — Вариант Б (Мини ПК)
FastAPI сервер: управление экранами, расписание, файлы, команды синхронизации

Точка сборки приложения. Сама логика эндпоинтов разнесена по routers/*
(один модуль — один логический раздел панели), общие зависимости (БД-сессия,
JWT, проверка токенов, MEDIA_PATH, pwd_context) — в deps.py.
"""
import logging

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from jose import JWTError, jwt as jose_jwt
from sqlalchemy import text

from deps import (ALGORITHM, ADVERTISER_ROLE, SECRET_KEY, SessionLocal,
                  is_path_allowed_for_advertiser)

log = logging.getLogger(__name__)

app = FastAPI(
    title="Digital Signage API — Вариант Б",
    description="Управление мини ПК, расписаниями, синхронизацией",
    version="2.0"
)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.middleware("http")
async def advertiser_guard(request: Request, call_next):
    """
    Роль «рекламодатель» видит ТОЛЬКО свой кабинет.

    Проверка стоит на входе и работает по принципу «запрещено по умолчанию»:
    любой путь, кроме явно разрешённых в deps._ADV_ALLOWED, закрыт. Так один
    забытый эндпоинт не превращается в утечку чужого эфира, счетов и списка
    рекламодателей — а закрывать полсотни эндпоинтов по одному пришлось бы
    именно с таким риском.

    Совпадение id кабинета проверяется отдельно (deps.advertiser_scope):
    здесь — какие пути вообще доступны, там — чей это кабинет.
    """
    auth = request.headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        try:
            username = jose_jwt.decode(auth[7:], SECRET_KEY, algorithms=[ALGORITHM]).get("sub")
        except JWTError:
            username = None
        if username:
            try:
                with SessionLocal() as db:
                    row = db.execute(text("SELECT role FROM users WHERE username = :u"),
                                     {"u": username}).fetchone()
                role = str(row.role).lower() if row and row.role else ""
            except Exception as e:
                # БД недоступна — не пропускаем «на всякий случай»: пусть
                # запрос дойдёт до эндпоинта, там своя проверка авторизации.
                log.warning("[guard] не удалось прочитать роль %s: %s", username, e)
                role = ""
            if role == ADVERTISER_ROLE and not is_path_allowed_for_advertiser(request.url.path):
                return JSONResponse({"detail": "Доступно только в вашем кабинете"},
                                    status_code=403)
    return await call_next(request)

from routers import (
    auth, system, media, screens, playlists, schedule,
    ticker, agent_ota, users, backups, broadcast, reports, websockets,
    billing, campaigns, notifications, advertiser_office, advertiser_docs,
)

app.include_router(auth.router)
app.include_router(system.router)
app.include_router(media.router)
app.include_router(screens.router)
app.include_router(playlists.router)
app.include_router(schedule.router)
app.include_router(ticker.router)
app.include_router(agent_ota.router)
app.include_router(users.router)
app.include_router(backups.router)
app.include_router(broadcast.router)
app.include_router(reports.router)
app.include_router(websockets.router)
app.include_router(billing.router)
app.include_router(campaigns.router)
app.include_router(notifications.router)
app.include_router(advertiser_office.router)
app.include_router(advertiser_docs.router)

# Роутеры-заглушки из более ранней версии проекта (по одному /health на модуль).
from admin_panel import router as admin_router
app.include_router(admin_router)
from agent_updater import ota_router as legacy_agent_updater_router
app.include_router(legacy_agent_updater_router, prefix="/api")
