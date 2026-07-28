# Project: Digital Signage v16.2 (Video_miniPC, модульная версия)

Система цифровых экранов (digital signage): сервер управляет контентом,
расписанием и рекламными кампаниями; агенты на мини-ПК (Astra Linux)
проигрывают контент на экранах в полноэкранном режиме.

> Этот файл кешируется на всю сессию. Держи здесь **только стабильный**
> контент. Динамику (текущие задачи, даты, меняющиеся версии, TODO) сюда
> НЕ добавляй — она инвалидирует кеш. Такое место — git-история и заметки.

## Stack

- Backend: Python + FastAPI (uvicorn), запускается в контейнере `ds_api` на порту 8000
- БД: PostgreSQL 15 (`postgres:15-alpine`), порт 5432
- Кеш/очереди: Redis 7 (`redis:7-alpine`), порт 6379
- Хранилище медиа: MinIO (S3-совместимое), порты 9000 (API) / 9001 (консоль)
- Фоновые задачи: Celery (уведомления → MAX)
- Reverse-proxy: nginx (`nginx:alpine`), внешний порт 80
- Синхронизация времени: NTP-сервер в стеке, порт 123/udp
- Frontend: vanilla JS, статика в `server/static/` (`index.html` + модульные вью)
- Агент (мини-ПК): Python, плеер на **mpv** через IPC Unix-сокет, systemd-юнит `ds-agent.service`

## Структура

- `server/main.py` — сборка FastAPI-приложения, подключение роутеров
- `server/routers/` — роутеры по доменам: auth, users, screens, media, playlists,
  schedule, campaigns, broadcast, billing, advertiser_office, reports,
  notifications, backups, ticker, system, agent_ota, websockets
- `server/migrations/` — SQL-миграции с номерами `001`…`029`, применяются
  автоматически при первом старте Postgres
- `server/tests/` — pytest (billing, campaigns, контроль монитора), запуск через `run_tests.sh`
- `ui_tests/ui_test.js` — браузерный E2E-прогон панели (puppeteer), запуск через `ui_test.sh`
- `agent/` — код агента для мини-ПК: `ds_agent.py`, `ds_player.py` (mpv),
  `ds_sync.py`, `ds_downloader.py`, `ds_heartbeat.py`, `ds_ws_client.py`,
  `ds_ota_updater.py`, `ds_cleanup.py`

## Conventions

- Секреты — только через `.env` (пароль БД, пароль MinIO, SECRET_KEY);
  никаких хардкодов. Пример значений — в `.env.example`
- `install_server.sh` при первой установке сам генерирует случайные секреты
  и пароль администратора; повторный запуск не перезаписывает существующий `.env`
- Аутентификация агента — по заголовку `X-Token` (device auth)
- Каждая новая миграция должна быть добавлена в список initdb в `docker-compose.yml`
- Эфир показывает только контент со статусом `approved` (модерация 38-ФЗ)
- Ограничение проекта: **никакого AI/ML** в продукте — только детерминированная логика

## Key commands

- `bash install_server.sh` — собрать и поднять весь стек (идемпотентно)
- `bash run_tests.sh` — прогнать pytest в контейнере `ds_api`
- `bash smoke_test.sh` — smoke-проверка ключевых HTTP-эндпоинтов
- `bash ui_test.sh` — браузерный прогон панели (разделы, кнопки, циклы CRUD)
- `docker compose logs -f api` — следить за логами backend
- `bash migrate.sh` — применить/проверить миграции БД
- `bash collect_diag.sh` — собрать диагностику сервера
- `bash selfheal.sh [--dry-run]` — самодиагностика/автолечение стека на хосте
  (панельный аналог: Настройки → Диагностика сервера, `GET /system/selfcheck`)
