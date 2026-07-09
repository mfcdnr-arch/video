#!/bin/bash
# run_tests.sh — воспроизводимый запуск регрессионных тестов (pytest).
#
# pytest не входит в production-образ (он там не нужен) и после пересборки
# образа отсутствует. Этот скрипт одной командой ставит dev-зависимости в
# работающий контейнер ds_api и прогоняет тесты — запускать можно после любого
# обновления/пересборки.
#
# Использование:  bash run_tests.sh
set -uo pipefail
cd "$(dirname "$0")"

if ! docker compose ps --format '{{.Name}}' 2>/dev/null | grep -qx ds_api; then
    echo "ОШИБКА: контейнер ds_api не запущен."
    echo "      └─ Поднимите стек: bash install_server.sh"
    exit 1
fi

echo "• Установка dev-зависимостей (pytest) в ds_api…"
docker exec ds_api pip install -q -r /app/requirements-dev.txt \
    || { echo "ОШИБКА: не удалось установить pytest в контейнер."; exit 1; }

echo "• Запуск тестов (server/tests)…"
docker exec -w /app ds_api python -m pytest tests -q
