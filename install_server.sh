#!/bin/bash
# install_server.sh — первичная установка сервера Digital Signage.
# Использование:  bash install_server.sh            (полная установка)
#                 bash install_server.sh --env-only (только создать .env, без запуска)
#
# Что делает:
#   1. Проверяет наличие Docker и Docker Compose v2.
#   2. Создаёт .env с автоматически сгенерированными секретами
#      (пароль БД, пароль MinIO, SECRET_KEY для подписи JWT).
#      Существующий .env НИКОГДА не перезаписывается — при обновлении
#      установки пароли и ключи сохраняются.
#   3. Собирает и запускает весь стек (docker compose up -d --build);
#      миграции БД применяются автоматически при первом старте Postgres.
#   4. Ждёт готовности API и печатает адрес панели.
#
# После установки ОБЯЗАТЕЛЬНО смените пароль администратора по умолчанию
# (admin / admin123) через панель: Настройки → Пользователи.
set -euo pipefail
cd "$(dirname "$0")"

# ── 1. Проверки окружения ────────────────────────────────────────────────────
command -v docker >/dev/null 2>&1 || {
    echo "ОШИБКА: Docker не установлен. Установите: https://docs.docker.com/engine/install/"
    exit 1
}
docker compose version >/dev/null 2>&1 || {
    echo "ОШИБКА: Docker Compose v2 не найден (нужна команда 'docker compose')."
    exit 1
}

# ── 2. Генерация секретов ────────────────────────────────────────────────────
# openssl есть почти везде; на всякий случай запасной вариант через /dev/urandom.
gen_hex() {  # gen_hex <байт> → hex-строка вдвое длиннее
    openssl rand -hex "$1" 2>/dev/null \
        || head -c "$1" /dev/urandom | od -An -tx1 | tr -d ' \n'
}

if [ -f .env ]; then
    echo "• .env уже существует — пароли и ключи НЕ перезаписываются."
else
    cat > .env <<EOF
# Сгенерировано install_server.sh $(date '+%Y-%m-%d %H:%M')
# НЕ публикуйте этот файл и не копируйте его между серверами.
POSTGRES_DB=display_system
POSTGRES_USER=display_user
DB_PASSWORD=$(gen_hex 16)
MINIO_USER=ds_minio
MINIO_PASSWORD=$(gen_hex 16)
SECRET_KEY=$(gen_hex 32)
EOF
    chmod 600 .env
    echo "• Создан .env: пароль БД, пароль MinIO и SECRET_KEY сгенерированы автоматически."
fi

# Путь обновления старой установки: .env есть, но SECRET_KEY в нём нет.
if ! grep -q '^SECRET_KEY=' .env; then
    echo "SECRET_KEY=$(gen_hex 32)" >> .env
    echo "• В существующий .env добавлен SECRET_KEY (раньше использовался небезопасный дефолт)."
fi

[ "${1:-}" = "--env-only" ] && { echo "Готово (--env-only): стек не запускался."; exit 0; }

# ── 3. Сборка и запуск ───────────────────────────────────────────────────────
echo "• Сборка и запуск контейнеров..."
docker compose up -d --build

# ── 4. Ожидание готовности API ───────────────────────────────────────────────
echo -n "• Ожидание готовности API"
ok=0
for _ in $(seq 1 45); do
    if docker compose exec -T api curl -sf http://localhost:8000/health >/dev/null 2>&1; then
        ok=1; break
    fi
    echo -n "."; sleep 2
done
echo
if [ "$ok" != "1" ]; then
    echo "ОШИБКА: API не поднялся за 90 секунд. Логи: docker compose logs api"
    exit 1
fi

# ── 5. Итог ──────────────────────────────────────────────────────────────────
IP=$(hostname -I 2>/dev/null | awk '{print $1}')
echo "════════════════════════════════════════════════════════════"
echo "  Установка завершена."
echo "  Панель:  http://${IP:-<IP-сервера>}/"
echo "  Вход:    admin / admin123"
echo ""
echo "  ⚠ ПЕРВЫМ ДЕЛОМ смените пароль администратора:"
echo "     Настройки → Пользователи → admin → Сменить пароль"
echo "════════════════════════════════════════════════════════════"
