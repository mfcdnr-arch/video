#!/bin/bash
# dev_stand.sh — локальный стенд разработки (НЕ для боевого сервера).
#
# Использование:  bash dev_stand.sh up      — собрать и поднять стенд
#                 bash dev_stand.sh down    — остановить (данные сохраняются)
#                 bash dev_stand.sh reset   — остановить, СТЕРЕТЬ тома и поднять заново
#
# Стенд — это обычный стек docker-compose.yml с надстройкой
# docker-compose.dev.yml под отдельным именем проекта dsdev (свои тома).
# Панель: http://localhost:8088 (только loopback), вход admin / пароль из .dev_admin_pass.
#
# Пароль стенда — сид-значение из миграции 001; принудительная смена пароля
# при первом входе на стенде снимается. Это допустимо ТОЛЬКО локально:
# install_server.sh на сервере генерирует случайный пароль admin.
#
# Контейнеры называются так же, как на сервере (ds_api, ds_postgres, …), —
# поэтому smoke_test.sh и migrate.sh работают со стендом без правок. Боевой
# стек и стенд на одной машине одновременно не поднять.
set -uo pipefail
cd "$(dirname "$0")"

# migrate.sh и прочие скрипты зовут `docker compose` без -p/-f — через эти
# переменные они попадают в стек стенда, а не в проект по имени каталога.
export COMPOSE_PROJECT_NAME=dsdev
export COMPOSE_FILE=docker-compose.yml:docker-compose.dev.yml
DC=(docker compose -p dsdev -f docker-compose.yml -f docker-compose.dev.yml)
PORT=8088
DEV_ADMIN_PASS=admin123          # сид из server/migrations/001_init_schema.sql

die(){ echo "ОСТАНОВ: $1"; exit 1; }

up(){
    bash install_server.sh --env-only >/dev/null || die "install_server.sh --env-only не создал .env"
    set -a; . ./.env; set +a
    : "${POSTGRES_USER:=display_user}"; : "${POSTGRES_DB:=display_system}"

    echo "▶ Сборка и запуск стенда (проект dsdev)…"
    "${DC[@]}" up -d --build || die "docker compose up не удался"

    # По TCP, а не через сокет: пока идут скрипты initdb, временный сервер
    # Postgres слушает только сокет — migrate.sh не должен влезть в их работу.
    echo "▶ Ожидание PostgreSQL…"
    for _ in $(seq 1 90); do
        "${DC[@]}" exec -T postgres pg_isready -h 127.0.0.1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
            >/dev/null 2>&1 && break
        sleep 2
    done
    "${DC[@]}" exec -T postgres pg_isready -h 127.0.0.1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
        >/dev/null 2>&1 || die "PostgreSQL не поднялась за 3 минуты (docker compose -p dsdev logs postgres)"

    echo "▶ Миграции…"
    bash migrate.sh || die "migrate.sh завершился с ошибкой"

    "${DC[@]}" exec -T postgres psql -q -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
        -c "UPDATE users SET must_change_password = FALSE WHERE username = 'admin'" >/dev/null \
        || die "не удалось снять принудительную смену пароля admin"
    ( umask 077; printf '%s\n' "$DEV_ADMIN_PASS" > .dev_admin_pass )

    echo "▶ Ожидание панели на localhost:$PORT…"
    for _ in $(seq 1 60); do
        curl -sf "http://localhost:$PORT/health" >/dev/null 2>&1 && break
        sleep 2
    done
    if curl -sf "http://localhost:$PORT/health" >/dev/null 2>&1; then
        echo "✅ Стенд работает: http://localhost:$PORT  (вход: admin, пароль — в .dev_admin_pass)"
        echo "   Smoke: SMOKE_ALLOW_BROADCAST=1 ADMIN_PASS=\$(cat .dev_admin_pass) bash smoke_test.sh"
        echo "          (SMOKE_ALLOW_BROADCAST=1 включает шаг «Общий эфир» — он идёт на ВСЕ экраны, только на стенде)"
        echo "   E2E:   E2E_ALLOW_BROADCAST=1 ADMIN_PASS=\$(cat .dev_admin_pass) DS_URL=http://nginx bash ui_test.sh"
        echo "          (E2E_ALLOW_BROADCAST=1 — то же для шага «Эфир сети: включить общий эфир»)"
    else
        die "панель не отвечает на localhost:$PORT/health (docker compose -p dsdev logs api nginx)"
    fi
}

down(){ "${DC[@]}" down; }

case "${1:-}" in
    up)    up ;;
    down)  down ;;
    reset) "${DC[@]}" down -v && up ;;
    *)     echo "Использование: bash dev_stand.sh up | down | reset"; exit 2 ;;
esac
