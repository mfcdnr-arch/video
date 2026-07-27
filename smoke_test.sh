#!/bin/bash
# smoke_test.sh — быстрый прогон ключевых сценариев ПЕРЕД каждым релизом.
# Запускать на сервере после `install_server.sh` / обновления.
# Проверяет: все контейнеры живы, вход, здоровье API, полный цикл
# «экран → медиа → плейлист → расписание», отчёты, биллинг, уведомления.
# Все тестовые данные создаются с префиксом SMOKE_ и удаляются в конце.
#
# Использование:   bash smoke_test.sh
# Переменные:      ADMIN_USER (по умолч. admin), ADMIN_PASS (admin123)
#
# Код возврата 0 — все проверки прошли; иначе число упавших проверок.
set -uo pipefail

ADMIN_USER="${ADMIN_USER:-admin}"
ADMIN_PASS="${ADMIN_PASS:-admin123}"
API="http://localhost:8000"           # внутри контейнера ds_api
DC="docker exec ds_api"               # выполняем запросы изнутри api-контейнера

PASS=0; FAIL=0
ok(){   echo "  ✓ $1"; PASS=$((PASS+1)); }
bad(){  echo "  ✗ $1"; FAIL=$((FAIL+1)); }
step(){ echo; echo "▶ $1"; }

# curl изнутри ds_api; печатает тело, код в глобальную HTTP
req(){ # req METHOD PATH [extra curl args...]
  local m="$1" p="$2"; shift 2
  RESP=$($DC curl -s -w $'\n%{http_code}' -X "$m" "$API$p" -H "Authorization: Bearer $TOKEN" "$@" 2>/dev/null)
  HTTP="${RESP##*$'\n'}"; BODY="${RESP%$'\n'*}"
}

echo "════════════════════════════════════════════"
echo " SMOKE TEST — Digital Signage $(date '+%Y-%m-%d %H:%M')"
echo "════════════════════════════════════════════"

# ── 1. Контейнеры ────────────────────────────────────────────────────────────
step "Контейнеры"
for c in ds_postgres ds_redis ds_minio ds_api ds_celery ds_nginx ds_ntp; do
  if docker ps --format '{{.Names}}' | grep -qx "$c"; then ok "$c запущен"; else bad "$c НЕ запущен"; fi
done

# ── 2. Здоровье API ──────────────────────────────────────────────────────────
step "API /health"
if $DC curl -sf "$API/health" >/dev/null 2>&1; then ok "/health отвечает"; else bad "/health не отвечает"; fi

# ── 3. Вход ──────────────────────────────────────────────────────────────────
step "Аутентификация"
TOKEN=$($DC curl -s -X POST "$API/token" -d "username=$ADMIN_USER&password=$ADMIN_PASS" \
        -H "Content-Type: application/x-www-form-urlencoded" 2>/dev/null \
        | python3 -c "import sys,json;print(json.load(sys.stdin).get('access_token',''))" 2>/dev/null)
if [ -n "$TOKEN" ]; then ok "вход $ADMIN_USER — токен получен"; else
  bad "вход не удался"
  echo "Дальнейшие проверки невозможны без токена."
  echo "Если пароль admin был сменён (боевой сервер), передайте его так:"
  echo '  read -rsp "Пароль admin: " ADMIN_PASS; echo; ADMIN_PASS="$ADMIN_PASS" bash smoke_test.sh; unset ADMIN_PASS'
  exit $((FAIL))
fi

# ── 4. Полный цикл: экран → медиа → плейлист → расписание ────────────────────
step "Основной цикл эфира"
STAMP=$(date +%s)
# экран
req POST "/minipc/register?name=SMOKE_scr_$STAMP"
SID=$(echo "$BODY" | python3 -c "import sys,json;print(json.load(sys.stdin).get('id',''))" 2>/dev/null)
[ -n "$SID" ] && ok "экран создан (id=$SID)" || bad "экран не создан"
# медиа (генерим тестовый ролик ffmpeg внутри контейнера)
$DC sh -c "ffmpeg -f lavfi -i testsrc=duration=1:size=160x120:rate=5 -y /tmp/smoke.mp4 -loglevel quiet" 2>/dev/null
req POST "/media/upload?title=SMOKE_media_$STAMP&category=service" -F "file=@/tmp/smoke.mp4"
MID=$(echo "$BODY" | python3 -c "import sys,json;print(json.load(sys.stdin).get('id',''))" 2>/dev/null)
[ -n "$MID" ] && ok "медиа загружено (id=$MID)" || bad "загрузка медиа: HTTP $HTTP"
# плейлист
req POST "/playlists?name=SMOKE_pl_$STAMP"
PID=$(echo "$BODY" | python3 -c "import sys,json;print(json.load(sys.stdin).get('id',''))" 2>/dev/null)
[ -n "$PID" ] && ok "плейлист создан (id=$PID)" || bad "плейлист не создан"
# ролик в плейлист
if [ -n "$PID" ] && [ -n "$MID" ]; then
  req POST "/playlists/$PID/items?media_id=$MID"
  [ "$HTTP" = "200" ] && ok "ролик добавлен в плейлист" || bad "добавление в плейлист: HTTP $HTTP"
fi
# слот расписания (экран, Пн 10:00)
if [ -n "$SID" ] && [ -n "$PID" ]; then
  req POST "/schedule?screen_id=$SID&day_of_week=0&hour=10&playlist_id=$PID"
  [ "$HTTP" = "200" ] && ok "слот расписания назначен" || bad "назначение слота: HTTP $HTTP"
fi

# ── 5. Отчёты / биллинг / уведомления доступны ───────────────────────────────
step "Модули доступны"
req GET "/dashboard";              [ "$HTTP" = "200" ] && ok "дашборд" || bad "дашборд: HTTP $HTTP"
req GET "/reports/inventory";      [ "$HTTP" = "200" ] && ok "отчёт занятости" || bad "отчёт: HTTP $HTTP"
req GET "/billing/invoices";       [ "$HTTP" = "200" ] && ok "биллинг" || bad "биллинг: HTTP $HTTP"
req GET "/campaigns";              [ "$HTTP" = "200" ] && ok "кампании" || bad "кампании: HTTP $HTTP"
req GET "/moderation/pending";     [ "$HTTP" = "200" ] && ok "модерация" || bad "модерация: HTTP $HTTP"
req GET "/notifications/settings"; [ "$HTTP" = "200" ] && ok "настройки уведомлений" || bad "уведомления: HTTP $HTTP"
req GET "/system/selfcheck";       [ "$HTTP" = "200" ] && ok "самодиагностика" || bad "самодиагностика: HTTP $HTTP"
req GET "/media/fillers";          [ "$HTTP" = "200" ] && ok "папка заглушек" || bad "заглушки: HTTP $HTTP"
req GET "/media/common";           [ "$HTTP" = "200" ] && ok "общая медиатека" || bad "общая медиатека: HTTP $HTTP"
req GET "/media/folders-all";      [ "$HTTP" = "200" ] && ok "список папок" || bad "папки: HTTP $HTTP"
req GET "/reports/fillers";        [ "$HTTP" = "200" ] && ok "отчёт по заглушкам" || bad "отчёт заглушек: HTTP $HTTP"
[ -n "${SID:-}" ] && { req GET "/minipc/$SID/diagnostics"; [ "$HTTP" = "200" ] && ok "диагностика экрана (список)" || bad "диагностика: HTTP $HTTP"; }

# ── 6. Уборка тестовых данных ────────────────────────────────────────────────
step "Уборка"
[ -n "${PID:-}" ] && req DELETE "/playlists/$PID" && ok "плейлист удалён"
[ -n "${MID:-}" ] && req DELETE "/media/$MID"      && ok "медиа удалено"
[ -n "${SID:-}" ] && req DELETE "/minipc/$SID"     && ok "экран удалён"
$DC rm -f /tmp/smoke.mp4 2>/dev/null

# ── Итог ─────────────────────────────────────────────────────────────────────
echo
echo "════════════════════════════════════════════"
echo " ИТОГ: пройдено $PASS, провалено $FAIL"
echo "════════════════════════════════════════════"
[ "$FAIL" -eq 0 ] && echo "✅ Все проверки прошли — релиз можно выпускать." \
                  || echo "❌ Есть провалы — НЕ выпускать до устранения."
exit "$FAIL"
