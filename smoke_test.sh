#!/bin/bash
# smoke_test.sh — быстрый прогон ключевых сценариев ПЕРЕД каждым релизом.
# Запускать на сервере после `install_server.sh` / обновления.
# Проверяет: все контейнеры живы, вход, здоровье API, полный цикл
# «экран → медиа → плейлист → расписание», показ по датам, что экран играет и
# качает (лента и список файлов глазами агента), «в сети» по последнему сигналу,
# отчёты, биллинг, уведомления.
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
SCR_TOKEN=$(body_get token)          # токен экрана: им smoke ходит как агент (X-Token)
[ -n "$SID" ] && ok "экран создан (id=$SID)" || bad "экран не создан"
# медиа (генерим тестовый ролик ffmpeg внутри контейнера)
$DC sh -c "ffmpeg -f lavfi -i testsrc=duration=1:size=160x120:rate=5 -y /tmp/smoke.mp4 -loglevel quiet" 2>/dev/null
# ffmpeg-ролик байт в байт один и тот же, поэтому остаток любого прошлого прогона дал бы 409:
# основную загрузку (и две следующие — это тот же файл) делаем с allow_duplicate=true
req POST "/media/upload?title=SMOKE_media_$STAMP&category=service&allow_duplicate=true" -F "file=@/tmp/smoke.mp4"
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
  req POST "/media/upload?title=SMOKE_media2_$STAMP&category=service&allow_duplicate=true" -F "file=@/tmp/smoke.mp4"
  MID3=$(body_get id)
  [ -n "$MID3" ] && ok "второй одобренный ролик загружен (id=$MID3)" || bad "второй ролик: HTTP $HTTP"
  req POST "/media/upload?title=SMOKE_pend_$STAMP&category=other&age_rating=0%2B&allow_duplicate=true" -F "file=@/tmp/smoke.mp4"
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

# ── 4б. Медиатека: дубли и сведения о ролике ─────────────────────────────────
step "Медиатека: дубли и сведения о ролике"
if [ -n "${MID:-}" ]; then
  JH='Content-Type: application/json'
  # сколько файлов с таким куском имени лежит в каталоге медиа (внутри ds_api)
  media_files(){ $DC sh -c "ls \"\${MEDIA_PATH:-/data/media}\" 2>/dev/null | grep -c \"$1\""; }

  # статус модерации виден в списке медиатеки
  req GET "/media"
  if [ "$HTTP" = "200" ] && body_has "any(x['id']==$MID and x['review_status']=='approved' for x in r)"; then
    ok "список медиатеки: у одобренного ролика review_status=approved"; else bad "список медиатеки без review_status: HTTP $HTTP"; fi
  if [ -n "${MID2:-}" ]; then
    if [ "$HTTP" = "200" ] && body_has "any(x['id']==$MID2 and x['review_status']=='pending' for x in r)"; then
      ok "список медиатеки: у ролика на модерации review_status=pending"; else bad "список медиатеки: pending не виден"; fi
  fi

  # тот же файл без allow_duplicate → 409 и сведения о ролике из медиатеки
  req GET "/media/$MID"; MD5_MID=$(body_get md5_hash)
  req POST "/media/upload?title=SMOKE_dup_$STAMP&category=service" -F "file=@/tmp/smoke.mp4;filename=SMOKE_dup_$STAMP.mp4"
  DUPX=$(body_get id)        # при 409 id нет; если файл всё же принят — запомним для уборки
  D409="$BODY"; H409="$HTTP"
  if [ "$H409" = "409" ] && body_has "r['detail']['message']=='Такой ролик уже есть в медиатеке' and isinstance(r['detail']['duplicate_of']['id'], int) and set(r['detail']['duplicate_of'])=={'id','title','filename','folder','advertiser','created_at','duration_seconds','playlists'} and r['detail']['duplicate_of']['title'] and r['detail']['duplicate_of']['filename'] and r['detail']['duplicate_of']['created_at'] and r['detail']['duplicate_of']['playlists']>=0"; then
    ok "повторная загрузка того же файла → 409 со сведениями о дубле"; else bad "повторная загрузка: HTTP $H409 (ждали 409 с duplicate_of)"; fi
  DUPOF=$(echo "$D409" | python3 -c "import sys,json;print(json.load(sys.stdin)['detail']['duplicate_of']['id'])" 2>/dev/null)
  req GET "/media/${DUPOF:-0}"
  if [ "$HTTP" = "200" ] && [ -n "$MD5_MID" ] && body_has "r['md5_hash']=='$MD5_MID'"; then
    ok "duplicate_of указывает на ролик с тем же md5 (id=$DUPOF)"; else bad "duplicate_of не тот ролик: HTTP $HTTP"; fi
  [ "$(media_files "SMOKE_dup_$STAMP")" = "0" ] && ok "после отказа файл с диска убран" || bad "после отказа файл остался на диске"
  req GET "/media"
  if [ "$HTTP" = "200" ] && ! body_has "any(x['title']=='SMOKE_dup_$STAMP' for x in r)"; then
    ok "после отказа записи в медиатеке нет"; else bad "после отказа в медиатеке появилась запись"; fi

  # allow_duplicate=true — загрузка проходит
  req POST "/media/upload?title=SMOKE_dup_$STAMP&category=service&allow_duplicate=true" -F "file=@/tmp/smoke.mp4;filename=SMOKE_dup_$STAMP.mp4"
  DUPID=$(body_get id)
  if [ "$HTTP" = "200" ] && [ -n "$DUPID" ] && body_has "r['md5_hash']=='$MD5_MID' and r['status']=='ready'"; then
    ok "allow_duplicate=true: дубль загружен (id=$DUPID)"; else bad "загрузка с allow_duplicate=true: HTTP $HTTP"; fi
  [ "$(media_files "SMOKE_dup_$STAMP")" = "1" ] && ok "файл дубля лежит на диске" || bad "файла дубля нет на диске"

  # список дублей: оба ролика видят друг друга, пустых групп и чужих полей нет
  req GET "/media/duplicates"
  if [ "$HTTP" = "200" ] && body_has "'$MID' in r and any(d['id']==${DUPID:-0} for d in r['$MID']) and all(d['id']!=$MID for d in r['$MID']) and '${DUPID:-0}' in r and any(d['id']==$MID for d in r['${DUPID:-0}']) and all(len(v)>=1 and all(set(d)=={'id','title'} for d in v) for v in r.values())"; then
    ok "/media/duplicates: ролики с одним md5 видят друг друга"; else bad "/media/duplicates: HTTP $HTTP"; fi

  # ролик с уникальным содержимым, рекламодателем, папкой и плейлистом: поля сведений
  $DC sh -c "ffmpeg -f lavfi -i testsrc=duration=1:size=160x120:rate=5 -metadata comment=SMOKE_$STAMP -y /tmp/smoke_u_$STAMP.mp4 -loglevel quiet" 2>/dev/null
  req POST "/advertisers" -H "$JH" -d "{\"name\": \"SMOKE_adv_$STAMP\", \"kind\": \"gov\"}"
  ADVID=$(body_get id)
  [ -n "$ADVID" ] && ok "владелец для проверки создан (id=$ADVID)" || bad "владелец не создан: HTTP $HTTP"
  FLID=""
  if [ -n "$ADVID" ]; then
    req POST "/advertisers/$ADVID/folders" -H "$JH" -d "{\"name\": \"SMOKE_fld_$STAMP\"}"
    FLID=$(body_get id)
    [ -n "$FLID" ] && ok "папка для проверки создана (id=$FLID)" || bad "папка не создана: HTTP $HTTP"
  fi
  req POST "/media/upload?title=SMOKE_uniq_$STAMP&category=service&allow_duplicate=true&advertiser_id=${ADVID:-0}&folder_id=${FLID:-0}" -F "file=@/tmp/smoke_u_$STAMP.mp4"
  MIDU=$(body_get id)
  if [ "$HTTP" = "200" ] && [ -n "$MIDU" ] && ! body_has "r['md5_hash']=='$MD5_MID'"; then
    ok "ролик с уникальным содержимым загружен (id=$MIDU)"; else bad "уникальный ролик: HTTP $HTTP"; fi
  if [ -n "$MIDU" ]; then
    req GET "/media/$MIDU"
    if [ "$HTTP" = "200" ] && body_has "r['folder_name']=='SMOKE_fld_$STAMP' and r['advertiser_name']=='SMOKE_adv_$STAMP' and r['playlists_count']==0 and r['review_status']=='approved'"; then
      ok "сведения о ролике: папка, владелец, 0 плейлистов"; else bad "сведения о ролике: HTTP $HTTP"; fi
    if [ -n "${PID:-}" ]; then
      req POST "/playlists/$PID/items?media_id=$MIDU"
      req GET "/media/$MIDU"
      if [ "$HTTP" = "200" ] && body_has "r['playlists_count']==1"; then
        ok "сведения о ролике: после добавления в плейлист playlists_count=1"; else bad "playlists_count после добавления: HTTP $HTTP"; fi
    fi
    # у ролика без папки и владельца поля сведений есть и пусты
    req GET "/media/$MID"
    if [ "$HTTP" = "200" ] && body_has "r['folder_name'] is None and r['advertiser_name'] is None and isinstance(r['playlists_count'], int)"; then
      ok "сведения о ролике без папки: folder_name и advertiser_name пусты"; else bad "сведения о ролике без папки: HTTP $HTTP"; fi
    # повтор уникального файла: в 409 — папка, владелец, число плейлистов, длительность
    EXPPL=0; [ -n "${PID:-}" ] && EXPPL=1          # он стоит в плейлисте $PID, если тот создан
    req POST "/media/upload?title=SMOKE_uniq_$STAMP&category=service" -F "file=@/tmp/smoke_u_$STAMP.mp4"
    UX=$(body_get id)
    if [ "$HTTP" = "409" ] && body_has "(lambda d: d['id']==$MIDU and d['title']=='SMOKE_uniq_$STAMP' and d['folder']=='SMOKE_fld_$STAMP' and d['advertiser']=='SMOKE_adv_$STAMP' and d['playlists']==$EXPPL and d['duration_seconds']>0 and d['filename'].endswith('smoke_u_$STAMP.mp4'))(r['detail']['duplicate_of'])"; then
      ok "409 о дубле: ролик, папка, владелец, плейлисты и длительность верны"; else bad "содержимое 409 о дубле: HTTP $HTTP"; fi
  fi
  req GET "/media/2147483000"
  [ "$HTTP" = "404" ] && ok "сведения о несуществующем ролике → 404" || bad "несуществующий ролик: HTTP $HTTP (ждали 404)"
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

# ── 4в. Что экран играет и качает: показ на текущий час ─────────────────────
# Глазами агента (X-Token экрана): показ на текущий час МСК на одном тестовом
# экране → лента отдаёт его как активный источник со сроком до следующего
# целого часа, ролик показа есть в списке закачки; после «Остановить» лента
# к показу не возвращается, ролик из закачки уходит. Плейлист — отдельный, с
# роликом MID3, которого нет ни в шаблоне, ни в других плейлистах экрана:
# иначе ролик был бы в списке закачки и без показа.
# Пересечение на том же часе и экране: второй показ (плейлист PID) без
# решения → 409, с «Заменить» — в ленте он; предпросмотр правки подставляет
# сохранённое решение, а у первого (старшего) — перевёрнутое решение второго
# («оставить»); после правки на «Оставить «…» на это время» (yield) лента
# снова отдаёт первый показ.
step "Экран: что играет и что качает"
# dreq — запрос агента: только X-Token экрана, без админского токена
dreq(){ # dreq PATH
  RESP=$($DC curl -s -w $'\n%{http_code}' "$API$1" -H "X-Token: $SCR_TOKEN" 2>/dev/null)
  HTTP="${RESP##*$'\n'}"; BODY="${RESP%$'\n'*}"
}
if [ -n "${SID:-}" ] && [ -n "${SCR_TOKEN:-}" ] && [ -n "${MID3:-}" ]; then
  req GET "/media/$MID3"; NOW_FILE=$(body_get filename)
  req POST "/playlists/full" -H 'Content-Type: application/json' \
      -d "{\"name\": \"SMOKE_now_$STAMP\", \"items\": [{\"media_id\": $MID3}]}"
  PIDN=$(body_get id)
  [ -n "$PIDN" ] && ok "плейлист для показа на текущий час создан (id=$PIDN)" || bad "плейлист SMOKE_now: HTTP $HTTP"

  # Часы — МСК внутри ds_api (на macOS-стенде нет `date -d`). В последние 30 с
  # часа ждём начала следующего, чтобы показ не кончился посреди проверки.
  # Печатает: дата, час, ожидаемый active_until (следующий целый час, наивное МСК).
  NOWH=$($DC python3 -c "
import datetime as d, time
msk = d.timezone(d.timedelta(hours=3))
n = d.datetime.now(msk)
if n.minute == 59 and n.second >= 30:
    time.sleep(61 - n.second); n = d.datetime.now(msk)
h = n.replace(minute=0, second=0, microsecond=0, tzinfo=None)
print(h.date(), h.hour, (h + d.timedelta(hours=1)).isoformat())" 2>/dev/null)
  read -r DN HN UNTIL <<< "$NOWH"

  dreq "/files/list/$SID"
  if [ "$HTTP" = "200" ] && [ -n "$NOW_FILE" ] && ! body_has "any(f['filename']=='$NOW_FILE' for f in r)"; then
    ok "до показа ролика SMOKE_now нет в списке закачки экрана"; else bad "список закачки до показа: HTTP $HTTP"; fi

  PLCN=""
  if [ -n "$PIDN" ] && [ -n "${HN:-}" ]; then
    req POST "/placements" -H 'Content-Type: application/json' \
        -d "{\"playlist_id\": $PIDN, \"all_screens\": false, \"screen_ids\": [$SID], \"date_from\": \"$DN\", \"date_to\": \"$DN\", \"hour_from\": $HN, \"hour_to\": $((HN + 1)), \"decisions\": []}"
    PLCN=$(body_get id)
    if [ "$HTTP" = "200" ] && [ -n "$PLCN" ]; then
      ok "показ на текущий час ${HN}:00–$((HN + 1)):00 создан (id=$PLCN)"; else bad "показ на текущий час: HTTP $HTTP"; fi
  fi

  if [ -n "$PLCN" ]; then
    req GET "/broadcast"
    BC_ON=""; body_has "r.get('is_on')" && BC_ON=1
    if [ -n "$BC_ON" ]; then
      echo "  – включён «Эфир сети»: он перекрывает показы, проверка ленты пропущена"
    else
      dreq "/schedule/minipc/$SID"
      if [ "$HTTP" = "200" ] && body_has "r['active_source']=='placement' and r['active_playlist']['playlist_id']==$PIDN and r['active_until']=='$UNTIL' and any(i['filename']=='$NOW_FILE' for i in r['active_items'])"; then
        ok "лента экрана: играет показ, active_until=$UNTIL"
      else
        bad "лента экрана: ждали показ #$PLCN до $UNTIL, получили HTTP $HTTP: $(echo "$BODY" | python3 -c "import sys,json;r=json.load(sys.stdin);print(r.get('active_source'), r.get('active_until'))" 2>/dev/null)"
      fi
    fi
    dreq "/files/list/$SID"
    if [ "$HTTP" = "200" ] && body_has "any(f['filename']=='$NOW_FILE' for f in r)"; then
      ok "ролик показа в списке закачки экрана"; else bad "ролика показа нет в списке закачки: HTTP $HTTP"; fi

    # пересечение: второй показ на тот же час — сначала «Заменить», потом правка на «Оставить …»
    PLCY=""
    if [ -n "${PID:-}" ]; then
      YJ="\"playlist_id\": $PID, \"all_screens\": false, \"screen_ids\": [$SID], \"date_from\": \"$DN\", \"date_to\": \"$DN\", \"hour_from\": $HN, \"hour_to\": $((HN + 1))"
      ydec(){ echo "[{\"screen_id\": $SID, \"other_kind\": \"placement\", \"other_id\": $PLCN, \"mode\": \"$1\"}]"; }
      req POST "/placements" -H 'Content-Type: application/json' -d "{$YJ, \"decisions\": []}"
      [ "$HTTP" = "409" ] && ok "второй показ на тот же час без решения → 409" || bad "пересечение без решения: HTTP $HTTP (ждали 409)"
      req POST "/placements" -H 'Content-Type: application/json' -d "{$YJ, \"decisions\": $(ydec replace)}"
      PLCY=$(body_get id)
      if [ "$HTTP" = "200" ] && [ -n "$PLCY" ]; then
        ok "второй показ «Заменить на это время» создан (id=$PLCY)"; else bad "второй показ с решением: HTTP $HTTP"; fi
    fi
    if [ -n "$PLCY" ]; then
      req POST "/placements/preview" -H 'Content-Type: application/json' -d "{$YJ, \"exclude_id\": $PLCY}"
      if [ "$HTTP" = "200" ] && body_has "r['decisions']==[{'screen_id': $SID, 'other_kind': 'placement', 'other_id': $PLCN, 'mode': 'replace'}]"; then
        ok "предпросмотр правки отдаёт сохранённое решение «заменить»"; else bad "предпросмотр правки без сохранённого решения: HTTP $HTTP"; fi
      # правка первого (старшего) показа: действует «заменить» второго — подставляется «оставить»
      req POST "/placements/preview" -H 'Content-Type: application/json' \
          -d "{\"playlist_id\": $PIDN, \"all_screens\": false, \"screen_ids\": [$SID], \"date_from\": \"$DN\", \"date_to\": \"$DN\", \"hour_from\": $HN, \"hour_to\": $((HN + 1)), \"exclude_id\": $PLCN}"
      if [ "$HTTP" = "200" ] && body_has "r['decisions']==[{'screen_id': $SID, 'other_kind': 'placement', 'other_id': $PLCY, 'mode': 'yield'}]"; then
        ok "предпросмотр правки первого показа: подставлено «оставить» (решение второго перевёрнуто)"; else bad "перевёрнутое решение в предпросмотре: HTTP $HTTP"; fi
      if [ -z "$BC_ON" ]; then
        dreq "/schedule/minipc/$SID"
        if [ "$HTTP" = "200" ] && body_has "r['active_source']=='placement' and r['active_playlist']['playlist_id']==$PID"; then
          ok "лента экрана: в час пересечения идёт заменивший показ"; else bad "лента при «Заменить»: HTTP $HTTP"; fi
      fi
      req PUT "/placements/$PLCY" -H 'Content-Type: application/json' -d "{$YJ, \"decisions\": $(ydec yield)}"
      [ "$HTTP" = "200" ] && ok "правка: «Оставить … на это время» (yield)" || bad "правка на yield: HTTP $HTTP"
      if [ -z "$BC_ON" ]; then
        dreq "/schedule/minipc/$SID"
        if [ "$HTTP" = "200" ] && body_has "r['active_source']=='placement' and r['active_playlist']['playlist_id']==$PIDN"; then
          ok "лента экрана: после yield в час пересечения снова идёт первый показ"
        else
          bad "лента после yield: ждали плейлист $PIDN, получили HTTP $HTTP: $(echo "$BODY" | python3 -c "import sys,json;r=json.load(sys.stdin);print(r.get('active_source'), (r.get('active_playlist') or {}).get('playlist_id'))" 2>/dev/null)"
        fi
      fi
      req GET "/placements?playlist_id=$PID"
      if [ "$HTTP" = "200" ] && body_has "any(x['id']==$PLCY and [a['mode'] for a in x['alternates']]==['yield'] for x in r)"; then
        ok "в списке показов строка «уступает»"; else bad "в списке показов нет строки «уступает»: HTTP $HTTP"; fi
      req POST "/placements/$PLCY/stop"
      [ "$HTTP" = "200" ] && ok "второй показ на текущий час остановлен" || bad "остановка второго показа: HTTP $HTTP"
    fi

    req POST "/placements/$PLCN/stop"
    [ "$HTTP" = "200" ] && ok "показ на текущий час остановлен" || bad "остановка показа на текущий час: HTTP $HTTP"
    dreq "/schedule/minipc/$SID"
    if [ "$HTTP" = "200" ] && body_has "r['active_source']!='placement' and (r['active_playlist'] or {}).get('playlist_id')!=$PIDN"; then
      ok "после «Остановить» лента экрана больше не отдаёт показ"; else bad "после остановки лента: HTTP $HTTP"; fi
    dreq "/files/list/$SID"
    if [ "$HTTP" = "200" ] && ! body_has "any(f['filename']=='$NOW_FILE' for f in r)"; then
      ok "после «Остановить» ролика показа нет в списке закачки"; else bad "после остановки ролик в списке закачки: HTTP $HTTP"; fi
  fi
else
  bad "нет экрана, его токена или ролика — проверку ленты провести не на чем"
fi

# ── 4г. «В сети» по последнему сигналу ───────────────────────────────────────
# Единое правило (этап 2, §4.5): экран в сети, если его последний сигнал не старше
# 5 минут. Только что зарегистрированный SMOKE-экран сигнала не подавал: не в сети,
# offline_for_seconds = null. После heartbeat (как агент: X-Token, тело как у агента) —
# в сети: и в списке экранов, и на дашборде (счётчик, плитки «Сейчас в эфире» и «Диски»);
# с последнего сигнала — секунды, а не часы (last_seen пишется в UTC, NOW() в БД тоже UTC).
step "Экран: «в сети» по последнему сигналу"
if [ -n "${SID:-}" ] && [ -n "${SCR_TOKEN:-}" ]; then
  req GET "/minipc"
  if [ "$HTTP" = "200" ] && body_has "any(x['id']==$SID and x['online'] is False and x['offline_for_seconds'] is None for x in r)"; then
    ok "экран без сигнала: не в сети, offline_for_seconds = null"
  else
    bad "экран без сигнала: ждали online=false и offline_for_seconds=null, HTTP $HTTP"
  fi
  RESP=$($DC curl -s -w $'\n%{http_code}' -X POST "$API/heartbeat/$SID" -H "X-Token: $SCR_TOKEN" \
        -H 'Content-Type: application/json' -d '{"status":"online","playing_file":null}' 2>/dev/null)
  HTTP="${RESP##*$'\n'}"; BODY="${RESP%$'\n'*}"
  [ "$HTTP" = "200" ] && ok "heartbeat экрана принят" || bad "heartbeat: HTTP $HTTP"
  req GET "/minipc"
  if [ "$HTTP" = "200" ] && body_has "any(x['id']==$SID and x['online'] is True and x['offline_for_seconds'] is not None and 0 <= x['offline_for_seconds'] < 60 for x in r)"; then
    ok "после heartbeat: в сети, с последнего сигнала — секунды"
  else
    bad "после heartbeat: ждали online=true и offline_for_seconds < 60, HTTP $HTTP: $(echo "$BODY" | python3 -c "import sys,json;print([(x['online'], x['offline_for_seconds']) for x in json.load(sys.stdin) if x['id']==$SID])" 2>/dev/null)"
  fi
  req GET "/dashboard"
  if [ "$HTTP" = "200" ] && body_has "r['screens_online']>=1 and any(x['name']=='SMOKE_scr_$STAMP' and x['online'] is True for x in r['now_playing']) and any(x['name']=='SMOKE_scr_$STAMP' and x['online'] is True for x in r['disks'])"; then
    ok "дашборд: экран в сети (счётчик, «Сейчас в эфире», «Диски»)"
  else
    bad "дашборд: экран после heartbeat не в сети, HTTP $HTTP"
  fi
else
  bad "нет экрана или его токена — проверку «в сети» провести не на чем"
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
# плейлист показа на текущий час — вместе с его (остановленным) показом
if [ -n "${PIDN:-}" ]; then
  req DELETE "/playlists/$PIDN"
  [ "$HTTP" = "200" ] && ok "плейлист показа на текущий час удалён" || bad "удаление плейлиста SMOKE_now: HTTP $HTTP"
fi
[ -n "${MID:-}" ] && req DELETE "/media/$MID"      && ok "медиа удалено"
[ -n "${MID2:-}" ] && req DELETE "/media/$MID2"    && ok "ролик на модерации удалён"
[ -n "${MID3:-}" ] && req DELETE "/media/$MID3"    && ok "второй ролик удалён"
# Дубли и сведения: id берём из ответов сервера как есть (в т.ч. если проверка
# выше упала и файл всё же принят), поэтому уборка не зависит от исхода проверок.
for DID in "${DUPX:-}" "${UX:-}" "${DUPID:-}" "${MIDU:-}"; do
  [ -n "$DID" ] || continue
  req DELETE "/media/$DID"
  [ "$HTTP" = "200" ] && ok "ролик проверки дублей удалён (id=$DID)" || bad "удаление ролика $DID: HTTP $HTTP"
done
if [ -n "${FLID:-}" ]; then
  req DELETE "/folders/$FLID"
  [ "$HTTP" = "200" ] && ok "папка проверки удалена" || bad "удаление папки проверки: HTTP $HTTP"
fi
if [ -n "${ADVID:-}" ]; then
  req DELETE "/advertisers/$ADVID"
  [ "$HTTP" = "200" ] && ok "владелец проверки удалён" || bad "удаление владельца проверки: HTTP $HTTP"
fi
if [ -n "${MD5_MID:-}" ]; then
  [ "$(media_files "SMOKE_dup_$STAMP")" = "0" ] && ok "файлов дублей на диске не осталось" || bad "на диске остались файлы дублей"
  [ "$(media_files "smoke_u_$STAMP")" = "0" ] && ok "файла уникального ролика на диске не осталось" || bad "на диске остался файл уникального ролика"
fi
[ -n "${SID:-}" ] && req DELETE "/minipc/$SID"     && ok "экран удалён"
$DC rm -f /tmp/smoke.mp4 /tmp/smoke_u_$STAMP.mp4 2>/dev/null

# ── Итог ─────────────────────────────────────────────────────────────────────
echo
echo "════════════════════════════════════════════"
echo " ИТОГ: пройдено $PASS, провалено $FAIL"
echo "════════════════════════════════════════════"
[ "$FAIL" -eq 0 ] && echo "✅ Все проверки прошли — релиз можно выпускать." \
                  || echo "❌ Есть провалы — НЕ выпускать до устранения."
exit "$FAIL"
