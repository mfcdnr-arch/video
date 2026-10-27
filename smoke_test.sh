#!/bin/bash
# smoke_test.sh — быстрый прогон ключевых сценариев ПЕРЕД каждым релизом.
# Запускать на сервере после `install_server.sh` / обновления.
# Проверяет: все контейнеры живы, вход, здоровье API, полный цикл
# «экран → медиа → плейлист → расписание», показ по датам, отчёты, биллинг,
# уведомления.
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

# python-проверки над JSON-телом ответа ($BODY, объект/список = r): код возврата 0 — условие выполнено
body_has(){ echo "$BODY" | python3 -c "import sys,json; r=json.load(sys.stdin); sys.exit(0 if ($1) else 1)" 2>/dev/null; }
# поле верхнего уровня JSON-тела (пусто, если нет)
body_get(){ echo "$BODY" | python3 -c "import sys,json;print(json.load(sys.stdin).get('$1',''))" 2>/dev/null; }

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

# Плейлист одним запросом: проверка, создание, замена (окно сборки плейлиста).
# MID — одобренный ролик (service), MID3 — второй одобренный, MID2 — «на модерации».
if [ -n "${MID:-}" ] && [ -n "${PID:-}" ]; then
  JSON_H='Content-Type: application/json'
  req POST "/media/upload?title=SMOKE_media2_$STAMP&category=service" -F "file=@/tmp/smoke.mp4"
  MID3=$(body_get id)
  [ -n "$MID3" ] && ok "второй одобренный ролик загружен (id=$MID3)" || bad "второй ролик: HTTP $HTTP"
  req POST "/media/upload?title=SMOKE_pend_$STAMP&category=other&age_rating=0%2B" -F "file=@/tmp/smoke.mp4"
  MID2=$(body_get id)
  if [ -n "$MID2" ] && body_has "r['review_status']=='pending'"; then ok "ролик на модерации загружен (id=$MID2)"; else bad "ролик на модерации: HTTP $HTTP"; fi

  # длительность тестового ролика — как её видит сервер (из плейлиста PID)
  req GET "/playlists/$PID/items"
  DUR=$(echo "$BODY" | python3 -c "import sys,json;print(json.load(sys.stdin)[0]['duration_seconds'])" 2>/dev/null)
  DUR="${DUR:-0}"

  # проверка: ролик ×4 — 4 показа, итог 4×длительность и одна подсказка про повторы подряд
  req POST "/playlists/check" -H "$JSON_H" -d "{\"items\": [{\"media_id\": $MID, \"repeat_count\": 4}]}"
  if [ "$HTTP" = "200" ] && body_has "r['plays']==4 and abs(r['total_seconds']-4*$DUR)<0.1 and len(r['warnings'])==1 and 'SMOKE_media_$STAMP' in r['warnings'][0] and '4' in r['warnings'][0]"; then
    ok "проверка плейлиста: ролик ×4 — 4 показа и подсказка про повторы"; else bad "проверка плейлиста ×4: HTTP $HTTP"; fi
  req POST "/playlists/check" -H "$JSON_H" -d "{\"items\": [{\"media_id\": $MID, \"repeat_count\": 1}]}"
  if [ "$HTTP" = "200" ] && body_has "r['warnings']==[] and r['plays']==1 and abs(r['total_seconds']-$DUR)<0.1"; then
    ok "проверка плейлиста: один ролик ×1 — подсказок нет"; else bad "проверка плейлиста ×1: HTTP $HTTP"; fi
  req POST "/playlists/check" -H "$JSON_H" -d '{"items": []}'
  if [ "$HTTP" = "200" ] && body_has "r=={'warnings': [], 'total_seconds': 0, 'plays': 0}"; then
    ok "проверка пустого списка: нули и без подсказок"; else bad "проверка пустого списка: HTTP $HTTP"; fi
  req POST "/playlists/check" -H "$JSON_H" -d "{\"items\": [{\"media_id\": $MID, \"repeat_count\": 100}]}"
  [ "$HTTP" = "400" ] && ok "проверка: повторов 100 → 400" || bad "проверка: повторов 100: HTTP $HTTP (ждали 400)"

  # создание одним запросом: название + ролик ×2
  req POST "/playlists/full" -H "$JSON_H" -d "{\"name\": \"SMOKE_full_$STAMP\", \"items\": [{\"media_id\": $MID, \"repeat_count\": 2}]}"
  PID2=$(body_get id)
  if [ "$HTTP" = "200" ] && [ -n "$PID2" ] && body_has "r['name']=='SMOKE_full_$STAMP' and r['warnings']==[]"; then
    ok "плейлист создан одним запросом (id=$PID2)"; else bad "создание плейлиста одним запросом: HTTP $HTTP"; PID2=""; fi
  if [ -n "$PID2" ]; then
    req GET "/playlists/$PID2/items"
    if [ "$HTTP" = "200" ] && body_has "len(r)==1 and r[0]['media_id']==$MID and r[0]['position']==1 and r[0]['repeat_count']==2"; then
      ok "в новом плейлисте ролик ×2 на позиции 1"; else bad "состав нового плейлиста: HTTP $HTTP"; fi
    req GET "/playlists"
    if [ "$HTTP" = "200" ] && body_has "any(x['id']==$PID2 and x['name']=='SMOKE_full_$STAMP' for x in r)"; then
      ok "новый плейлист виден в списке"; else bad "нового плейлиста нет в списке: HTTP $HTTP"; fi
  fi

  # ошибки при создании: плейлист не появляется вовсе
  if [ -n "${MID2:-}" ]; then
    req POST "/playlists/full" -H "$JSON_H" -d "{\"name\": \"SMOKE_fullbad_$STAMP\", \"items\": [{\"media_id\": $MID}, {\"media_id\": $MID2}]}"
    if [ "$HTTP" = "409" ] && body_has "'SMOKE_pend_$STAMP' in r['detail']"; then
      ok "создание с роликом на модерации → 409 с его названием"; else bad "создание с неодобренным: HTTP $HTTP (ждали 409)"; fi
  fi
  req POST "/playlists/full" -H "$JSON_H" -d "{\"name\": \"SMOKE_fullbad_$STAMP\", \"items\": [{\"media_id\": $MID}, {\"media_id\": 2147483000}]}"
  [ "$HTTP" = "404" ] && ok "создание с несуществующим роликом → 404" || bad "создание с несуществующим роликом: HTTP $HTTP (ждали 404)"
  req POST "/playlists/full" -H "$JSON_H" -d "{\"name\": \"  \", \"items\": [{\"media_id\": $MID}]}"
  [ "$HTTP" = "400" ] && ok "создание с пустым названием → 400" || bad "создание с пустым названием: HTTP $HTTP (ждали 400)"
  req GET "/playlists"
  if [ "$HTTP" = "200" ] && ! body_has "any(x['name']=='SMOKE_fullbad_$STAMP' or x['name'].strip()=='' for x in r)"; then
    ok "после ошибок создания плейлист не появился"; else bad "после ошибок создания остался плейлист"; fi

  # замена роликов: другой порядок, два ролика, новое название
  if [ -n "$PID2" ] && [ -n "${MID3:-}" ]; then
    req PUT "/playlists/$PID2/items" -H "$JSON_H" -d "{\"name\": \"SMOKE_put_$STAMP\", \"items\": [{\"media_id\": $MID3}, {\"media_id\": $MID, \"repeat_count\": 3}]}"
    # оба ролика — один и тот же файл: подсказки про долю («×3» — три четверти плейлиста) и про дубль файла
    if [ "$HTTP" = "200" ] && body_has "r['status']=='ok' and len(r['warnings'])==2 and 'SMOKE_media_$STAMP' in r['warnings'][0] and 'SMOKE_media2_$STAMP' in r['warnings'][1] and 'SMOKE_media_$STAMP' in r['warnings'][1]"; then
      ok "ролики плейлиста заменены, в ответе подсказки про долю и дубль файла"; else bad "замена роликов: HTTP $HTTP"; fi
    req GET "/playlists/$PID2/items"
    if [ "$HTTP" = "200" ] && body_has "[(x['media_id'], x['position'], x['repeat_count']) for x in r]==[($MID3,1,1),($MID,2,3)]"; then
      ok "после замены: новый порядок, позиции 1–2, повторы ×1 и ×3"; else bad "состав после замены: HTTP $HTTP"; fi
    req GET "/playlists"
    if [ "$HTTP" = "200" ] && body_has "any(x['id']==$PID2 and x['name']=='SMOKE_put_$STAMP' for x in r)"; then
      ok "плейлист переименован при замене"; else bad "переименования при замене нет: HTTP $HTTP"; fi

    # замена без name название не трогает; повторы ×5 → подсказка в ответе
    req PUT "/playlists/$PID2/items" -H "$JSON_H" -d "{\"items\": [{\"media_id\": $MID, \"repeat_count\": 5}]}"
    if [ "$HTTP" = "200" ] && body_has "r['status']=='ok' and len(r['warnings'])==1"; then
      ok "замена без названия: подсказка про ×5 в ответе"; else bad "замена без названия: HTTP $HTTP"; fi
    req GET "/playlists"
    if [ "$HTTP" = "200" ] && body_has "any(x['id']==$PID2 and x['name']=='SMOKE_put_$STAMP' for x in r)"; then
      ok "название без name не изменилось"; else bad "название изменилось без name"; fi

    # неудачная замена оставляет прежний состав и название
    if [ -n "${MID2:-}" ]; then
      req PUT "/playlists/$PID2/items" -H "$JSON_H" -d "{\"name\": \"SMOKE_nope_$STAMP\", \"items\": [{\"media_id\": $MID3}, {\"media_id\": $MID2}]}"
      [ "$HTTP" = "409" ] && ok "замена с роликом на модерации → 409" || bad "замена с неодобренным: HTTP $HTTP (ждали 409)"
    fi
    req PUT "/playlists/$PID2/items" -H "$JSON_H" -d '{"items": []}'
    [ "$HTTP" = "400" ] && ok "замена пустым списком → 400" || bad "замена пустым списком: HTTP $HTTP (ждали 400)"
    req GET "/playlists/$PID2/items"
    if [ "$HTTP" = "200" ] && body_has "[(x['media_id'], x['position'], x['repeat_count']) for x in r]==[($MID,1,5)]"; then
      ok "после ошибок замены состав прежний"; else bad "состав изменился после неудачной замены"; fi
    req GET "/playlists"
    if [ "$HTTP" = "200" ] && body_has "any(x['id']==$PID2 and x['name']=='SMOKE_put_$STAMP' for x in r)"; then
      ok "после ошибок замены название прежнее"; else bad "название изменилось после неудачной замены"; fi
  fi
  req PUT "/playlists/2147483000/items" -H "$JSON_H" -d "{\"items\": [{\"media_id\": $MID}]}"
  [ "$HTTP" = "404" ] && ok "замена в несуществующем плейлисте → 404" || bad "замена в несуществующем плейлисте: HTTP $HTTP (ждали 404)"
fi

# ── 4а. Показ по датам: предпросмотр и создание ─────────────────────────────
step "Показ по датам"
if [ -n "${SID:-}" ] && [ -n "${PID:-}" ]; then
  # Даты — завтра и послезавтра по МСК, считаем внутри ds_api (python там есть
  # всегда, а `date -d` на macOS-стенде не работает).
  msk_day(){ $DC python3 -c "import datetime as d; print(d.datetime.now(d.timezone(d.timedelta(hours=3))).date() + d.timedelta(days=$1))" 2>/dev/null; }
  D1=$(msk_day 1); D2=$(msk_day 2)
  PLC_JSON="\"playlist_id\": $PID, \"all_screens\": false, \"screen_ids\": [$SID], \"date_from\": \"$D1\", \"date_to\": \"$D2\", \"hour_from\": 10, \"hour_to\": 12"
  req POST "/placements/preview" -H 'Content-Type: application/json' -d "{$PLC_JSON}"
  SUMMARY=$(echo "$BODY" | python3 -c "import sys,json;print(json.load(sys.stdin).get('summary',''))" 2>/dev/null)
  if [ "$HTTP" = "200" ] && [ -n "$SUMMARY" ]; then ok "предпросмотр показа: $SUMMARY"; else bad "предпросмотр показа: HTTP $HTTP"; fi
  req POST "/placements" -H 'Content-Type: application/json' -d "{$PLC_JSON, \"decisions\": []}"
  PLC=$(echo "$BODY" | python3 -c "import sys,json;print(json.load(sys.stdin).get('id',''))" 2>/dev/null)
  if [ "$HTTP" = "200" ] && [ -n "$PLC" ]; then ok "показ создан (id=$PLC)"; else bad "создание показа: HTTP $HTTP"; fi
fi
if [ -n "${PLC:-}" ]; then
  msk_monday(){ $DC python3 -c "import datetime as d; t=d.datetime.now(d.timezone(d.timedelta(hours=3))).date()+d.timedelta(days=$1); print(t-d.timedelta(days=t.weekday()-$2))" 2>/dev/null; }

  # список показов плейлиста: новый показ есть, он запланирован на завтра
  req GET "/placements?playlist_id=$PID"
  if [ "$HTTP" = "200" ] && body_has "any(x['id']==$PLC and x['status']=='planned' and x['screen_count']==1 for x in r)"; then
    ok "список показов плейлиста: #$PLC запланирован"; else bad "список показов: HTTP $HTTP"; fi

  # правка: часы 11–13 (дата начала не менялась)
  req PUT "/placements/$PLC" -H 'Content-Type: application/json' \
      -d "{\"playlist_id\": $PID, \"all_screens\": false, \"screen_ids\": [$SID], \"date_from\": \"$D1\", \"date_to\": \"$D2\", \"hour_from\": 11, \"hour_to\": 13, \"decisions\": []}"
  if [ "$HTTP" = "200" ]; then ok "показ изменён (11–13)"; else bad "правка показа: HTTP $HTTP"; fi
  req GET "/placements?playlist_id=$PID"
  if [ "$HTTP" = "200" ] && body_has "any(x['id']==$PLC and x['hour_from']==11 and x['hour_to']==13 for x in r)"; then
    ok "в списке новые часы показа"; else bad "правка не видна в списке"; fi

  # неделя экрана: текущая (168 клеток) и та, где завтрашний показ (виден в клетках 11–12)
  req GET "/placements/week?screen_id=$SID&start=$(msk_monday 0 0)"
  if [ "$HTTP" = "200" ] && body_has "len(r['days'])==7 and len(r['cells'])==168"; then
    ok "неделя экрана: 7 дней, 168 клеток"; else bad "неделя экрана: HTTP $HTTP"; fi
  req GET "/placements/week?screen_id=$SID&start=$(msk_monday 1 0)"
  if [ "$HTTP" = "200" ] && body_has "any(c['placement_id']==$PLC and c['kind']=='placement' and c['hour']==11 for c in r['cells'])"; then
    ok "недельная сетка показывает показ #$PLC"; else bad "в сетке нет показа #$PLC: HTTP $HTTP"; fi
  req GET "/placements/week?screen_id=$SID&start=$(msk_monday 0 1)"
  [ "$HTTP" = "400" ] && ok "неделя не с понедельника → 400" || bad "неделя не с понедельника: HTTP $HTTP (ждали 400)"

  # остановить запланированный нельзя
  req POST "/placements/$PLC/stop"
  [ "$HTTP" = "409" ] && ok "остановка запланированного показа → 409" || bad "остановка запланированного: HTTP $HTTP (ждали 409)"

  # идущий показ (сегодня, весь день): остановка → статус «stopped», повторная и отмена → 409
  D0=$(msk_day 0)
  req POST "/placements" -H 'Content-Type: application/json' \
      -d "{\"playlist_id\": $PID, \"all_screens\": false, \"screen_ids\": [$SID], \"date_from\": \"$D0\", \"date_to\": \"$D0\", \"hour_from\": 0, \"hour_to\": 24, \"decisions\": []}"
  PLC2=$(echo "$BODY" | python3 -c "import sys,json;print(json.load(sys.stdin).get('id',''))" 2>/dev/null)
  if [ "$HTTP" = "200" ] && [ -n "$PLC2" ]; then
    ok "идущий показ создан (id=$PLC2)"
    req POST "/placements/$PLC2/stop"
    [ "$HTTP" = "200" ] && ok "идущий показ остановлен" || bad "остановка идущего показа: HTTP $HTTP"
    req GET "/placements?playlist_id=$PID"
    if [ "$HTTP" = "200" ] && body_has "any(x['id']==$PLC2 and x['status']=='stopped' for x in r)"; then
      ok "в списке статус «остановлен»"; else bad "после остановки статус не stopped"; fi
    req POST "/placements/$PLC2/stop"
    [ "$HTTP" = "409" ] && ok "повторная остановка → 409" || bad "повторная остановка: HTTP $HTTP (ждали 409)"
    req DELETE "/placements/$PLC2"
    [ "$HTTP" = "409" ] && ok "отмена остановленного показа → 409" || bad "отмена остановленного: HTTP $HTTP (ждали 409)"
  else
    bad "создание идущего показа: HTTP $HTTP"
  fi
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
# Запланированный показ ($PLC) отменяем сам; остановленный ($PLC2) уходит
# вместе с плейлистом (ON DELETE CASCADE).
if [ -n "${PLC:-}" ]; then
  req DELETE "/placements/$PLC"
  if [ "$HTTP" = "200" ]; then ok "запланированный показ отменён"; else bad "отмена показа: HTTP $HTTP"; fi
  req GET "/placements?playlist_id=$PID"
  if [ "$HTTP" = "200" ] && ! echo "$BODY" | python3 -c "import sys,json; sys.exit(0 if any(x['id']==$PLC for x in json.load(sys.stdin)) else 1)" 2>/dev/null; then
    ok "отменённого показа в списке нет"; else bad "отменённый показ остался в списке"; fi
fi
[ -n "${PID:-}" ] && req DELETE "/playlists/$PID" && ok "плейлист удалён"
[ -n "${PID2:-}" ] && req DELETE "/playlists/$PID2" && ok "плейлист «одним запросом» удалён"
[ -n "${MID:-}" ] && req DELETE "/media/$MID"      && ok "медиа удалено"
[ -n "${MID2:-}" ] && req DELETE "/media/$MID2"    && ok "ролик на модерации удалён"
[ -n "${MID3:-}" ] && req DELETE "/media/$MID3"    && ok "второй ролик удалён"
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
