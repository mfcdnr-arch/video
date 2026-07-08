# Digital Signage v16.2 — Модульная версия
**Дата**: 2026-07-06  
**Версия**: 16.2  
**Статус**: ✅ Production Ready

## Быстрый старт

### Шаг 1: Перейдите в каталог проекта
```bash
cd Video_miniPC_v16.2_modular
```

### Шаг 2: Запустите сервер
```bash
bash install_server.sh
```
Скрипт сам создаст `.env` со случайными секретами (пароль БД, пароль MinIO,
SECRET_KEY для подписи токенов), соберёт контейнеры и дождётся готовности API.
Миграции БД применяются автоматически при первом старте Postgres.

Повторный запуск безопасен: существующий `.env` не перезаписывается
(пароли и ключи сохраняются), контейнеры просто обновляются.

**После первого входа (admin / admin123) сразу смените пароль администратора:**
Настройки → Пользователи.

Веб-панель доступна на порту 80 (nginx): `http://<IP_сервера>/`.

### Шаг 3: Установите агент на мини ПК
Из корня проекта (скрипт `install.sh` лежит рядом с `install_server.sh`):
```bash
sudo bash install.sh http://10.0.119.100 YOUR_TOKEN 1 toor
```
Аргументы: URL сервера, токен устройства, ID экрана, пользователь X-сессии
(по умолчанию `toor`).

## Структура проекта

```
Video_miniPC_v16.2_modular/
├── README.md                  # Этот файл
├── install_server.sh          # Установка/обновление сервера (Docker)
├── install.sh                 # Установка агента на мини ПК (Astra Linux)
├── docker-compose.yml         # Оркестрация: postgres, redis, minio, api, celery, nginx, ntp
├── ntp/                       # NTP-служба: раздаёт точное время мини-ПК (chrony,
│                              #   синхронизируется с российскими серверами ВНИИФТРИ/MSK-IX)
├── nginx.conf                 # Nginx: панель на порту 80, проксирование /api/, /ws/, /media/
│
├── server/                    # СЕРВЕР (FastAPI + Celery)
│   ├── main.py                # Точка сборки приложения (только подключение роутеров)
│   ├── deps.py                # Общие зависимости: БД-сессия, JWT, токены устройств
│   ├── routers/               # 14 модулей эндпоинтов: auth, system, media, screens,
│   │                          #   playlists, schedule, ticker, agent_ota, users,
│   │                          #   backups, broadcast, reports, websockets, billing
│   ├── static/                # Веб-панель (index.html, js/, css/)
│   ├── migrations/            # SQL-миграции (001–018)
│   ├── tasks.py               # Celery-задачи
│   ├── ws_manager.py          # Реестр WebSocket-подключений
│   ├── Dockerfile
│   └── requirements.txt
│
├── agent/                     # АГЕНТ (мини ПК, Astra Linux)
│   ├── ds_agent.py            # Главный агент
│   ├── ds_player.py           # Плеер (mpv + IPC Unix-сокет)
│   ├── ds_downloader.py       # Загрузчик файлов
│   ├── ds_sync.py             # Синхронизация с сервером
│   ├── ds_heartbeat.py        # Мониторинг
│   ├── ds_cleanup.py          # Очистка устаревших файлов
│   ├── ds_ws_client.py        # WebSocket клиент
│   ├── ds_ota_updater.py      # OTA-обновление агента
│   ├── ds-agent.service       # systemd-сервис
│   ├── config.ini.example     # Пример конфигурации агента
│   └── requirements.txt
│
└── docs/                      # Документация
    ├── CHANGES_2026-07-01.md
    ├── CLEANUP_FULL_2026-07-01.md
    └── DEPLOYMENT_GUIDE_2026-07-01.md
```

Данные для подключения к БД и MinIO (`POSTGRES_DB`, `POSTGRES_USER`,
`DB_PASSWORD`, `MINIO_USER`, `MINIO_PASSWORD`, `SECRET_KEY`) хранятся в `.env`,
который генерирует `install_server.sh`.

## Что нового в v16.2

✅ Модульный сервер: монолитный `main.py` разнесён на 14 роутеров в `server/routers/`,
`main.py` теперь только собирает приложение  
✅ Каталоги переименованы: `app/` → `server/`, `client/` → `agent/`;
инсталлятор агента `install.sh` перенесён в корень проекта  
✅ Аутентификация агентов по токену устройства (заголовок `X-Token`)  
✅ WebSocket-push команд агентам в реальном времени (`ws_manager.py`, роутер `websockets`)  
✅ Биллинг: тарифы рекламодателей и отчёты (`routers/billing.py`, миграция `017_billing.sql`)  
✅ OTA-обновление агентов (`routers/agent_ota.py`, `agent/ds_ota_updater.py`)  

Подробная история изменений — в `docs/CHANGES_2026-07-01.md`.

## Проверка после установки

### На сервере
```bash
cd Video_miniPC_v16.2_modular
docker compose ps                    # все 7 контейнеров должны быть running
curl http://localhost/health         # 200 OK
docker exec ds_ntp chronyc sources   # NTP видит российские серверы времени
```
Затем откройте панель в браузере: `http://<IP_сервера>/`.

### На мини ПК
```bash
systemctl status ds-agent            # active (running)
journalctl -u ds-agent -f            # логи в реальном времени
pgrep -a mpv                         # процесс должен быть запущен
```

## Полное удаление и переустановка

**⚠️ Смотрите `docs/CLEANUP_FULL_2026-07-01.md` для полных инструкций**

Быстро:
```bash
# Удалить сервер (из каталога проекта; -v удаляет тома с БД и медиа!)
docker compose down -v

# Удалить мини ПК
sudo systemctl stop ds-agent
sudo systemctl disable ds-agent
sudo rm -rf /opt/ds-agent /etc/ds-agent /var/log/ds-agent
sudo rm -f /etc/systemd/system/ds-agent.service
sudo systemctl daemon-reload
```

## Поддержка

Вопросы и проблемы:
1. Проверьте логи: `journalctl -u ds-agent -f`
2. Проверьте сеть: `ping 10.0.119.100`
3. Проверьте API: `curl http://10.0.119.100/health`

## Лицензия

Digital Signage v16.2 — Все права защищены © 2026

---
**Версия**: 16.2  
**Дата**: 2026-07-06  
**Статус**: ✅ Production Ready
