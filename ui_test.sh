#!/bin/bash
# ui_test.sh — браузерный (E2E) прогон админ-панели ПЕРЕД релизом.
#
# Дополняет smoke_test.sh: тот проверяет HTTP-эндпоинты, а этот — сам
# интерфейс. Кнопка может звать неверный метод или падать в JS, при том что
# API зелёный: именно так выглядели почти все дефекты панели, найденные на
# пилоте живым кликом.
#
# Что проверяет: вход, открытие всех 13 разделов меню, нажатие всех
# «безопасных» кнопок каждого раздела (разрушающие не трогаются), полный цикл
# «создать → увидеть → удалить» для экрана и плейлиста, индикатор монитора.
# Любая ошибка в консоли браузера или ответ HTTP ≥400 — провал прогона.
#
# Использование:   bash ui_test.sh
# Переменные:      ADMIN_USER (по умолч. admin), ADMIN_PASS (admin123),
#                  DS_URL (по умолч. http://nginx — адрес панели ИЗНУТРИ
#                  docker-сети стека).
#
# Chromium не ставится на сервер: прогон идёт в одноразовом контейнере
# официального образа puppeteer, подключённом к сети стека. Первый запуск
# скачивает образ (~1 ГБ), дальше он берётся из кеша.
#
# Код возврата 0 — всё чисто; 1 — есть провалы (подробности в выводе).
set -uo pipefail
cd "$(dirname "$0")"

ADMIN_USER="${ADMIN_USER:-admin}"
ADMIN_PASS="${ADMIN_PASS:-admin123}"
DS_URL="${DS_URL:-http://nginx}"
IMAGE="${UI_TEST_IMAGE:-ghcr.io/puppeteer/puppeteer:23.11.1}"

command -v docker >/dev/null 2>&1 || { echo "ОШИБКА: docker не найден."; exit 1; }

# Сеть стека (имя зависит от каталога проекта — определяем по контейнеру nginx).
NET=$(docker inspect -f '{{range $k,$v := .NetworkSettings.Networks}}{{$k}}{{end}}' ds_nginx 2>/dev/null)
if [ -z "$NET" ]; then
    echo "ОШИБКА: контейнер ds_nginx не запущен — панель проверять негде."
    echo "      └─ Поднимите стек: docker compose up -d  (или bash install_server.sh)"
    exit 1
fi

echo "• Прогон панели ($DS_URL, сеть $NET, образ $IMAGE)…"
docker run --rm --network "$NET" \
    -v "$PWD/ui_tests":/ui:ro \
    -e DS_URL="$DS_URL" -e ADMIN_USER="$ADMIN_USER" -e ADMIN_PASS="$ADMIN_PASS" \
    -e NODE_PATH=/home/pptruser/node_modules \
    --entrypoint node "$IMAGE" /ui/ui_test.js
RC=$?

if [ "$RC" -ne 0 ]; then
    echo
    echo "Прогон нашёл проблемы. Как читать вывод:"
    echo "  ✗ строка раздела   — вью не открылось или отдало ошибку;"
    echo "  ✗ строка кнопки    — клик привёл к ошибке в консоли/HTTP ≥400;"
    echo "  блок «Ошибки консоли / HTTP» — где именно (шаг → сообщение)."
fi
exit "$RC"
