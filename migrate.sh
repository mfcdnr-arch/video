#!/bin/bash
# migrate.sh — идемпотентное применение миграций БД к работающему стеку.
#
# Зачем: миграции из списка initdb в docker-compose.yml применяются Postgres
# ТОЛЬКО при первом старте на пустой базе. На уже работающей БД новые миграции
# нужно применять отдельно — этим занимается данный скрипт.
#
# Безопасность повторного запуска: применяются только НЕ применённые ранее
# миграции (учёт в таблице schema_migrations); при этом все миграции проекта
# идемпотентны (CREATE TABLE/ADD COLUMN IF NOT EXISTS, INSERT ... ON CONFLICT
# или WHERE NOT EXISTS), поэтому даже повторное применение не портит данные.
#
# Первый запуск после initdb (baseline): initdb сам в schema_migrations не
# пишет. Если учёта ещё не было, а схема уже есть и экранов в ней нет (свежая
# установка), файлы из списка initdb в docker-compose.yml записываются как
# применённые без повторного запуска. Файлы не из этого списка initdb не
# применял — они идут обычным порядком. Если же экраны уже есть (живая база
# без учёта), baseline не делается: все миграции применяются, как раньше.
#
# Использование:  bash migrate.sh
# Требуется запущенный контейнер ds_postgres (docker compose up -d postgres).
set -uo pipefail
cd "$(dirname "$0")"

# Параметры подключения из .env (генерируется install_server.sh).
set -a; . ./.env 2>/dev/null || true; set +a
: "${POSTGRES_USER:=display_user}"; : "${POSTGRES_DB:=display_system}"

psqldb(){ docker compose exec -T postgres psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" "$@"; }

# 1. Доступна ли БД. По TCP, а не через сокет: пока идут скрипты initdb,
# временный сервер Postgres слушает только сокет — baseline (шаг 3), снятый
# посреди initdb, записал бы ещё не применённые файлы.
if ! docker compose exec -T postgres pg_isready -h 127.0.0.1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" >/dev/null 2>&1; then
    echo "ОШИБКА: PostgreSQL (ds_postgres) недоступна."
    echo "      └─ Запустите стек: docker compose up -d postgres  (и дождитесь healthy)"
    exit 1
fi

# 2. Таблица учёта применённых миграций (была ли она до этого запуска — для шага 3)
had_journal="$(psqldb -tAq -c "SELECT to_regclass('public.schema_migrations') IS NOT NULL" 2>/dev/null | tr -d '[:space:]')"
if ! psqldb -q -c "CREATE TABLE IF NOT EXISTS schema_migrations (
        filename   text        PRIMARY KEY,
        applied_at timestamptz NOT NULL DEFAULT now());" >/dev/null 2>&1; then
    echo "ОШИБКА: не удалось создать таблицу schema_migrations."
    exit 1
fi

shopt -s nullglob
FILES=(server/migrations/*.sql)
if [ ${#FILES[@]} -eq 0 ]; then
    echo "Миграции не найдены в server/migrations/ — нечего применять."
    exit 0
fi

# 3. Baseline: учёт только что создан, а схему уже накатил initdb
if [ "$had_journal" = "f" ]; then
    has_screens="$(psqldb -tAq -c "SELECT to_regclass('public.screens') IS NOT NULL" 2>/dev/null | tr -d '[:space:]')"
    if [ "$has_screens" = "t" ]; then
        n_screens="$(psqldb -tAq -c "SELECT count(*) FROM screens" 2>/dev/null | tr -d '[:space:]')"
        if [ "$n_screens" = "0" ]; then
            based=0
            for f in "${FILES[@]}"; do
                name="$(basename "$f")"
                grep -qF "./server/migrations/$name:" docker-compose.yml || continue
                psqldb -q -c "INSERT INTO schema_migrations(filename) VALUES ('$name') ON CONFLICT DO NOTHING;" >/dev/null 2>&1 \
                    && based=$((based+1))
            done
            echo "Учёт миграций начат: файлы, применённые initdb, записаны без повторного запуска — $based шт."
        elif [ -n "$n_screens" ]; then
            echo "Учёта миграций не было, а экранов в базе $n_screens — применяю все миграции (они идемпотентны)."
        fi
    fi
fi

# 4. Применяем недостающие миграции по порядку
applied=0; skipped=0; failed=0
ERR="$(mktemp)"
for f in "${FILES[@]}"; do
    name="$(basename "$f")"
    done_row="$(psqldb -tAq -c "SELECT 1 FROM schema_migrations WHERE filename = '$name'" 2>/dev/null | tr -d '[:space:]')"
    if [ "$done_row" = "1" ]; then
        skipped=$((skipped+1)); continue
    fi
    printf '  → %-40s ' "$name"
    if psqldb -q < "$f" >/dev/null 2>"$ERR"; then
        psqldb -q -c "INSERT INTO schema_migrations(filename) VALUES ('$name') ON CONFLICT DO NOTHING;" >/dev/null 2>&1
        echo "применена"; applied=$((applied+1))
    else
        echo "ОШИБКА"; sed 's/^/        /' "$ERR"; failed=$((failed+1))
    fi
done
rm -f "$ERR"

echo "──────────────────────────────────────────"
echo "Миграции: применено $applied, уже было $skipped, ошибок $failed"
[ "$failed" -eq 0 ] || { echo "❌ Есть ошибки — разберите вывод выше."; exit 1; }
echo "✅ База данных в актуальном состоянии."
