#!/bin/bash
# smoke_test.sh — быстрый прогон ключевых сценариев ПЕРЕД каждым релизом.
# Запускать на сервере после `install_server.sh` / обновления.
# Проверяет: все контейнеры живы, вход, здоровье API, полный цикл
# «экран → медиа → плейлист → расписание», показ по датам, что экран играет и
# качает (лента и список файлов глазами агента), «в сети» по последнему сигналу,
# бегущая строка (только на тестовом экране), отчёты, биллинг, уведомления,
# бэкап и архив диагностики отдаются только по входу (без входа — 401), файлы
# медиатеки — тоже (через nginx; экран качает одобренный ролик с докачкой);
# «Копия на флешку»: статус и выдача ссылок — только суперадмину (без входа и по токену
# экрана — 401), одноразовая ссылка скачивания без выдачи — 404.
# Все тестовые данные создаются с префиксом SMOKE_ и удаляются в конце.
#
# Использование:   bash smoke_test.sh
# Переменные:      ADMIN_USER (по умолч. admin), ADMIN_PASS (admin123)
#                  SMOKE_ALLOW_BROADCAST=1 — шаг «Общий эфир»: он включает эфир на ВСЕ
#                  экраны сети (прежнее состояние возвращается), поэтому только на стенде
#                  SMOKE_ALLOW_OFFSITE=1 — полный цикл «Копии на флешку»: СБРАСЫВАЕТ пароль на
#                  копию и задаёт тестовый (SMOKE-copy-pass-1), собирает и скачивает копию и
#                  ролики (двигает отметку «последняя копия» и «водяной знак» роликов),
#                  проверяет tar с тремя файлами и расшифровку, заливает дамп из копии во
#                  временную базу (и удаляет её), пишет в журнал; в конце снова
#                  сбрасывает пароль на копию — поэтому только на стенде: шаг сам отказывает
#                  (проверка проваливается), если контейнер ds_api не из compose-проекта dsdev
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

    # обзор сети («Эфир сети»): SMOKE-экран в таблице, «сейчас» — показ текущего часа до
    # следующего целого часа (в 23 ч смены сегодня нет: until = null), сигнала ещё не было
    req GET "/network/live"
    if [ "$HTTP" = "200" ] && body_has "r['online_minutes']==5 and r['counts']['total']==len(r['screens']) and r['counts']['online']+r['counts']['offline']==r['counts']['total'] and r['broadcast']['screens_total']==len(r['screens'])"; then
      ok "обзор сети: ответ собран, счётчики сходятся"; else bad "обзор сети: HTTP $HTTP"; fi
    if [ -n "$BC_ON" ]; then
      echo "  – включён «Эфир сети»: «сейчас» в обзоре — эфир, проверка показа пропущена"
    elif body_has "any(x['id']==$SID and x['online'] is False and x['offline_for_seconds'] is None and x['playing'] is None and x['now']['kind']=='placement' and x['now']['playlist_ids']==[$PIDN] and (x['now']['until']=='$UNTIL' or ($HN==23 and x['now']['until'] is None)) and x['broadcast_skipped'] is False for x in r['screens']) and r['playlists']['$PIDN']['name']=='SMOKE_now_$STAMP'"; then
      ok "обзор сети: SMOKE-экран — показ на текущий час до $UNTIL, не в сети"
    else
      bad "обзор сети: ждали показ #$PLCN до $UNTIL, получили HTTP $HTTP: $(echo "$BODY" | python3 -c "import sys,json;print([(x['online'], x['now']) for x in json.load(sys.stdin)['screens'] if x['id']==$SID])" 2>/dev/null)"
    fi

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
  # вкладка «Экраны → Группы» раскладывает экраны по group_id: ключ должен быть в списке
  # (null допустим), а если у экрана есть group_name — и group_id (экран сам попадает в «Все экраны»)
  if body_has "any(x['id']==$SID and 'group_id' in x and (x['group_id'] is not None or x['group_name'] is None) for x in r)"; then
    ok "GET /minipc отдаёт group_id экрана"
  else
    bad "GET /minipc: у экрана нет group_id (вкладка «Группы» не сможет разложить экраны)"
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

# ── 4г-2. Обзор сети: «играет ролик» и слоты шаблона ─────────────────────────
# Агент сообщил ролик (heartbeat с playing_file) → в обзоре сети экран в сети, ролик
# сопоставлен с медиатекой (id и название); слоты недельного шаблона плейлиста считаются
# (слот Пн 10:00 из основного цикла); неизвестный плейлист → 404.
step "Обзор сети: ролик на экране и слоты шаблона"
if [ -n "${SID:-}" ] && [ -n "${SCR_TOKEN:-}" ] && [ -n "${NOW_FILE:-}" ] && [ -n "${MID3:-}" ]; then
  RESP=$($DC curl -s -w $'\n%{http_code}' -X POST "$API/heartbeat/$SID" -H "X-Token: $SCR_TOKEN" \
        -H 'Content-Type: application/json' -d "{\"status\":\"online\",\"playing_file\":\"$NOW_FILE\"}" 2>/dev/null)
  HTTP="${RESP##*$'\n'}"; BODY="${RESP%$'\n'*}"
  [ "$HTTP" = "200" ] && ok "heartbeat с playing_file принят" || bad "heartbeat с playing_file: HTTP $HTTP"
  req GET "/network/live"
  if [ "$HTTP" = "200" ] && body_has "any(x['id']==$SID and x['online'] is True and x['offline_for_seconds'] < 60 and x['playing']=={'media_id': $MID3, 'title': 'SMOKE_media2_$STAMP', 'filename': '$NOW_FILE'} for x in r['screens'])"; then
    ok "обзор сети: экран в сети, играет ролик SMOKE_media2 (id=$MID3)"
  else
    bad "обзор сети: «играет ролик» не сошёлся, HTTP $HTTP: $(echo "$BODY" | python3 -c "import sys,json;print([(x['online'], x['playing']) for x in json.load(sys.stdin)['screens'] if x['id']==$SID])" 2>/dev/null)"
  fi
else
  bad "нет экрана, токена или ролика — «играет ролик» в обзоре сети проверить не на чем"
fi
if [ -n "${PID:-}" ]; then
  req GET "/network/playlist/$PID/usage"
  if [ "$HTTP" = "200" ] && body_has "set(r)=={'template_slots'} and isinstance(r['template_slots'], int) and r['template_slots']>=1"; then
    ok "слоты шаблона плейлиста: template_slots=$(body_get template_slots)"; else bad "usage плейлиста: HTTP $HTTP $BODY"; fi
fi
req GET "/network/playlist/2147483000/usage"
[ "$HTTP" = "404" ] && ok "usage несуществующего плейлиста → 404" || bad "usage несуществующего плейлиста: HTTP $HTTP (ждали 404)"

# ── 4д. Общий эфир: срок «до ЧЧ:00» ──────────────────────────────────────────
# Общий эфир сети идёт на ВСЕ экраны сразу, поэтому шаг выполняется только на стенде
# (SMOKE_ALLOW_BROADCAST=1); на боевом сервере его не включают. Что проверяем:
# включение до следующего целого часа (час и ожидаемый срок считаем в ds_api по МСК,
# при 23 час окончания = 24), срок в GET /broadcast, в журнале и на дашборде; глазами
# SMOKE-экрана — пустому плейлисту эфира нечего играть (эфир на экране не идёт), с
# роликом лента отдаёт эфир с active_until = срок, ролик эфира в списке закачки,
# симулятор пишет срок; час окончания не позже текущего → 400, неизвестный плейлист
# → 404, старый вызов без тела (?playlist_id=N) = «до отмены» (active_until = null);
# выключение — лента не отдаёт эфир, ролика нет в закачке. Что было до проверки — запоминаем
# и возвращаем в конце (и при обрыве скрипта), плейлист проверки удаляем.
# Само истечение по времени (Celery раз в минуту) здесь не ждём: час целый,
# оно покрыто pytest, а на стенде проверяется сдвигом until_at в БД.
step "Общий эфир: срок «до ЧЧ:00»"
BC_DIRTY=0; BC_PID_TMP=""
bc_finish(){ # вернуть прежнее состояние эфира, затем удалить плейлист проверки
  if [ "$BC_DIRTY" = "1" ]; then
    BC_DIRTY=0
    if [ "$BC0_ON" = "1" ]; then
      req POST "/broadcast/on" -H 'Content-Type: application/json' \
          -d "{\"playlist_id\": $BC0_PID, \"until_hour\": ${BC0_HOUR:-null}}"
      if [ "$HTTP" = "200" ]; then ok "прежний общий эфир возвращён (плейлист #$BC0_PID, ${BC0_UNTIL:-до отмены})"
      else bad "не удалось вернуть прежний общий эфир: HTTP $HTTP $BODY"; fi
    else
      req POST "/broadcast/off"
      if [ "$HTTP" = "200" ]; then ok "общий эфир выключен, как и было"
      else bad "не удалось выключить общий эфир после проверки: HTTP $HTTP"; fi
    fi
  fi
  if [ -n "$BC_PID_TMP" ]; then
    req DELETE "/playlists/$BC_PID_TMP"
    [ "$HTTP" = "200" ] && ok "плейлист проверки эфира удалён" || bad "удаление плейлиста проверки эфира: HTTP $HTTP"
    BC_PID_TMP=""
  fi
}
if [ "${SMOKE_ALLOW_BROADCAST:-}" != "1" ]; then
  echo "  – эфир на все экраны — пропущено (задайте SMOKE_ALLOW_BROADCAST=1 на стенде)"
else
  # что было до проверки
  req GET "/broadcast"
  BC0_ON=""; BC0_PID=""; BC0_UNTIL=""; BC0_HOUR=""
  if [ "$HTTP" = "200" ]; then
    body_has "r.get('is_on') is True" && BC0_ON=1
    if [ -n "$BC0_ON" ]; then
      BC0_PID=$(body_get playlist_id); BC0_UNTIL=$(body_get until_at)
      [ "$BC0_UNTIL" = "None" ] && BC0_UNTIL=""
      [ -n "$BC0_UNTIL" ] && BC0_HOUR=$($DC python3 -c "from datetime import datetime; u=datetime.fromisoformat('$BC0_UNTIL'); print(24 if (u.hour==0 and u.minute==0) else u.hour + (1 if (u.minute or u.second) else 0))" 2>/dev/null)
      echo "  – эфир уже включён (плейлист #$BC0_PID, ${BC0_UNTIL:-до отмены}) — вернём после проверки"
    fi
    trap bc_finish EXIT
    req POST "/playlists?name=SMOKE_bc_$STAMP"
    BC_PID_TMP=$(body_get id)
    [ -n "$BC_PID_TMP" ] && ok "плейлист для проверки эфира создан (id=$BC_PID_TMP)" || bad "плейлист для проверки эфира не создан: HTTP $HTTP"
  else
    bad "общий эфир: GET /broadcast вернул HTTP $HTTP — проверку провести не на чем"
  fi

  if [ -n "$BC_PID_TMP" ]; then
    # час и срок считаем в ds_api по МСК: хост может быть macOS или в другом поясе.
    # В последние 30 с часа ждём следующего, чтобы эфир не кончился посреди проверки.
    # Печатает: текущий час, ожидаемый until_at, «ЧЧ:00» срока, сегодняшняя дата.
    read -r BC_H BC_EXP BC_TXT BC_D <<< "$($DC python3 -c "
import time
from datetime import datetime, timezone, timedelta as td
msk = timezone(td(hours=3))
n = datetime.now(msk)
if n.minute == 59 and n.second >= 30:
    time.sleep(61 - n.second); n = datetime.now(msk)
n = n.replace(tzinfo=None)
u = datetime(n.year, n.month, n.day) + td(hours=n.hour + 1)
print(n.hour, u.isoformat(), '24:00' if n.hour == 23 else '%02d:00' % (n.hour + 1), n.date())" 2>/dev/null)"
    # лента экрана глазами агента: источник и срок — для сообщений о провале
    feed_brief(){ echo "$BODY" | python3 -c "import sys,json;r=json.load(sys.stdin);print(r.get('active_source'), (r.get('active_playlist') or {}).get('playlist_id'), r.get('active_until'))" 2>/dev/null; }
    BC_FEED=""   # проверять ленту и закачку есть на чем: SMOKE-экран, его токен и ролик MID3
    [ -n "${SID:-}" ] && [ -n "${SCR_TOKEN:-}" ] && [ -n "${MID3:-}" ] && [ -n "${NOW_FILE:-}" ] && BC_FEED=1
    if [ -z "${BC_H:-}" ]; then
      bad "общий эфир: не удалось вычислить час в ds_api"
    else
      BC_DIRTY=1
      req POST "/broadcast/on" -H 'Content-Type: application/json' \
          -d "{\"playlist_id\": $BC_PID_TMP, \"until_hour\": $((BC_H + 1))}"
      if [ "$HTTP" = "200" ] && body_has "r['is_on'] is True and r['playlist_id']==$BC_PID_TMP and r['until_at']=='$BC_EXP'"; then
        ok "общий эфир включён до $BC_TXT МСК (until_at=$BC_EXP)"
      else
        bad "включение эфира до $BC_TXT: HTTP $HTTP $BODY"
      fi
      req GET "/broadcast"
      if [ "$HTTP" = "200" ] && body_has "r['is_on'] is True and r['playlist_name']=='SMOKE_bc_$STAMP' and r['until_at']=='$BC_EXP'"; then
        ok "GET /broadcast: идёт, срок $BC_EXP"; else bad "GET /broadcast после включения: HTTP $HTTP $BODY"; fi
      req GET "/dashboard"
      if [ "$HTTP" = "200" ] && body_has "any(x['title'] in ('Общий эфир включён','Общий эфир сменён') and 'SMOKE_bc_$STAMP' in (x['detail'] or '') and 'до $BC_TXT' in x['detail'] for x in r['feed'])"; then
        ok "в журнале: «Общий эфир включён/сменён … до $BC_TXT»"; else bad "в журнале нет записи о включении до $BC_TXT"; fi
      if [ "$HTTP" = "200" ] && body_has "r['broadcast_on'] is True and r['broadcast_playlist']=='SMOKE_bc_$STAMP' and r['broadcast_until']=='$BC_EXP'"; then
        ok "дашборд: общий эфир идёт, срок $BC_EXP"; else bad "дашборд при эфире: HTTP $HTTP"; fi

      # Глазами SMOKE-экрана (X-Token): плейлист эфира пока пуст — экрану нечего
      # играть, эфир на нём не идёт (не чёрный экран); с роликом MID3 — лента
      # отдаёт эфир со сроком, ролик эфира в списке закачки.
      if [ -n "$BC_FEED" ]; then
        dreq "/schedule/minipc/$SID"
        if [ "$HTTP" = "200" ] && body_has "r['active_source']!='broadcast'"; then
          ok "лента экрана: пустому плейлисту эфира нечего играть — эфир здесь не идёт"
        else bad "лента при пустом плейлисте эфира: HTTP $HTTP $(feed_brief)"; fi
        req GET "/network/live"
        if [ "$HTTP" = "200" ] && body_has "r['broadcast']['is_on'] is True and r['broadcast']['unplayable_screens']>=1 and any(x['id']==$SID and x['broadcast_skipped'] is True and x['now']['kind']!='broadcast' and (x['next'] is not None)==(x['now']['until'] is not None) and (x['next'] is None or x['next']['from']==x['now']['until']) for x in r['screens'])"; then
          ok "обзор сети: пустому плейлисту эфира нечего играть на SMOKE-экране (broadcast_skipped), «дальше» — его расписание"
        else bad "обзор сети при пустом плейлисте эфира: HTTP $HTTP"; fi
        req POST "/playlists/$BC_PID_TMP/items?media_id=$MID3"
        [ "$HTTP" = "200" ] && ok "ролик SMOKE_now добавлен в плейлист эфира" || bad "ролик в плейлист эфира: HTTP $HTTP"
        dreq "/schedule/minipc/$SID"
        if [ "$HTTP" = "200" ] && body_has "r['active_source']=='broadcast' and r['active_playlist']['playlist_id']==$BC_PID_TMP and r['active_until']=='$BC_EXP' and any(i['filename']=='$NOW_FILE' for i in r['active_items'])"; then
          ok "лента экрана: идёт общий эфир, active_until=$BC_EXP"
        else bad "лента экрана при эфире: ждали эфир #$BC_PID_TMP до $BC_EXP, получили HTTP $HTTP $(feed_brief)"; fi
        req GET "/network/live"
        if [ "$HTTP" = "200" ] && body_has "r['broadcast']['is_on'] is True and r['broadcast']['until_at']=='$BC_EXP' and r['broadcast']['screens_total']==len(r['screens']) and r['broadcast']['offline_screens']==r['counts']['offline'] and r['playlists']['$BC_PID_TMP']['name']=='SMOKE_bc_$STAMP' and any(x['id']==$SID and x['broadcast_skipped'] is False and x['now']['kind']=='broadcast' and x['now']['playlist_ids']==[$BC_PID_TMP] and x['now']['until']=='$BC_EXP' and x['next'] is None and x['after_broadcast'] is not None for x in r['screens'])"; then
          ok "обзор сети: на SMOKE-экране идёт общий эфир до $BC_EXP, после него — расписание"
        else bad "обзор сети при эфире: HTTP $HTTP"; fi
        dreq "/files/list/$SID"
        if [ "$HTTP" = "200" ] && body_has "any(f['filename']=='$NOW_FILE' for f in r)"; then
          ok "ролик эфира в списке закачки экрана"; else bad "ролика эфира нет в списке закачки: HTTP $HTTP"; fi
        req GET "/schedule/simulate?screen_id=$SID&on_date=$BC_D&hour=$BC_H"
        if [ "$HTTP" = "200" ] && body_has "any('общий эфир «SMOKE_bc_$STAMP» до $BC_TXT' in n for n in r['notes'])"; then
          ok "симулятор: пометка об эфире до $BC_TXT"; else bad "симулятор без пометки об эфире до $BC_TXT: HTTP $HTTP"; fi
      else
        bad "нет экрана, его токена или ролика — ленту и закачку при эфире проверить не на чем"
      fi

      # час не позже текущего и мусор — 400, состояние не меняется
      req POST "/broadcast/on" -H 'Content-Type: application/json' \
          -d "{\"playlist_id\": $BC_PID_TMP, \"until_hour\": $BC_H}"
      [ "$HTTP" = "400" ] && ok "час окончания = текущему часу ($BC_H) → 400" || bad "до текущего часа: HTTP $HTTP (ждали 400)"
      req POST "/broadcast/on" -H 'Content-Type: application/json' \
          -d "{\"playlist_id\": $BC_PID_TMP, \"until_hour\": 25}"
      [ "$HTTP" = "400" ] && ok "час окончания 25 → 400" || bad "до 25 часов: HTTP $HTTP (ждали 400)"
      req POST "/broadcast/on" -H 'Content-Type: application/json' \
          -d '{"playlist_id": 2147483647, "until_hour": null}'
      [ "$HTTP" = "404" ] && ok "неизвестный плейлист → 404" || bad "неизвестный плейлист: HTTP $HTTP (ждали 404)"
      req GET "/broadcast"
      if [ "$HTTP" = "200" ] && body_has "r['is_on'] is True and r['until_at']=='$BC_EXP'"; then
        ok "после отказов эфир прежний (срок $BC_EXP)"; else bad "отказы изменили эфир: HTTP $HTTP $BODY"; fi

      # старый вызов без тела — «до отмены»
      req POST "/broadcast/on?playlist_id=$BC_PID_TMP"
      if [ "$HTTP" = "200" ] && body_has "r['is_on'] is True and r['until_at'] is None"; then
        ok "POST /broadcast/on?playlist_id=N без тела → до отмены"; else bad "старый вызов без тела: HTTP $HTTP $BODY"; fi
      req GET "/dashboard"
      if [ "$HTTP" = "200" ] && body_has "any(x['title']=='Общий эфир сменён' and 'SMOKE_bc_$STAMP' in (x['detail'] or '') and 'до отмены' in x['detail'] for x in r['feed'])"; then
        ok "в журнале: «Общий эфир сменён … до отмены»"; else bad "в журнале нет записи о смене эфира"; fi
      if [ "$HTTP" = "200" ] && body_has "r['broadcast_on'] is True and r['broadcast_until'] is None"; then
        ok "дашборд: эфир до отмены — срока нет"; else bad "дашборд при эфире до отмены: HTTP $HTTP"; fi
      if [ -n "$BC_FEED" ]; then
        dreq "/schedule/minipc/$SID"
        if [ "$HTTP" = "200" ] && body_has "r['active_source']=='broadcast' and r['active_until'] is None"; then
          ok "лента экрана: эфир до отмены — active_until=null"
        else bad "лента экрана при эфире до отмены: HTTP $HTTP $(feed_brief)"; fi
      fi

      req POST "/broadcast/off"
      [ "$HTTP" = "200" ] && ok "общий эфир выключен" || bad "выключение эфира: HTTP $HTTP"
      req GET "/broadcast"
      if [ "$HTTP" = "200" ] && body_has "r['is_on'] is False and r['playlist_id'] is None and r['until_at'] is None"; then
        ok "GET /broadcast: эфир выключен, срока нет"; else bad "GET /broadcast после выключения: HTTP $HTTP $BODY"; fi
      req GET "/dashboard"
      if [ "$HTTP" = "200" ] && body_has "r['broadcast_on'] is False and r['broadcast_playlist'] is None and r['broadcast_until'] is None"; then
        ok "дашборд: эфир выключен"; else bad "дашборд после выключения эфира: HTTP $HTTP"; fi
      if [ -n "$BC_FEED" ]; then
        dreq "/schedule/minipc/$SID"
        if [ "$HTTP" = "200" ] && body_has "r['active_source']!='broadcast'"; then
          ok "после выключения лента экрана не отдаёт эфир"; else bad "лента после выключения эфира: HTTP $HTTP $(feed_brief)"; fi
        req GET "/network/live"
        if [ "$HTTP" = "200" ] && body_has "r['broadcast']['is_on'] is False and r['broadcast']['offline_screens']==0 and r['broadcast']['unplayable_screens']==0 and all(x['now']['kind']!='broadcast' and x['broadcast_skipped'] is False and x['after_broadcast'] is None for x in r['screens'])"; then
          ok "обзор сети после выключения: эфира нет ни на одном экране"; else bad "обзор сети после выключения эфира: HTTP $HTTP"; fi
        dreq "/files/list/$SID"
        if [ "$HTTP" = "200" ] && ! body_has "any(f['filename']=='$NOW_FILE' for f in r)"; then
          ok "после выключения ролика эфира нет в списке закачки"; else bad "после выключения ролик эфира в списке закачки: HTTP $HTTP"; fi
      fi
    fi
  fi
  bc_finish
  trap - EXIT
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

# ── 5б. Бегущая строка: запуск, правка, снятие (только тестовый экран) ────────
# Строки и шаблоны создаются ТОЛЬКО на SMOKE-экране (target "screens", его id): smoke идёт и на
# боевом сервере, а строка «на все экраны» засветила бы тест у посетителей. Что проверяем: запуск
# на 15 минут (срок считается по МСК: осталось ~900 с); обзор GET /tickers/overview (строка «на
# 1 экране», подтверждение, у экрана live_count = 1, группы, шаблоны); GET /tickers остаётся
# списком; старому агенту поставлена команда ticker_show; правка текста без срока — срок прежний,
# запись «Бегущая строка изменена»; снятие и повторное снятие (404); старое тело запроса
# (is_all=false, screen_ids, duration=60 → срок +60 с); ошибки по-русски (400); шаблон: создать,
# найти, переписать, удалить. Тестовый экран подаёт сигнал как агент 1.4.0 (ticker_state в heartbeat):
# «бежит» (confirm.running = 1) считается по его отчёту. Лента экрана содержит строку; «Стоп» её не
# снимает (она остаётся в обзоре и в ленте, экрану поставлены stop и ticker_hide, отчёт «остановлен»),
# «Показ» возвращает ticker_show самой новой строки экрана. На экране без живых строк «Стоп» и «Показ»
# ставят только stop / resume (лишняя команда в очереди старого агента задержала бы следующую).
# Оставшиеся строки и шаблоны SMOKE снимаются в «Уборке».

# drain_cmds — забрать и подтвердить ВСЕ команды тестового экрана (агент берёт по одной за пробуждение,
# здесь нужен весь список); печатает JSON-список [{id, type, payload}] в порядке очереди
drain_cmds(){
  local out="[" sep="" i R cid c
  for i in 1 2 3 4 5 6 7 8 9 10; do
    R=$($DC curl -s "$API/command/poll/$SID" -H "X-Token: $SCR_TOKEN" 2>/dev/null)
    cid=$(echo "$R" | python3 -c "import sys,json;c=json.load(sys.stdin).get('command');print(c['id'] if c else '')" 2>/dev/null)
    [ -n "$cid" ] || break
    c=$(echo "$R" | python3 -c "import sys,json;print(json.dumps(json.load(sys.stdin)['command']))" 2>/dev/null)
    out="$out$sep$c"; sep=","
    $DC curl -s -X POST "$API/command/ack/$cid" -H "X-Token: $SCR_TOKEN" >/dev/null 2>&1
  done
  echo "$out]"
}
# tk_heartbeat IDS STOPPED — heartbeat тестового экрана как у агента 1.4.0 (IDS — JSON-список id, STOPPED — true|false)
tk_heartbeat(){
  RESP=$($DC curl -s -w $'\n%{http_code}' -X POST "$API/heartbeat/$SID" -H "X-Token: $SCR_TOKEN" \
        -H 'Content-Type: application/json' \
        -d "{\"status\":\"online\",\"playing_file\":null,\"agent_version\":\"1.4.0\",\"ticker_state\":{\"ids\":$1,\"stopped\":$2,\"screen_off\":false}}" 2>/dev/null)
  HTTP="${RESP##*$'\n'}"; BODY="${RESP%$'\n'*}"
}
step "Бегущая строка: запуск, правка, снятие (только тестовый экран)"
TK_MSG="SMOKE_tk_$STAMP"; TK_ID=""; TK_OLD_ID=""; TPL_ID=""
if [ -n "${SID:-}" ] && [ -n "${SCR_TOKEN:-}" ]; then
  JSON_T='Content-Type: application/json'
  req POST "/tickers" -H "$JSON_T" \
      -d "{\"message\": \"$TK_MSG\", \"color\": \"#ffd34d\", \"speed\": \"medium\", \"target\": \"screens\", \"screen_ids\": [$SID], \"term\": {\"kind\": \"minutes\", \"minutes\": 15}}"
  TK_ID=$(body_get id)
  if [ "$HTTP" = "200" ] && [ -n "$TK_ID" ]; then ok "строка запущена на тестовом экране на 15 минут (id=$TK_ID)"; else bad "запуск строки: HTTP $HTTP $BODY"; TK_ID=""; fi

  # экран подаёт сигнал как агент 1.4.0 и сообщает, что строка у него бежит
  tk_heartbeat "[${TK_ID:-0}]" false
  [ "$HTTP" = "200" ] && ok "heartbeat экрана (агент 1.4.0) принят" || bad "heartbeat: HTTP $HTTP"

  if [ -n "$TK_ID" ]; then
    req GET "/tickers/overview"
    if [ "$HTTP" = "200" ] && body_has "any(t['id']==$TK_ID and t['message']=='$TK_MSG' and t['target']=='screens' and t['screen_ids']==[$SID] and t['screens_total']==1 and t['confirm']['total']==1 and t['confirm']['running']==1 and not t['confirm']['problems'] and 800 <= t['remaining_seconds'] <= 900 for t in r['tickers'])"; then
      ok "обзор: строка на 1 экране, по отчёту экрана она бежит, осталось ~15 минут"
    else
      bad "обзор: строки нет или она неверная, HTTP $HTTP: $(echo "$BODY" | python3 -c "import sys,json;print([(t['id'],t['screens_total'],t['confirm'],t['remaining_seconds']) for t in json.load(sys.stdin)['tickers'] if t['id']==$TK_ID])" 2>/dev/null)"
    fi
    if body_has "any(x['id']==$SID and x['live_count']==1 for x in r['screens']) and isinstance(r['groups'], list) and isinstance(r['templates'], list) and r['generated_at']"; then
      ok "обзор: у тестового экрана одна живая строка, группы и шаблоны на месте"
    else
      bad "обзор: у экрана нет live_count=1 или нет групп/шаблонов"
    fi
    req GET "/tickers"
    if [ "$HTTP" = "200" ] && body_has "isinstance(r, list) and any(t['id']==$TK_ID and t['is_all'] is False and t['target']=='screens' and t['until_at'] and t['started_by'] for t in r)"; then
      ok "GET /tickers — по-прежнему список, строка в нём с target и сроком"
    else
      bad "GET /tickers: HTTP $HTTP"
    fi
    dreq "/command/poll/$SID"
    if [ "$HTTP" = "200" ] && body_has "(r['command'] or {}).get('type')=='ticker_show' and r['command']['payload'].get('message')=='$TK_MSG' and 800 <= r['command']['payload'].get('duration', 0) <= 900"; then
      ok "старому агенту поставлена команда ticker_show (с оставшимся сроком)"
    else
      bad "команда ticker_show экрану не поставлена, HTTP $HTTP: $BODY"
    fi

    # лента экрана — как её читает агент 1.4.0: строка с видом и сроком
    dreq "/schedule/minipc/$SID"
    if [ "$HTTP" = "200" ] && body_has "any(t['id']==$TK_ID and t['message']=='$TK_MSG' and t['color']=='#ffd34d' and t['speed']=='medium' and t['until_at'] for t in r['tickers'])"; then
      ok "лента экрана: бегущая строка на месте, с видом и сроком"
    else
      bad "в ленте экрана нет строки или она неверная, HTTP $HTTP: $(echo "$BODY" | python3 -c "import sys,json;print(json.load(sys.stdin).get('tickers'))" 2>/dev/null)"
    fi

    # «Стоп» строку не снимает. Очередь команд экрана сначала разбираем (там ticker_show от запуска),
    # чтобы дальше видеть только то, что поставят «Стоп» и «Показ»
    drain_cmds >/dev/null
    req POST "/command/stop/$SID"
    [ "$HTTP" = "200" ] && ok "«Стоп» тестовому экрану принят" || bad "«Стоп»: HTTP $HTTP $BODY"
    BODY=$(drain_cmds)
    if body_has "[c['type'] for c in r]==['stop','ticker_hide']"; then
      ok "«Стоп»: экрану поставлены stop и ticker_hide (строку гасят только на нём)"
    else
      bad "«Стоп»: ждали stop и ticker_hide, в очереди: $BODY"
    fi
    tk_heartbeat "[]" true          # агент 1.4.0 после «Стоп»: показ остановлен, строки не бегут
    req GET "/tickers/overview"
    if [ "$HTTP" = "200" ] && body_has "any(t['id']==$TK_ID and t['confirm']['running']==0 and [p['reason'] for p in t['confirm']['problems']]==['stopped'] and t['confirm']['problems'][0]['screen_id']==$SID for t in r['tickers'])"; then
      ok "после «Стоп» строка из обзора не исчезла, на экране причина «остановлен» (с id экрана)"
    else
      bad "после «Стоп» строка пропала из обзора или причина не «остановлен», HTTP $HTTP"
    fi
    dreq "/schedule/minipc/$SID"
    if [ "$HTTP" = "200" ] && body_has "any(t['id']==$TK_ID for t in r['tickers'])"; then
      ok "после «Стоп» строка осталась в ленте экрана"
    else
      bad "после «Стоп» строки нет в ленте экрана, HTTP $HTTP"
    fi
    req POST "/command/resume/$SID"
    [ "$HTTP" = "200" ] && ok "«Показ» тестовому экрану принят" || bad "«Показ»: HTTP $HTTP $BODY"
    BODY=$(drain_cmds)
    if body_has "[c['type'] for c in r]==['resume','ticker_show'] and r[1]['payload']['message']=='$TK_MSG' and 800 <= r[1]['payload']['duration'] <= 900"; then
      ok "«Показ»: экрану поставлены resume и ticker_show той же строки (с оставшимся сроком)"
    else
      bad "«Показ»: ждали resume и ticker_show, в очереди: $BODY"
    fi
    tk_heartbeat "[$TK_ID]" false   # показ возобновлён — строка снова бежит
    req GET "/tickers/overview"
    if [ "$HTTP" = "200" ] && body_has "any(t['id']==$TK_ID and t['confirm']['running']==1 and not t['confirm']['problems'] for t in r['tickers'])"; then
      ok "после «Показ» строка снова бежит"
    else
      bad "после «Показ» строка не бежит по отчёту экрана, HTTP $HTTP"
    fi

    # правка: текст и вид новые, срок не присылаем — остаётся прежний
    req PUT "/tickers/$TK_ID" -H "$JSON_T" \
        -d "{\"message\": \"${TK_MSG}_edit\", \"color\": \"#ff6b5e\", \"speed\": \"fast\", \"target\": \"screens\", \"screen_ids\": [$SID]}"
    [ "$HTTP" = "200" ] && ok "строка изменена" || bad "правка строки: HTTP $HTTP $BODY"
    req GET "/tickers/overview"
    if [ "$HTTP" = "200" ] && body_has "any(t['id']==$TK_ID and t['message']=='${TK_MSG}_edit' and t['color']=='#ff6b5e' and t['speed']=='fast' and t['changed_by'] and 780 <= t['remaining_seconds'] <= 900 for t in r['tickers'])"; then
      ok "после правки: новый текст и вид, срок прежний"
    else
      bad "после правки обзор неверный, HTTP $HTTP"
    fi
    # команды строки — состояние, а не события: вторая правка гасит ожидающий ticker_show первой, в очереди остаётся одна
    # (иначе старый агент по WS тратил бы на устаревшую команду будильник следующей)
    req PUT "/tickers/$TK_ID" -H "$JSON_T" \
        -d "{\"message\": \"${TK_MSG}_edit\", \"color\": \"#ff6b5e\", \"speed\": \"fast\", \"target\": \"screens\", \"screen_ids\": [$SID]}"
    [ "$HTTP" = "200" ] && ok "повторная правка принята" || bad "повторная правка: HTTP $HTTP $BODY"
    BODY=$(drain_cmds)
    if body_has "[c['type'] for c in r]==['ticker_show'] and r[0]['payload']['message']=='${TK_MSG}_edit'"; then
      ok "две правки подряд: в очереди одна команда ticker_show (устаревшая погашена)"
    else
      bad "две правки подряд: ждали одну ticker_show, в очереди: $BODY"
    fi
    req GET "/audit?limit=40"
    if [ "$HTTP" = "200" ] && body_has "any(x['event_type']=='ticker' and x['title']=='Бегущая строка изменена' and '${TK_MSG}_edit' in (x['detail'] or '') for x in r)"; then
      ok "в журнале: «Бегущая строка изменена»"
    else
      bad "в журнале нет записи об изменении строки"
    fi
    if body_has "any(x['event_type']=='ticker' and x['title']=='Бегущая строка запущена' and '$TK_MSG' in (x['detail'] or '') for x in r)"; then
      ok "в журнале: «Бегущая строка запущена»"
    else
      bad "в журнале нет записи о запуске строки"
    fi

    # снятие
    req DELETE "/tickers/$TK_ID"
    [ "$HTTP" = "200" ] && ok "строка снята" || bad "снятие строки: HTTP $HTTP $BODY"
    req GET "/tickers/overview"
    if [ "$HTTP" = "200" ] && body_has "not any(t['id']==$TK_ID for t in r['tickers']) and all(x['live_count']==0 for x in r['screens'] if x['id']==$SID)"; then
      ok "после снятия строки в обзоре нет, у тестового экрана 0 строк"
    else
      bad "после снятия строка осталась в обзоре, HTTP $HTTP"
    fi
    req GET "/audit?limit=40"
    if [ "$HTTP" = "200" ] && body_has "any(x['event_type']=='ticker' and x['title']=='Бегущая строка снята' and '${TK_MSG}_edit' in (x['detail'] or '') for x in r)"; then
      ok "в журнале: «Бегущая строка снята»"
    else
      bad "в журнале нет записи о снятии строки"
    fi
    req DELETE "/tickers/$TK_ID"
    if [ "$HTTP" = "404" ] && body_has "r['detail']=='Бегущая строка не найдена'"; then ok "повторное снятие — 404 «Бегущая строка не найдена»"; else bad "повторное снятие: ждали 404, HTTP $HTTP $BODY"; fi
    req PUT "/tickers/$TK_ID" -H "$JSON_T" -d "{\"message\": \"${TK_MSG}_late\", \"target\": \"screens\", \"screen_ids\": [$SID]}"
    [ "$HTTP" = "404" ] && ok "правка снятой строки — 404" || bad "правка снятой строки: ждали 404, HTTP $HTTP"
    # правка без «где» не должна тихо делать строку «на все экраны»: 400 (проверка тела идёт раньше поиска строки)
    req PUT "/tickers/$TK_ID" -H "$JSON_T" -d "{\"message\": \"${TK_MSG}_late\"}"
    if [ "$HTTP" = "400" ] && body_has "r['detail']=='Укажите, где показывать строку'"; then ok "правка без «где» — 400 «Укажите, где показывать строку»"; else bad "правка без «где»: ждали 400, HTTP $HTTP $BODY"; fi

    # строки сняты: «Стоп» и «Показ» тестовому экрану ставят только stop / resume, без ticker_hide / ticker_show.
    # Чужая строка «на все экраны» (боевой сервер) была бы живой и у тестового экрана — тогда проверку пропускаем
    req GET "/tickers/overview"
    if body_has "all(x['live_count']==0 for x in r['screens'] if x['id']==$SID)"; then
      drain_cmds >/dev/null
      req POST "/command/stop/$SID"
      BODY=$(drain_cmds)
      if body_has "[c['type'] for c in r]==['stop']"; then ok "«Стоп» экрану без строк: только stop"; else bad "«Стоп» без строк: ждали только stop, в очереди: $BODY"; fi
      req POST "/command/resume/$SID"
      BODY=$(drain_cmds)
      if body_has "[c['type'] for c in r]==['resume']"; then ok "«Показ» экрану без строк: только resume"; else bad "«Показ» без строк: ждали только resume, в очереди: $BODY"; fi
    else
      echo "  - у тестового экрана идёт чужая строка «на все экраны»: проверку «Стоп» без строк пропускаем"
    fi
    TK_ID=""
  fi

  # старое тело запроса: is_all=false, screen_ids, duration=60 (секунды) → срок +60 с
  req POST "/tickers" -H "$JSON_T" \
      -d "{\"message\": \"${TK_MSG}_old\", \"is_all\": false, \"screen_ids\": [$SID], \"duration\": 60}"
  TK_OLD_ID=$(body_get id)
  if [ "$HTTP" = "200" ] && [ -n "$TK_OLD_ID" ]; then
    req GET "/tickers/overview"
    if [ "$HTTP" = "200" ] && body_has "any(t['id']==$TK_OLD_ID and t['target']=='screens' and t['screen_ids']==[$SID] and 1 <= t['remaining_seconds'] <= 60 and abs((__import__('datetime').datetime.fromisoformat(t['until_at']) - __import__('datetime').datetime.fromisoformat(t['started_at'])).total_seconds() - 60) < 1 for t in r['tickers'])"; then
      ok "старое тело (is_all=false, duration=60): срок — запуск + 60 с"
    else
      bad "старое тело: срок не запуск + 60 с, HTTP $HTTP"
    fi
    req DELETE "/tickers/$TK_OLD_ID"
    if [ "$HTTP" = "200" ]; then ok "строка по старому телу снята"; TK_OLD_ID=""; else bad "снятие строки по старому телу: HTTP $HTTP"; fi
  else
    bad "старое тело запроса не принято: HTTP $HTTP $BODY"; TK_OLD_ID=""
  fi

  # ошибки — 400 по-русски
  req POST "/tickers" -H "$JSON_T" -d "{\"message\": \"   \", \"target\": \"screens\", \"screen_ids\": [$SID]}"
  if [ "$HTTP" = "400" ] && body_has "any('А' <= c <= 'я' for c in r['detail'])"; then ok "пустой текст — 400 с русским пояснением"; else bad "пустой текст: ждали 400, HTTP $HTTP $BODY"; fi
  req POST "/tickers" -H "$JSON_T" -d "{\"message\": \"a\\u0000b\", \"target\": \"screens\", \"screen_ids\": [$SID]}"
  if [ "$HTTP" = "400" ] && body_has "'недопустим' in r['detail']"; then ok "NUL в тексте — 400 «недопустимые символы», не 500"; else bad "NUL в тексте: ждали 400, HTTP $HTTP $BODY"; fi
  req POST "/tickers" -H "$JSON_T" -d "{\"message\": \"$TK_MSG\", \"target\": \"screens\", \"screen_ids\": [$SID], \"term\": {\"kind\": \"minutes\", \"minutes\": 0}}"
  if [ "$HTTP" = "400" ]; then ok "срок «0 минут» — 400"; else bad "срок 0 минут: ждали 400, HTTP $HTTP $BODY"; fi
  req POST "/tickers" -H "$JSON_T" -d "{\"message\": \"$TK_MSG\", \"target\": \"screens\", \"screen_ids\": [2000000000]}"
  if [ "$HTTP" = "400" ] && body_has "'обновите страницу' in r['detail']"; then ok "несуществующий экран — 400 «обновите страницу», не 500"; else bad "несуществующий экран: ждали 400, HTTP $HTTP $BODY"; fi

  # шаблоны
  req POST "/ticker-templates" -H "$JSON_T" \
      -d "{\"message\": \"SMOKE_tpl_$STAMP\", \"color\": \"#ffffff\", \"speed\": \"slow\", \"term\": {\"kind\": \"minutes\", \"minutes\": 30}}"
  TPL_ID=$(body_get id)
  if [ "$HTTP" = "200" ] && [ -n "$TPL_ID" ]; then ok "шаблон создан (id=$TPL_ID)"; else bad "создание шаблона: HTTP $HTTP $BODY"; TPL_ID=""; fi
  if [ -n "$TPL_ID" ]; then
    req GET "/ticker-templates"
    if [ "$HTTP" = "200" ] && body_has "any(t['id']==$TPL_ID and t['message']=='SMOKE_tpl_$STAMP' and t['color']=='#ffffff' and t['speed']=='slow' and t['term']=={'kind':'minutes','minutes':30} for t in r)"; then
      ok "шаблон найден в списке: текст, вид и срок на месте"
    else
      bad "шаблона нет в списке или он неверный, HTTP $HTTP"
    fi
    req PUT "/ticker-templates/$TPL_ID" -H "$JSON_T" -d "{\"message\": \"SMOKE_tpl_$STAMP\", \"term\": {\"kind\": \"hour\", \"hour\": 24}}"
    [ "$HTTP" = "200" ] && ok "шаблон переписан (до 24:00)" || bad "правка шаблона: HTTP $HTTP $BODY"
    req GET "/tickers/overview"
    if [ "$HTTP" = "200" ] && body_has "any(t['id']==$TPL_ID and t['term']=={'kind':'hour','hour':24} and t['color']=='#ffd34d' for t in r['templates'])"; then
      ok "обзор отдаёт шаблон с новым сроком"
    else
      bad "обзор: шаблон не найден или срок не тот, HTTP $HTTP"
    fi
    req DELETE "/ticker-templates/$TPL_ID"
    [ "$HTTP" = "200" ] && ok "шаблон удалён" || bad "удаление шаблона: HTTP $HTTP"
    req GET "/ticker-templates"
    if [ "$HTTP" = "200" ] && body_has "not any(t['id']==$TPL_ID for t in r)"; then ok "удалённого шаблона в списке нет"; else bad "шаблон остался в списке"; fi
    req DELETE "/ticker-templates/$TPL_ID"
    [ "$HTTP" = "404" ] && ok "повторное удаление шаблона — 404" || bad "повторное удаление шаблона: ждали 404, HTTP $HTTP"
    TPL_ID=""
  fi
else
  bad "нет экрана или его токена — проверку бегущей строки провести не на чем"
fi

# ── 5в. Бэкапы и архивы диагностики — только по входу ────────────────────────
# Бэкап — полный дамп БД (хеши паролей, токены экранов, счета), номера идут подряд:
# без входа сервер обязан отвечать 401 на ЛЮБОЙ номер (и не выдавать, есть ли он),
# токен экрана (X-Token) эти файлы тоже не открывает. По входу: последний бэкап
# скачивается (200), если он есть; иначе — 404 на несуществующий номер. Архивов
# диагностики у SMOKE-экрана нет, поэтому по входу ждём 404.
step "Бэкапы и диагностика: скачивание только по входу"
# code_as PATH [curl args] — код ответа на GET с указанными заголовками (тело не нужно)
code_as(){ local p="$1"; shift; $DC curl -s -o /dev/null -w '%{http_code}' "$API$p" "$@" 2>/dev/null; }
req GET "/backups"
BKID=$(echo "$BODY" | python3 -c "import sys,json; l=json.load(sys.stdin); print(l[0]['id'] if l else '')" 2>/dev/null)
BK="/backups/${BKID:-2147483000}/download"
DG="/diagnostics/2147483000/download"
C=$(code_as "$BK"); [ "$C" = "401" ] && ok "бэкап без входа — 401" || bad "бэкап без входа: HTTP $C (дамп БД открыт!)"
C=$(code_as "$DG"); [ "$C" = "401" ] && ok "архив диагностики без входа — 401" || bad "диагностика без входа: HTTP $C"
C=$(code_as "$BK" -H "X-Token: ${SCR_TOKEN:-x}"); [ "$C" = "401" ] && ok "бэкап по токену экрана — 401" || bad "бэкап по токену экрана: HTTP $C"
C=$(code_as "$DG" -H "X-Token: ${SCR_TOKEN:-x}"); [ "$C" = "401" ] && ok "диагностика по токену экрана — 401" || bad "диагностика по токену экрана: HTTP $C"
C=$(code_as "$BK" -H "Authorization: Bearer $TOKEN")
if [ -n "$BKID" ]; then
  [ "$C" = "200" ] && ok "бэкап по входу скачивается (id=$BKID)" || bad "бэкап по входу: HTTP $C"
else
  [ "$C" = "404" ] && ok "бэкапов нет — по входу 404 на несуществующий номер" || bad "бэкап по входу: HTTP $C"
fi
C=$(code_as "$DG" -H "Authorization: Bearer $TOKEN"); [ "$C" = "404" ] && ok "диагностика по входу — 404 на несуществующий номер" || bad "диагностика по входу: HTTP $C"

# ── 5г. Файлы медиатеки — только по входу ────────────────────────────────────
# Номера роликов идут подряд, имена файлов угадываются: без входа и ролик
# (/api/files/download), и миниатюра, и /media/<имя> — 401 на любой номер.
# Экран (X-Token) качает одобренный ролик с докачкой: Range → 206, файл отдаёт
# nginx по X-Accel-Redirect; ролик на модерации для экрана «не найден» (404),
# миниатюры и /media/ токен экрана не открывает. Панель: по входу (Bearer) и по
# cookie ds_media (её ставит POST /session/media; так ходят <img> и <video>) —
# 200, с Range — 206; токен входа вместо cookie и снятая cookie — 401.
# Запросы идут через nginx (http://nginx изнутри ds_api): Range и /media/ живут там.
step "Файлы медиатеки: только по входу"
NGX="http://nginx"
# ncode PATH [curl args] — код ответа nginx (тело не нужно); nget — код и размер тела
ncode(){ local p="$1"; shift; $DC curl -s -o /dev/null -w '%{http_code}' "$NGX$p" "$@" 2>/dev/null; }
nget(){ local p="$1"; shift; $DC curl -s -o /dev/null -w '%{http_code} %{size_download}' "$NGX$p" "$@" 2>/dev/null; }
if [ -n "${MID:-}" ] && [ -n "${MID2:-}" ] && [ -n "${SCR_TOKEN:-}" ]; then
  req GET "/media/$MID"; MFILE=$(body_get filename); MSIZE=$(body_get filesize)
  MURL="/media/$(python3 -c 'import sys,urllib.parse;print(urllib.parse.quote(sys.argv[1]))' "$MFILE")"
  DL="/api/files/download/$MID"; TH="/api/media/$MID/thumbnail"
  for P in "$DL" "/api/files/download/2147483000" "$TH" "$MURL"; do
    C=$(ncode "$P"); [ "$C" = "401" ] && ok "без входа $P — 401" || bad "без входа $P: HTTP $C (файл открыт!)"
  done
  # /media/ открыт, а API закрыт — nginx работает со старой копией nginx.conf
  [ "$(ncode "$MURL")" = "200" ] && echo "    └─ после обновления nginx.conf: docker compose up -d --force-recreate nginx"
  C=$(ncode "$MURL" -I); [ "$C" = "401" ] && ok "без входа HEAD /media/ — 401" || bad "без входа HEAD /media/: HTTP $C"
  C=$(ncode "$DL" -H "Authorization: Bearer not-a-jwt"); [ "$C" = "401" ] && ok "поддельный токен — 401" || bad "поддельный токен: HTTP $C"
  C=$(ncode "$MURL" -H "Cookie: ds_media=$TOKEN"); [ "$C" = "401" ] && ok "токен входа вместо cookie — 401" || bad "токен входа вместо cookie: HTTP $C"
  C=$(ncode "$TH" -H "X-Token: $SCR_TOKEN"); [ "$C" = "401" ] && ok "миниатюра по токену экрана — 401" || bad "миниатюра по токену экрана: HTTP $C"
  C=$(ncode "$MURL" -H "X-Token: $SCR_TOKEN"); [ "$C" = "401" ] && ok "/media/ по токену экрана — 401" || bad "/media/ по токену экрана: HTTP $C"

  R=$(nget "$DL" -H "X-Token: $SCR_TOKEN"); [ "$R" = "200 $MSIZE" ] && ok "экран качает одобренный ролик ($MSIZE байт)" || bad "экран качает ролик: ждали «200 $MSIZE», получили «$R»"
  R=$(nget "$DL" -H "X-Token: $SCR_TOKEN" -H "Range: bytes=100-"); [ "$R" = "206 $((MSIZE - 100))" ] \
    && ok "докачка экрана: Range с 100-го байта — 206, $((MSIZE - 100)) байт" || bad "докачка экрана: ждали «206 $((MSIZE - 100))», получили «$R»"
  C=$(ncode "/api/files/download/$MID2" -H "X-Token: $SCR_TOKEN"); [ "$C" = "404" ] && ok "ролик на модерации экрану не отдаётся (404)" || bad "ролик на модерации экрану: HTTP $C"

  R=$(nget "$DL" -H "Authorization: Bearer $TOKEN"); [ "$R" = "200 $MSIZE" ] && ok "по входу ролик скачивается" || bad "по входу ролик: «$R»"
  C=$(ncode "/api/files/download/$MID2" -H "Authorization: Bearer $TOKEN"); [ "$C" = "200" ] && ok "по входу и ролик на модерации (для модерации)" || bad "по входу ролик на модерации: HTTP $C"
  C=$(ncode "$TH" -H "Authorization: Bearer $TOKEN"); [ "$C" = "200" ] && ok "по входу миниатюра — 200" || bad "по входу миниатюра: HTTP $C"
  C=$(ncode "$MURL" -I -H "Authorization: Bearer $TOKEN"); [ "$C" = "200" ] && ok "по входу HEAD /media/<имя> — 200" || bad "по входу HEAD /media/: HTTP $C"

  JAR="/tmp/smoke_media_$STAMP.jar"
  C=$($DC curl -s -o /dev/null -w '%{http_code}' -c "$JAR" -X POST "$NGX/api/session/media" -H "Authorization: Bearer $TOKEN" 2>/dev/null)
  [ "$C" = "200" ] && ok "cookie файлов выдана (POST /session/media)" || bad "cookie файлов: HTTP $C"
  R=$(nget "$MURL" -b "$JAR" -H "Range: bytes=0-99"); [ "$R" = "206 100" ] && ok "по cookie /media/<имя> с Range — 206 (перемотка в просмотре)" || bad "по cookie /media/: ждали «206 100», получили «$R»"
  C=$(ncode "$TH" -b "$JAR"); [ "$C" = "200" ] && ok "по cookie миниатюра — 200" || bad "по cookie миниатюра: HTTP $C"
  C=$(ncode "$DL" -b "$JAR"); [ "$C" = "200" ] && ok "по cookie «Скачать» — 200" || bad "по cookie «Скачать»: HTTP $C"
  $DC curl -s -o /dev/null -b "$JAR" -c "$JAR" -X DELETE "$NGX/api/session/media" 2>/dev/null
  C=$(ncode "$MURL" -b "$JAR"); [ "$C" = "401" ] && ok "снятая cookie (выход) — 401" || bad "снятая cookie: HTTP $C"
  $DC rm -f "$JAR" 2>/dev/null
else
  bad "нет роликов или токена экрана — проверку файлов медиатеки провести не на чем"
fi

# ── 5д. Копия на флешку ──────────────────────────────────────────────────────
# Копия сервера (запертая паролем) и tar-архивы роликов отдаются суперадмину по ОДНОРАЗОВОЙ ссылке:
# браузер качает её без заголовка Authorization, поэтому «входом» служит сам токен.
# Без флага (ничего не меняет, безопасно и на боевом сервере): статус и выдача ссылок без входа и по
# токену экрана — 401; по входу статус — 200 со всеми ключами, без пароля, «ok/warn» согласован с
# «копия сегодня и новых роликов нет»; proxy_ok напрямую в API — false, через nginx — true (иначе nginx
# со старым nginx.conf); ссылка скачивания с чужим или пустым кодом — 404; неверный объём роликов — 400.
# С SMOKE_ALLOW_OFFSITE=1 (только стенд dsdev: меняет пароль на копию и отметки «скачано»; до первого
# изменения проверяется метка compose-проекта контейнера ds_api, не dsdev — отказ и провал проверки):
# пароль на копию сбрасывается на сервере и задаётся заново (неверный пароль входа → 403, не совпали → 400, слабый → 400,
# смена без текущего → 403); копия без пароля → 409, с неверным паролем входа → 403; копия собирается,
# ссылка отдаёт НЕсжатый tar ровно из трёх файлов (инструкция с командой расшифровки как на сервере,
# запертая часть, sha256), сумма сходится, запертая часть открывается openssl паролем на копию и не открывается чужим, внутри база, программа с .env и
# опись; дамп из копии заливается во временную базу smoke_restore_<время> на ds_postgres (gunzip | psql
# -v ON_ERROR_STOP=1, как в ВОССТАНОВЛЕНИЕ.txt), users в ней = users из MANIFEST.txt, затем база удаляется
# (и при обрыве); второй заход по ссылке → 404; «скачано» попало в статус и в журнал (пароля в журнале нет);
# ролики «новые» — tar с манифестом и файлом тестового ролика («нет файла» в манифесте = missing из ответа),
# после скачивания «новых» нет (409), «все» — тоже скачиваются; напоминание в итоге «ok». В конце пароль на копию снова сбрасывается (и при обрыве
# скрипта): стенд остаётся без пароля. Отметка «последняя копия» и «водяной знак» роликов остаются.
step "Копия на флешку: доступ и одноразовая ссылка"
JSON_O='Content-Type: application/json'
# oj ключ=значение … — JSON-тело из пар: пароли с кавычками и кириллица тело не ломают
oj(){ python3 -c "import sys,json; print(json.dumps(dict(a.split('=',1) for a in sys.argv[1:]), ensure_ascii=False))" "$@"; }
# detail_has ТЕКСТ — в поле detail ответа об ошибке есть этот текст
detail_has(){ local d; d=$(body_get detail); [[ "$d" == *"$1"* ]]; }
C=$(code_as "/offsite/status"); [ "$C" = "401" ] && ok "статус копии без входа — 401" || bad "статус копии без входа: HTTP $C"
C=$(code_as "/offsite/status" -H "X-Token: ${SCR_TOKEN:-x}"); [ "$C" = "401" ] && ok "статус копии по токену экрана — 401" || bad "статус копии по токену экрана: HTTP $C"
for OP in password copy media; do
  C=$(code_as "/offsite/$OP" -X POST -H "$JSON_O" -d '{}')
  [ "$C" = "401" ] && ok "POST /offsite/$OP без входа — 401" || bad "POST /offsite/$OP без входа: HTTP $C"
done
req GET "/offsite/status"
if [ "$HTTP" = "200" ] && body_has "set(r)=={'password','copy','media','state','project_mounted','proxy_ok'} and set(r['password'])=={'set','set_at','set_by','needs_reset'} and set(r['copy'])=={'last_at','last_by','last_file','last_bytes','today'} and set(r['media'])=={'new_count','new_bytes','all_count','all_bytes','last_at','last_by'} and isinstance(r['password']['set'],bool) and isinstance(r['project_mounted'],bool)"; then
  ok "статус копии по входу: все ключи на месте, пароля в ответе нет"; else bad "статус копии: HTTP $HTTP, ключи не те"; fi
if body_has "r['state'] in ('ok','warn') and (r['state']=='ok')==(r['copy']['today'] and r['media']['new_count']==0)"; then
  ok "напоминание «ok/warn» согласовано: ok только когда копия сегодня и новых роликов нет"; else bad "напоминание не сходится с «копия сегодня» и «новые ролики»"; fi
# proxy_ok: метку X-DS-Offsite ставит только location /api/offsite/ в nginx.conf. Напрямую в API (как ходит
# этот скрипт) — false; через nginx — true, иначе nginx живёт со старой копией конфига: ответы буферизуются
# («скачано» раньше, чем файл дошёл) и долгая сборка копии упирается в 504
body_has "r['proxy_ok'] is False" && ok "статус копии напрямую в API (мимо nginx) — proxy_ok=false" || bad "статус напрямую в API: proxy_ok не false"
NB=$($DC curl -s "$NGX/api/offsite/status" -H "Authorization: Bearer $TOKEN" 2>/dev/null)
if echo "$NB" | python3 -c "import sys,json; sys.exit(0 if json.load(sys.stdin).get('proxy_ok') is True else 1)" 2>/dev/null; then
  ok "статус копии через nginx — proxy_ok=true (локация /api/offsite/ на месте)"
else
  bad "через nginx proxy_ok не true — nginx со старым nginx.conf"
  echo "    └─ docker compose up -d --force-recreate nginx"
fi
C=$(code_as "/offsite/download?t=xxx"); [ "$C" = "404" ] && ok "ссылка с чужим кодом — 404 (входа не требует)" || bad "ссылка с чужим кодом: HTTP $C"
C=$(code_as "/offsite/download"); [ "$C" = "404" ] && ok "ссылка без кода — 404" || bad "ссылка без кода: HTTP $C"
req POST "/offsite/media" -H "$JSON_O" -d '{"scope":"zzz"}'
[ "$HTTP" = "400" ] && ok "ролики: неизвестный объём — 400" || bad "ролики: неизвестный объём: HTTP $HTTP (ждали 400)"

# Скрипты проверки архивов — внутри ds_api (там есть python3, tarfile и openssl)
OFF_PY_COPY='
import sys, tarfile, hashlib, subprocess, tempfile, os, shutil, re
path, pw, instr, enc_name, sha_name, decrypt_cmd, dump_out = sys.argv[1:8]
try:
    t = tarfile.open(path, "r:")      # только несжатый tar: сжатый gzip-ом здесь не открывается
except Exception as e:
    print("архив не открывается как несжатый tar: " + ascii(str(e))); sys.exit(0)
names = sorted(m.name for m in t.getmembers())
if names != sorted([instr, enc_name, sha_name]):
    print("состав tar: " + ascii(names)); sys.exit(0)
d = tempfile.mkdtemp()
try:
    h = hashlib.sha256()
    enc = os.path.join(d, "enc")
    with t.extractfile(enc_name) as src, open(enc, "wb") as dst:
        while True:
            b = src.read(1 << 20)
            if not b: break
            h.update(b); dst.write(b)
    if t.extractfile(sha_name).read().decode("ascii").split() != [h.hexdigest(), enc_name]:
        print("sha256 запертой части не сошлась"); sys.exit(0)
    if open(enc, "rb").read(8) != b"Salted__":
        print("запертая часть не в формате openssl"); sys.exit(0)
    text = t.extractfile(instr).read().decode("utf-8")
    if pw in text: print("пароль на копию лежит в инструкции"); sys.exit(0)
    if decrypt_cmd not in text: print("в инструкции нет команды расшифровки как на сервере: " + decrypt_cmd); sys.exit(0)
    def dec(p, out):
        return subprocess.run(["openssl", "enc", "-d", "-aes-256-cbc", "-pbkdf2", "-md", "sha256", "-iter", "200000",
                               "-pass", "pass:" + p, "-in", enc, "-out", out], capture_output=True).returncode
    bad = os.path.join(d, "bad")
    if dec(pw + "x", bad) == 0 and open(bad, "rb").read(2) == b"\x1f\x8b":
        print("копия открылась чужим паролем"); sys.exit(0)
    good = os.path.join(d, "inner.tar.gz")
    if dec(pw, good) != 0: print("копия не открылась паролем на копию"); sys.exit(0)
    inner = tarfile.open(good, "r:gz")
    names = inner.getnames()
    if "MANIFEST.txt" not in names: print("внутри нет MANIFEST.txt"); sys.exit(0)
    dumps = [n for n in names if n.startswith("db/backup_") and n.endswith(".sql.gz")]
    if len(dumps) != 1: print("внутри не один дамп базы: " + ascii(dumps)); sys.exit(0)
    if "program/.env" not in names: print("внутри нет program/.env"); sys.exit(0)
    # дамп — наружу (его зальют во временную базу), контрольное число users — из описи
    with inner.extractfile(dumps[0]) as src, open(dump_out, "wb") as dst:
        shutil.copyfileobj(src, dst)
    m = re.search(r"^\s*users:\s*(\d+)\b", inner.extractfile("MANIFEST.txt").read().decode("utf-8"), re.M)
    if not m: print("в MANIFEST.txt нет контрольного числа users"); sys.exit(0)
    print("OK %d %s" % (len(names), m.group(1)))
finally:
    shutil.rmtree(d, ignore_errors=True)
'
OFF_PY_MEDIA='
import sys, tarfile
path, fname, title = sys.argv[1:4]
try:
    t = tarfile.open(path, "r:")      # только несжатый tar: сжатый gzip-ом здесь не открывается
except Exception as e:
    print("архив не открывается как несжатый tar: " + ascii(str(e))); sys.exit(0)
names = t.getnames()
man = "media/MANIFEST_media.txt"
if man not in names: print("нет MANIFEST_media.txt"); sys.exit(0)
text = t.extractfile(man).read().decode("utf-8")
lines = [l for l in text.splitlines() if l.strip()]
if title not in text: print("в манифесте нет названия тестового ролика"); sys.exit(0)
if "media/" + fname not in names: print("в архиве нет файла тестового ролика"); sys.exit(0)
print("OK %d %d %d" % (len([n for n in names if n != man]), len(lines), sum(1 for l in lines if l.endswith("нет файла"))))
'

# Охрана: шаг с флагом стирает пароль на копию и сдвигает «водяной знак» роликов (его reset не откатывает),
# а имя контейнера ds_api у боевого сервера такое же, как у стенда. Поэтому до первого изменения состояния
# проверяем метку compose-проекта контейнера: только dsdev (bash dev_stand.sh up) — всё остальное отказ.
off_on_dev_stand(){ [ "$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project"}}' ds_api 2>/dev/null)" = "dsdev" ]; }
if [ "${SMOKE_ALLOW_OFFSITE:-}" != "1" ]; then
  echo "  – копия и ролики на флешку (пароль, сборка, скачивание) — пропущено (задайте SMOKE_ALLOW_OFFSITE=1 на стенде)"
elif ! off_on_dev_stand; then
  bad "шаг копии на флешку с флагом — только на стенде dsdev (bash dev_stand.sh up); пропущено"
else
  OFF_PW0="SMOKE-copy-pass-0"; OFF_PW1="SMOKE-copy-pass-1"
  # команда расшифровки — как её знает сервер (OPENSSL_DECRYPT_CMD); в инструкции на флешке она должна стоять слово в слово
  OFF_DECRYPT=$($DC python -c "import sys; sys.path.insert(0, '/app'); from offsite_crypto import OPENSSL_DECRYPT_CMD; print(OPENSSL_DECRYPT_CMD)" 2>/dev/null)
  [[ "$OFF_DECRYPT" == *"-aes-256-cbc -pbkdf2 -md sha256 -iter 200000"* ]] && ok "команда расшифровки взята с сервера: $OFF_DECRYPT" \
    || bad "команду расшифровки с сервера прочитать не удалось или она другая: «$OFF_DECRYPT»"
  OFF_DIR="/tmp/smoke_offsite_$STAMP"; OFF_DIRTY=1
  # Временная база для заливки дампа из копии (как шаг 4 ВОССТАНОВЛЕНИЕ.txt, но не в рабочую базу стенда).
  # Пользователь и база — из окружения контейнера ds_postgres; psql внутри контейнера ходит без пароля.
  OFF_SCRATCH="smoke_restore_$STAMP"; OFF_SCRATCH_MADE=0
  OFF_PGU=$(docker exec ds_postgres printenv POSTGRES_USER 2>/dev/null); OFF_PGD=$(docker exec ds_postgres printenv POSTGRES_DB 2>/dev/null)
  off_pg(){ docker exec ds_postgres psql -v ON_ERROR_STOP=1 -U "$OFF_PGU" -d "$OFF_PGD" -q "$@"; }
  off_drop_scratch(){ # временную базу — удалить (и при обрыве скрипта)
    if [ "$OFF_SCRATCH_MADE" = "1" ]; then
      OFF_SCRATCH_MADE=0
      off_pg -c "DROP DATABASE IF EXISTS $OFF_SCRATCH WITH (FORCE)" >/dev/null 2>&1 \
        && ok "временная база $OFF_SCRATCH удалена" || bad "временная база $OFF_SCRATCH не удалилась (DROP DATABASE … WITH (FORCE) вручную)"
    fi
  }
  off_finish(){ # пароль на копию — снова сбросить (и при обрыве скрипта), убрать скачанные тестовые файлы
    off_drop_scratch
    if [ "$OFF_DIRTY" = "1" ]; then
      OFF_DIRTY=0
      if $DC python /app/reset_offsite_password.py >/dev/null 2>&1; then
        req GET "/offsite/status"
        body_has "r['password']['set'] is False" && ok "пароль на копию снова сброшен — стенд без пароля" || bad "после сброса пароль на копию остался"
      else bad "пароль на копию не сбросился (docker exec ds_api python /app/reset_offsite_password.py)"; fi
    fi
    $DC rm -rf "$OFF_DIR" 2>/dev/null
  }
  trap off_finish EXIT
  $DC mkdir -p "$OFF_DIR"
  # off_get ССЫЛКА ФАЙЛ — скачать без входа по ссылке из ответа (в ответе она с /api — так её видит браузер,
  # а curl идёт в API напрямую); код — в OFF_CODE, заголовки — в OFF_HEAD
  off_get(){
    OFF_HEAD=$($DC curl -s -D - -o "$2" -w $'\nHTTPCODE:%{http_code}' "$API${1#/api}" 2>/dev/null | tr -d '\r')
    OFF_CODE="${OFF_HEAD##*HTTPCODE:}"
  }
  off_hdr(){ echo "$OFF_HEAD" | grep -i "^$1:" | head -1 | cut -d: -f2- | sed 's/^ *//'; }
  # off_wait ВЫРАЖЕНИЕ — до 10 с ждём, пока статус копии удовлетворит python-выражению над r (отметку «скачано»
  # сервер ставит чуть позже последнего байта)
  off_wait(){ local i; for i in $(seq 1 20); do req GET "/offsite/status"; [ "$HTTP" = "200" ] && body_has "$1" && return 0; sleep 0.5; done; return 1; }

  # 1. чистое начало: пароль сброшен командой на сервере
  R=$($DC python /app/reset_offsite_password.py 2>&1); RC=$?
  [ "$RC" -eq 0 ] && ok "пароль на копию сброшен на сервере (чистое начало)" || bad "сброс пароля на копию: $R"
  req GET "/offsite/status"
  body_has "r['password']['set'] is False and r['password']['needs_reset'] is False" && ok "пароля на копию нет" || bad "после сброса пароль на копию остался"
  body_has "r['project_mounted'] is True" && ok "папка проекта подключена к ds_api — копию есть из чего собирать" \
    || bad "project_mounted=false: копию не собрать (запустите install_server.sh)"
  req POST "/offsite/copy" -H "$JSON_O" -d "$(oj login_password="$ADMIN_PASS")"
  [ "$HTTP" = "409" ] && detail_has "Сначала задайте пароль на копию" && ok "копия без пароля на копию — 409" || bad "копия без пароля на копию: HTTP $HTTP (ждали 409)"

  # 2. пароль на копию: правила и порядок проверок
  req POST "/offsite/password" -H "$JSON_O" -d "$(oj new_password="$OFF_PW0" new_password2="$OFF_PW0" login_password="wrong-$ADMIN_PASS")"
  [ "$HTTP" = "403" ] && detail_has "Неверный пароль входа" && ok "пароль на копию: неверный пароль входа — 403" || bad "пароль на копию, неверный вход: HTTP $HTTP (ждали 403)"
  req POST "/offsite/password" -H "$JSON_O" -d "$(oj new_password="$OFF_PW0" new_password2="${OFF_PW0}x" login_password="$ADMIN_PASS")"
  [ "$HTTP" = "400" ] && detail_has "Пароли не совпадают" && ok "пароль на копию: не совпали — 400" || bad "пароль на копию, не совпали: HTTP $HTTP (ждали 400)"
  req POST "/offsite/password" -H "$JSON_O" -d "$(oj new_password="short1" new_password2="short1" login_password="$ADMIN_PASS")"
  [ "$HTTP" = "400" ] && ok "пароль на копию: слишком короткий — 400" || bad "пароль на копию, короткий: HTTP $HTTP (ждали 400)"
  req POST "/offsite/password" -H "$JSON_O" -d "$(oj new_password="Пароль-Копия-12345" new_password2="Пароль-Копия-12345" login_password="$ADMIN_PASS")"
  [ "$HTTP" = "400" ] && ok "пароль на копию: кириллица — 400 (нужны латинские знаки)" || bad "пароль на копию, кириллица: HTTP $HTTP (ждали 400)"
  req GET "/offsite/status"
  body_has "r['password']['set'] is False" && ok "отказы пароля не оставили: пароля на копию по-прежнему нет" || bad "после отказов пароль на копию появился"
  req POST "/offsite/password" -H "$JSON_O" -d "$(oj new_password="$OFF_PW0" new_password2="$OFF_PW0" login_password="$ADMIN_PASS")"
  [ "$HTTP" = "200" ] && body_has "r=={'status':'saved'}" && ok "пароль на копию задан" || bad "задать пароль на копию: HTTP $HTTP $BODY"
  req GET "/offsite/status"
  if body_has "r['password']['set'] is True and r['password']['needs_reset'] is False and r['password']['set_by']=='$ADMIN_USER' and r['password']['set_at']"; then
    ok "в статусе: пароль задан, кем и когда"; else bad "статус после задания пароля: HTTP $HTTP"; fi
  req POST "/offsite/password" -H "$JSON_O" -d "$(oj new_password="$OFF_PW1" new_password2="$OFF_PW1" login_password="$ADMIN_PASS")"
  [ "$HTTP" = "403" ] && detail_has "Неверный текущий пароль на копию" && ok "смена пароля без текущего — 403" || bad "смена без текущего: HTTP $HTTP (ждали 403)"
  req POST "/offsite/password" -H "$JSON_O" -d "$(oj new_password="$OFF_PW1" new_password2="$OFF_PW1" login_password="$ADMIN_PASS" current_password="${OFF_PW0}x")"
  [ "$HTTP" = "403" ] && ok "смена пароля с неверным текущим — 403" || bad "смена с неверным текущим: HTTP $HTTP (ждали 403)"
  req POST "/offsite/password" -H "$JSON_O" -d "$(oj new_password="$OFF_PW1" new_password2="$OFF_PW1" login_password="$ADMIN_PASS" current_password="$OFF_PW0")"
  [ "$HTTP" = "200" ] && ok "пароль на копию сменён (с текущим)" || bad "смена пароля с текущим: HTTP $HTTP $BODY"

  # 3. копия: отказы и сборка
  req POST "/offsite/copy" -H "$JSON_O" -d "$(oj login_password="wrong-$ADMIN_PASS")"
  [ "$HTTP" = "403" ] && detail_has "Неверный пароль входа" && ok "копия: неверный пароль входа — 403" || bad "копия, неверный вход: HTTP $HTTP (ждали 403)"
  req POST "/offsite/copy" -H "$JSON_O" -d '{}'
  [ "$HTTP" = "403" ] && ok "копия без пароля входа в теле — 403" || bad "копия без пароля входа: HTTP $HTTP (ждали 403)"
  req POST "/offsite/copy" -H "$JSON_O" -d "$(oj login_password="$ADMIN_PASS")"
  if [ "$HTTP" = "200" ] && body_has "r['url'].startswith('/api/offsite/download?t=') and len(r['url'])>40 and r['filename'].startswith('ds_copy_') and r['filename'].endswith('.tar') and r['bytes']>0"; then
    OFF_URL=$(body_get url); OFF_FILE=$(body_get filename); OFF_BYTES=$(body_get bytes)
    ok "копия собрана: $OFF_FILE, $OFF_BYTES байт, одноразовая ссылка выдана"
  else
    OFF_URL=""; OFF_FILE=""; OFF_BYTES=""; bad "сборка копии: HTTP $HTTP $BODY"
  fi
  if [ -n "$OFF_URL" ]; then
    off_get "$OFF_URL" "$OFF_DIR/copy.tar"
    OFF_SIZE=$($DC stat -c %s "$OFF_DIR/copy.tar" 2>/dev/null)
    if [ "$OFF_CODE" = "200" ] && [ "$OFF_SIZE" = "$OFF_BYTES" ] && [ "$(off_hdr content-length)" = "$OFF_BYTES" ] \
       && [[ "$(off_hdr content-disposition)" == *"filename=\"$OFF_FILE\""* ]] && [[ "$(off_hdr content-type)" == application/x-tar* ]]; then
      ok "скачивание по ссылке без входа: 200, $OFF_SIZE байт = Content-Length, имя $OFF_FILE, application/x-tar"
    else bad "скачивание копии: HTTP $OFF_CODE, на диске ${OFF_SIZE:-?} из $OFF_BYTES, Content-Length «$(off_hdr content-length)»"; fi
    R=$($DC python3 -I -c "$OFF_PY_COPY" "$OFF_DIR/copy.tar" "$OFF_PW1" "ВОССТАНОВЛЕНИЕ.txt" "copy.tar.gz.enc" "copy.tar.gz.enc.sha256" "$OFF_DECRYPT" "$OFF_DIR/restore.sql.gz" 2>&1)
    OFF_MUSERS=""
    if [[ "$R" == OK* ]]; then
      read -r _ OFF_NFILES OFF_MUSERS <<< "$R"
      ok "копия: несжатый tar из трёх файлов, sha256 сходится, в инструкции команда расшифровки как на сервере, openssl открывает паролем на копию, чужим — нет, внутри база, программа с .env и опись ($OFF_NFILES файлов)"
    else bad "проверка содержимого копии: $R"; fi
    # Дамп из копии реально заливается в PostgreSQL 15 стенда тем же способом, что в ВОССТАНОВЛЕНИЕ.txt
    # (gunzip | psql -v ON_ERROR_STOP=1), — во временную базу. Так ловится расхождение версий pg_dump в
    # образе api и PostgreSQL (как было со строкой transaction_timeout) до того, как копия понадобится.
    if [ -n "$OFF_MUSERS" ]; then
      if [ -n "$OFF_PGU" ] && [ -n "$OFF_PGD" ] && off_pg -c "CREATE DATABASE $OFF_SCRATCH OWNER $OFF_PGU" >/dev/null 2>&1; then
        OFF_SCRATCH_MADE=1
        OFF_ERR=$( { $DC cat "$OFF_DIR/restore.sql.gz" | gunzip -c | docker exec -i ds_postgres psql -v ON_ERROR_STOP=1 -U "$OFF_PGU" -d "$OFF_SCRATCH" -q >/dev/null; } 2>&1 ); OFF_RC=$?
        if [ "$OFF_RC" -eq 0 ]; then
          ok "дамп из копии залился в PostgreSQL стенда (gunzip | psql -v ON_ERROR_STOP=1, временная база $OFF_SCRATCH)"
          OFF_U=$(docker exec ds_postgres psql -U "$OFF_PGU" -d "$OFF_SCRATCH" -tAc "SELECT count(*) FROM users" 2>/dev/null)
          [ "$OFF_U" = "$OFF_MUSERS" ] && ok "контрольное число сошлось: users во временной базе = $OFF_U, как в MANIFEST.txt" \
            || bad "контрольное число users: во временной базе «$OFF_U», в MANIFEST.txt $OFF_MUSERS"
        else bad "дамп из копии не залился в PostgreSQL (код $OFF_RC): $(echo "$OFF_ERR" | head -c 300)"; fi
      else bad "временная база $OFF_SCRATCH не создалась (POSTGRES_USER/POSTGRES_DB из ds_postgres: «$OFF_PGU»/«$OFF_PGD»)"; fi
      off_drop_scratch
    fi
    off_get "$OFF_URL" "$OFF_DIR/copy2.tar"
    [ "$OFF_CODE" = "404" ] && ok "вторая попытка по той же ссылке — 404 (ссылка одноразовая)" || bad "вторая попытка по ссылке: HTTP $OFF_CODE (ждали 404)"
    # «скачано» отмечается после последнего байта: ждём отметку в статусе
    if off_wait "r['copy']['today'] is True and r['copy']['last_file']=='$OFF_FILE' and r['copy']['last_bytes']==$OFF_BYTES and r['copy']['last_by']=='$ADMIN_USER'"; then
      ok "в статусе: копия сегодня скачана ($OFF_FILE, $ADMIN_USER)"; else bad "статус не отметил скачанную копию (last_file/last_bytes/last_by)"; fi
    req GET "/audit?limit=100"
    if body_has "any(x['title']=='Копия на флешку скачана' and '$OFF_FILE' in (x['detail'] or '') and x['actor']=='$ADMIN_USER' for x in r)"; then
      ok "в журнале: «Копия на флешку скачана»"; else bad "в журнале нет записи о скачанной копии"; fi
    body_has "any(x['title']=='Неверный пароль при скачивании копии' for x in r) and any(x['title']=='Пароль на копию изменён' for x in r)" \
      && ok "в журнале: отказ по неверному паролю входа и смена пароля на копию" || bad "в журнале нет записи об отказе или о смене пароля"
    if echo "$BODY" | grep -qF -e "$OFF_PW0" -e "$OFF_PW1" -e "$ADMIN_PASS"; then bad "в журнале действий нашёлся пароль!"; else ok "в журнале паролей нет"; fi
  fi

  # 4. ролики: «новые», потом «все»
  req GET "/media/${MID:-0}"; OFF_MFILE=$(body_get filename)
  req POST "/offsite/media" -H "$JSON_O" -d '{"scope":"new"}'
  if [ "$HTTP" = "200" ] && body_has "r['url'].startswith('/api/offsite/download?t=') and r['filename'].startswith('ds_media_new_') and r['filename'].endswith('.tar') and r['count']>=1 and r['bytes']>0 and isinstance(r['missing'],int)"; then
    OFF_URL=$(body_get url); OFF_MFN=$(body_get filename); OFF_MCNT=$(body_get count); OFF_MMISS=$(body_get missing)
    ok "ролики «новые»: $OFF_MFN, $OFF_MCNT шт, ссылка выдана"
  else
    OFF_URL=""; bad "ролики «новые»: HTTP $HTTP $BODY"
  fi
  if [ -n "$OFF_URL" ]; then
    off_get "$OFF_URL" "$OFF_DIR/media_new.tar"
    [ "$OFF_CODE" = "200" ] && [ "$(off_hdr content-length)" = "$($DC stat -c %s "$OFF_DIR/media_new.tar" 2>/dev/null)" ] \
      && ok "ролики «новые»: скачались, размер = Content-Length" || bad "ролики «новые»: HTTP $OFF_CODE, Content-Length «$(off_hdr content-length)»"
    R=$($DC python3 -I -c "$OFF_PY_MEDIA" "$OFF_DIR/media_new.tar" "${OFF_MFILE:-?}" "SMOKE_media_$STAMP" 2>&1)
    if [[ "$R" == OK* ]]; then
      read -r _ OFF_NF OFF_NL OFF_NM <<< "$R"
      [ "$OFF_NL" = "$OFF_MCNT" ] && [ "$OFF_NM" = "$OFF_MMISS" ] && ok "архив «новых»: файлов $OFF_NF, манифест на $OFF_NL строк = count из ответа, «нет файла» в манифесте ($OFF_NM) = missing из ответа ($OFF_MMISS), файл тестового ролика внутри" \
        || bad "архив «новых»: в манифесте $OFF_NL строк (ждали $OFF_MCNT), «нет файла» $OFF_NM (в ответе missing=$OFF_MMISS)"
    else bad "проверка архива «новых»: $R"; fi
    off_get "$OFF_URL" "$OFF_DIR/media_new2.tar"
    [ "$OFF_CODE" = "404" ] && ok "ролики: вторая попытка по ссылке — 404" || bad "ролики, вторая попытка: HTTP $OFF_CODE (ждали 404)"
    if off_wait "r['media']['new_count']==0 and r['media']['last_at'] and r['media']['last_by']=='$ADMIN_USER'"; then
      ok "в статусе: новых роликов нет, кто и когда скачал — на месте"; else bad "после скачивания «новых» статус всё ещё показывает новые ролики"; fi
    req POST "/offsite/media" -H "$JSON_O" -d '{"scope":"new"}'
    [ "$HTTP" = "409" ] && detail_has "Новых роликов нет" && ok "«новые» после скачивания — 409 «Новых роликов нет»" || bad "«новые» повторно: HTTP $HTTP (ждали 409)"
  fi
  req POST "/offsite/media" -H "$JSON_O" -d '{"scope":"all"}'
  if [ "$HTTP" = "200" ] && body_has "r['filename'].startswith('ds_media_all_') and r['count']>=1 and isinstance(r['missing'],int)"; then
    OFF_URL=$(body_get url); OFF_MCNT=$(body_get count); OFF_MMISS=$(body_get missing)
    off_get "$OFF_URL" "$OFF_DIR/media_all.tar"
    R=$($DC python3 -I -c "$OFF_PY_MEDIA" "$OFF_DIR/media_all.tar" "${OFF_MFILE:-?}" "SMOKE_media_$STAMP" 2>&1)
    if [ "$OFF_CODE" = "200" ] && [[ "$R" == OK* ]]; then
      read -r _ OFF_NF OFF_NL OFF_NM <<< "$R"
      [ "$OFF_NL" = "$OFF_MCNT" ] && [ "$OFF_NM" = "$OFF_MMISS" ] && ok "ролики «все»: $OFF_MCNT шт скачались, манифест на $OFF_NL строк, «нет файла» ($OFF_NM) = missing из ответа, файл тестового ролика на месте" \
        || bad "ролики «все»: в манифесте $OFF_NL строк (ждали $OFF_MCNT), «нет файла» $OFF_NM (в ответе missing=$OFF_MMISS)"
    else bad "ролики «все»: HTTP $OFF_CODE, $R"; fi
  else bad "ролики «все»: HTTP $HTTP $BODY"; fi
  req GET "/audit?limit=100"
  body_has "any(x['title']=='Ролики скачаны на флешку' and x['detail'].startswith('новые, ') for x in r) and any(x['title']=='Ролики скачаны на флешку' and x['detail'].startswith('все, ') for x in r)" \
    && ok "в журнале: «Ролики скачаны на флешку» — и новые, и все" || bad "в журнале нет записей о скачанных роликах"

  # 5. итог: копия сегодня, новых роликов нет — напоминание «ok»
  if off_wait "r['state']=='ok' and r['copy']['today'] is True and r['media']['new_count']==0"; then
    ok "напоминание «ok»: копия сегодня и новых роликов нет"; else bad "напоминание не стало «ok» после копии и роликов"; fi
  off_finish
  trap - EXIT
fi

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
# Бегущие строки и шаблоны SMOKE — по тексту (на случай, если проверка выше оборвалась на полпути)
req GET "/tickers"
for LEFT_ID in $(echo "$BODY" | python3 -c "import sys,json;print(' '.join(str(t['id']) for t in json.load(sys.stdin) if t['message'].startswith('SMOKE_tk_$STAMP')))" 2>/dev/null); do
  req DELETE "/tickers/$LEFT_ID"
  [ "$HTTP" = "200" ] && ok "бегущая строка проверки снята (id=$LEFT_ID)" || bad "снятие строки $LEFT_ID: HTTP $HTTP"
done
req GET "/ticker-templates"
for LEFT_ID in $(echo "$BODY" | python3 -c "import sys,json;print(' '.join(str(t['id']) for t in json.load(sys.stdin) if t['message'].startswith('SMOKE_tpl_$STAMP')))" 2>/dev/null); do
  req DELETE "/ticker-templates/$LEFT_ID"
  [ "$HTTP" = "200" ] && ok "шаблон проверки удалён (id=$LEFT_ID)" || bad "удаление шаблона $LEFT_ID: HTTP $HTTP"
done
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
