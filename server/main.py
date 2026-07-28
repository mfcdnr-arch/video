"""
Digital Signage — Вариант Б (Мини ПК)
FastAPI сервер: управление экранами, расписание, файлы, команды синхронизации

Точка сборки приложения. Сама логика эндпоинтов разнесена по routers/*
(один модуль — один логический раздел панели), общие зависимости (БД-сессия,
JWT, проверка токенов, MEDIA_PATH, pwd_context) — в deps.py.
"""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(
    title="Digital Signage API — Вариант Б",
    description="Управление мини ПК, расписаниями, синхронизацией",
    version="2.0"
)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

from routers import (
    auth, system, media, screens, playlists, schedule,
    ticker, agent_ota, users, backups, broadcast, reports, websockets,
    billing, campaigns, notifications, advertiser_office,
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

# Роутеры-заглушки из более ранней версии проекта (по одному /health на модуль).
from admin_panel import router as admin_router
app.include_router(admin_router)
from agent_updater import ota_router as legacy_agent_updater_router
app.include_router(legacy_agent_updater_router, prefix="/api")
