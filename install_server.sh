#!/bin/bash
# install_server.sh — первичная установка / обновление сервера Digital Signage
# с самодиагностикой хода установки.
#
# Использование:  bash install_server.sh            (полная установка)
#                 bash install_server.sh --env-only (только создать .env, без запуска)
#
# Что делает:
#   0. PREFLIGHT: проверяет окружение ДО любых изменений (Docker, Compose, демон,
#      место на диске, наличие файлов проекта) и падает с понятной причиной.
#   1. Создаёт .env с автогенерированными секретами (не перезаписывает существующий).
#   2. Собирает и запускает стек; при ошибке сборки/запуска — диагностирует причину.
#   3. SELF-TEST: проверяет, что поднялся КАЖДЫЙ из 7 контейнеров, доступна БД с
#      применёнными миграциями, и панель отдаётся через nginx. Печатает итог PASS/FAIL.
#
# Весь вывод дублируется в install_server.log (рядом со скриптом).
# После установки ОБЯЗАТЕЛЬНО смените пароль admin (панель форсит это при первом входе).
set -uo pipefail
cd "$(dirname "$0")"

LOG="./install_server.log"
exec > >(tee -a "$LOG") 2>&1
echo "════════ install_server.sh — $(date '+%Y-%m-%d %H:%M:%S') ════════"

# ── Хелперы вывода/диагностики ───────────────────────────────────────────────
PASS=0; FAIL=0; WARN=0
pass(){ echo "  ✓ $1"; PASS=$((PASS+1)); }
bad(){  echo "  ✗ $1"; FAIL=$((FAIL+1)); }
warn(){ echo "  ⚠ $1"; WARN=$((WARN+1)); }
hint(){ echo "      └─ $1"; }
die(){  echo; echo "ОСТАНОВ: $1"; [ -n "${2:-}" ] && hint "$2"; echo "Полный лог: $LOG"; exit 1; }

EXPECTED_CONTAINERS="ds_postgres ds_redis ds_minio ds_api ds_celery ds_nginx ds_ntp"

# ══════════════════════════════════════════════════════════════════════════════
# 0. PREFLIGHT — проверки ДО любых изменений
# ══════════════════════════════════════════════════════════════════════════════
echo; echo "▶ [0/3] Preflight — проверка окружения"

command -v docker >/dev/null 2>&1 \
    || die "Docker не установлен." "Установите: https://docs.docker.com/engine/install/"
pass "Docker найден ($(docker --version 2>/dev/null | awk '{print $3}' | tr -d ,))"

docker info >/dev/null 2>&1 \
    || die "Docker-демон не запущен или нет прав." "Запустите Docker (sudo systemctl start docker) или добавьте пользователя в группу docker."
pass "Docker-демон отвечает"

docker compose version >/dev/null 2>&1 \
    || die "Docker Compose v2 не найден (нужна команда 'docker compose')." "Обновите Docker до версии с Compose v2."
pass "Docker Compose v2 найден"

for f in docker-compose.yml server/migrations/001_init_schema.sql; do
    [ -f "$f" ] || die "Не найден обязательный файл: $f" "Запускайте скрипт из каталога проекта, скопированного полностью."
done
pass "Файлы проекта на месте"

# Свободное место (предупреждение, не блокер): образы+тома требуют места.
FREE_KB=$(df -Pk . 2>/dev/null | awk 'NR==2{print $4}')
if [ -n "${FREE_KB:-}" ] && [ "$FREE_KB" -lt 2097152 ]; then
    warn "Мало места на диске (<2 ГБ свободно) — сборке образов и БД может не хватить."
    hint "Освободите место: docker image prune -f; docker builder prune -f"
else
    pass "Места на диске достаточно"
fi

# Часы хоста. Сбитое время — самая обидная причина провала установки: apt
# внутри сборки образа отвергает подписи репозиториев («Release file is not
# yet valid»), и сообщение об ошибке никак не намекает на время. Плюс кривые
# часы искажают расписание и отчёты. Часы НЕ переводим сами (это дело
# администратора), а называем проблему и даём команду.
NOW_TS=$(date +%s)
HTTP_DATE=""
for URL in https://registry-1.docker.io/v2/ https://deb.debian.org https://ya.ru; do
    HTTP_DATE=$(curl -sSI -m 5 "$URL" 2>/dev/null | awk 'tolower($1)=="date:"{sub(/^[^ ]+ /,""); sub(/\r$/,""); print; exit}')
    [ -n "$HTTP_DATE" ] && break
done
REMOTE_TS=""
[ -n "$HTTP_DATE" ] && REMOTE_TS=$(date -d "$HTTP_DATE" +%s 2>/dev/null)
if [ -n "$REMOTE_TS" ]; then
    DRIFT=$((NOW_TS - REMOTE_TS)); ABS_DRIFT=${DRIFT#-}
    if [ "$ABS_DRIFT" -ge 60 ]; then
        [ "$DRIFT" -lt 0 ] && DIR="отстают" || DIR="спешат"
        warn "Часы хоста $DIR на ${ABS_DRIFT} с — сборка образов может упасть на проверке подписей apt."
        hint "Выставить сейчас: sudo date -s \"$(date -d "@$REMOTE_TS" '+%Y-%m-%d %H:%M:%S')\""
        hint "И включить синхронизацию: sudo apt install chrony (см. 01_УСТАНОВКА_СЕРВЕР, «Часы хоста»)"
    else
        pass "Часы хоста верны (расхождение ${ABS_DRIFT} с)"
    fi
else
    # Без сети сверить не с чем — остаётся грубая проверка «время не в прошлом»:
    # файлы проекта не могут быть новее текущего момента.
    NEWEST_TS=$(find . -type f -not -path './.git/*' -printf '%T@\n' 2>/dev/null | sort -n | tail -1 | cut -d. -f1)
    if [ -n "${NEWEST_TS:-}" ] && [ "$NOW_TS" -lt "$((NEWEST_TS - 86400))" ]; then
        warn "Часы хоста отстают: системное время ($(date '+%Y-%m-%d %H:%M')) раньше даты файлов проекта."
        hint "Выставьте верное время (sudo date -s \"ГГГГ-ММ-ДД ЧЧ:ММ:СС\") и повторите установку."
    else
        pass "Часы хоста: сверить с эталоном не удалось (нет сети), грубая проверка пройдена"
    fi
fi

# ══════════════════════════════════════════════════════════════════════════════
# 1. Секреты (.env)
# ══════════════════════════════════════════════════════════════════════════════
echo; echo "▶ [1/3] Секреты (.env)"
gen_hex(){ openssl rand -hex "$1" 2>/dev/null || head -c "$1" /dev/urandom | od -An -tx1 | tr -d ' \n'; }

FRESH_INSTALL=0
if [ -f .env ]; then
    pass ".env уже существует — пароли и ключи НЕ перезаписываются"
else
    FRESH_INSTALL=1
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
    pass "Создан .env: пароль БД, пароль MinIO и SECRET_KEY сгенерированы"
fi
if ! grep -q '^SECRET_KEY=' .env; then
    echo "SECRET_KEY=$(gen_hex 32)" >> .env
    warn "В существующий .env добавлен SECRET_KEY (раньше был небезопасный дефолт)"
fi

# Значения для последующих проверок БД.
set -a; . ./.env 2>/dev/null || true; set +a
: "${POSTGRES_USER:=display_user}"; : "${POSTGRES_DB:=display_system}"

[ "${1:-}" = "--env-only" ] && { echo; echo "Готово (--env-only): стек не запускался."; exit 0; }

# ══════════════════════════════════════════════════════════════════════════════
# 2. Сборка и запуск (с диагностикой ошибки)
# ══════════════════════════════════════════════════════════════════════════════
echo; echo "▶ [2/3] Сборка и запуск контейнеров"
BUILD_LOG="$(mktemp)"
docker compose up -d --build 2>&1 | tee "$BUILD_LOG"
RC=${PIPESTATUS[0]}
if [ "$RC" -ne 0 ]; then
    echo
    echo "Запуск не удался. Вероятная причина:"
    if grep -qiE 'port is already allocated|address already in use|bind.*:80' "$BUILD_LOG"; then
        hint "Порт 80 занят другим процессом. Найдите его: 'sudo lsof -i :80' (или 'ss -ltnp sport = :80') и остановите."
    elif grep -qiE 'no space left' "$BUILD_LOG"; then
        hint "Кончилось место на диске. Освободите: docker image prune -f; docker builder prune -f"
    elif grep -qiE 'permission denied|got permission denied.*docker.sock' "$BUILD_LOG"; then
        hint "Нет прав на Docker. Запустите через sudo или добавьте пользователя в группу docker."
    elif grep -qiE 'failed to solve|error building|dockerfile' "$BUILD_LOG"; then
        hint "Ошибка сборки образа. Смотрите вывод выше и Dockerfile в server/."
    else
        hint "Смотрите вывод выше и 'docker compose logs'."
    fi
    rm -f "$BUILD_LOG"; die "docker compose up завершился с ошибкой (код $RC)."
fi
rm -f "$BUILD_LOG"
pass "docker compose up отработал"

# ── Применение миграций БД (идемпотентно, безопасно при обновлении) ───────────
# На пустой БД миграции уже накатил initdb; на существующей — migrate.sh
# добьёт недостающие. Повторный запуск безопасен (учёт в schema_migrations).
echo; echo "▶ Миграции БД"
echo -n "  … ожидание готовности PostgreSQL"
for _ in $(seq 1 30); do
    docker compose exec -T postgres pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB" >/dev/null 2>&1 && break
    echo -n "."; sleep 2
done
echo
if bash migrate.sh; then
    pass "миграции БД в актуальном состоянии"
else
    bad "ошибка применения миграций БД"; hint "Разберите вывод выше; повторить вручную: bash migrate.sh"
fi

# ── Стартовый пароль администратора (только при ПЕРВОЙ установке) ─────────────
# Чтобы не оставлять предсказуемый admin123: генерируем случайный пароль и
# показываем его один раз. Обязательная смена при первом входе сохраняется.
ADMIN_PW=""
if [ "$FRESH_INSTALL" = "1" ]; then
    ADMIN_PW="$(docker compose exec -T api python /app/reset_admin_password.py 2>/dev/null | tr -d '\r\n')"
    if [ -n "$ADMIN_PW" ]; then
        pass "сгенерирован случайный стартовый пароль администратора"
    else
        warn "не удалось сгенерировать пароль admin — остаётся admin123 (смените в панели при первом входе)"
    fi
fi

# ══════════════════════════════════════════════════════════════════════════════
# 3. SELF-TEST — проверка результата установки
# ══════════════════════════════════════════════════════════════════════════════
echo; echo "▶ [3/3] Самопроверка установки"

# 3.1 Готовность API (до 90 c)
echo -n "  … ожидание готовности API"
api_ok=0
for _ in $(seq 1 45); do
    if docker compose exec -T api curl -sf http://localhost:8000/health >/dev/null 2>&1; then api_ok=1; break; fi
    echo -n "."; sleep 2
done
echo
if [ "$api_ok" = 1 ]; then pass "API /health отвечает"
else bad "API не поднялся за 90 c"; hint "Логи: docker compose logs api"; fi

# 3.2 Каждый контейнер запущен
for c in $EXPECTED_CONTAINERS; do
    state=$(docker inspect -f '{{.State.Running}}' "$c" 2>/dev/null || echo "нет")
    if [ "$state" = "true" ]; then
        pass "контейнер $c запущен"
    else
        bad "контейнер $c НЕ запущен (состояние: $state)"
        hint "Диагностика: docker compose logs --tail=50 ${c#ds_}"
    fi
done

# 3.3 БД доступна и миграции применены
if docker compose exec -T postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc "SELECT 1" >/dev/null 2>&1; then
    pass "PostgreSQL доступна"
    core=$(docker compose exec -T postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc "SELECT to_regclass('public.users')" 2>/dev/null | tr -d '[:space:]')
    [ "$core" = "users" ] && pass "базовая схема применена (таблица users)" \
                          || bad "нет таблицы users — миграция 001 не применилась"
    recent=$(docker compose exec -T postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc "SELECT to_regclass('public.campaigns')" 2>/dev/null | tr -d '[:space:]')
    if [ "$recent" = "campaigns" ]; then pass "поздние миграции применены (таблица campaigns)"
    else warn "нет таблицы campaigns — на существующей БД примените новые миграции вручную"
         hint "См. docs/Инструкции/08_ОБНОВЛЕНИЕ_СЕРВЕРА.md (миграции на живой БД)"; fi
else
    bad "PostgreSQL недоступна"; hint "Логи: docker compose logs postgres"
fi

# 3.4 Панель отдаётся через nginx (порт 80 на хосте)
code=$(curl -s -o /dev/null -w '%{http_code}' http://localhost/ 2>/dev/null || echo 000)
[ "$code" = 200 ] && pass "панель отдаётся nginx (HTTP 200)" \
                   || { bad "панель не отвечает (HTTP $code)"; hint "Логи: docker compose logs nginx"; }

# ── Итог ──────────────────────────────────────────────────────────────────────
IP=$(hostname -I 2>/dev/null | awk '{print $1}')
echo
echo "════════════════════════════════════════════════════════════"
if [ "$FAIL" -eq 0 ]; then
    echo "  ✅ Установка завершена. Проверок пройдено: $PASS, предупреждений: $WARN."
    echo "  Панель:  http://${IP:-<IP-сервера>}/"
    if [ -n "$ADMIN_PW" ]; then
        echo "  Вход:    admin / $ADMIN_PW"
        echo "  ⚠ Пароль показан ОДИН раз — запишите его. При первом входе панель"
        echo "     потребует задать новый пароль."
    else
        echo "  Вход:    admin / admin123  (панель попросит сменить пароль при первом входе)"
    fi
else
    echo "  ❌ Установка завершилась с ошибками: провалено $FAIL, пройдено $PASS, предупреждений $WARN."
    echo "  Разберите ✗ выше по подсказкам. Полный лог: $LOG"
fi
echo "════════════════════════════════════════════════════════════"
[ "$FAIL" -eq 0 ] && exit 0 || exit 1
