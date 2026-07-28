#!/usr/bin/env bash
# =============================================================================
# selfheal.sh — самодиагностика и автолечение Docker-стека Digital Signage.
#
# Запускается НА ХОСТЕ сервера (не в контейнере) — поэтому ловит и те случаи,
# когда панель вообще не открывается и встроенная диагностика (Настройки →
# Диагностика сервера) недоступна.
#
# Что проверяет и чинит сам:
#   • контейнер остановлен/отсутствует        → docker compose up -d <служба>
#   • ds_ntp в рестарт-цикле (баг pid-файла)  → docker compose up -d --build ntp
#   • api жив, но снаружи 502 (nginx держит
#     устаревший IP пересозданного api)       → docker restart ds_nginx
# Что только сообщает (лечение зависит от причины — см. runbook):
#   • любой другой контейнер в рестарт-цикле (показывает хвост его логов)
#   • диск хоста занят > 90%
#
# Использование:
#   bash selfheal.sh            # проверить и починить
#   bash selfheal.sh --dry-run  # только показать, что бы сделал
#
# Автозапуск раз в 5 минут (от root, путь подставьте свой):
#   echo '*/5 * * * * root /home/toor/Video_miniPC_v16.2_modular/selfheal.sh >> /var/log/ds_selfheal.log 2>&1' \
#     | sudo tee /etc/cron.d/ds-selfheal
#
# Код возврата: 0 — всё ок (или вылечено), 1 — остались проблемы.
# =============================================================================
set -u

cd "$(dirname "$0")"
if [ ! -f docker-compose.yml ]; then
    echo "ОШИБКА: рядом со скриптом нет docker-compose.yml — запускайте из каталога проекта."
    exit 1
fi

DRY=0
[ "${1:-}" = "--dry-run" ] && DRY=1

PROBLEMS=0
log(){ echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }
act(){  # act "<описание>" <команда…> — выполняет лечение (или печатает при --dry-run)
    local desc="$1"; shift
    if [ "$DRY" = 1 ]; then
        log "  [dry-run] $desc: $*"
    else
        log "  ЛЕЧУ: $desc"
        "$@" >/dev/null 2>&1 && log "  ok" || { log "  НЕ ПОМОГЛО: $desc"; PROBLEMS=1; }
    fi
}

# --- 1. Состояние контейнеров -------------------------------------------------
# имя_службы_в_compose:имя_контейнера
SERVICES="nginx:ds_nginx api:ds_api postgres:ds_postgres redis:ds_redis minio:ds_minio celery:ds_celery ntp:ds_ntp"

for pair in $SERVICES; do
    svc="${pair%%:*}"; cn="${pair##*:}"
    state="$(docker inspect -f '{{.State.Status}}' "$cn" 2>/dev/null || echo missing)"
    case "$state" in
        running)
            ;;
        restarting)
            if [ "$svc" = ntp ]; then
                # Известный баг: chronyd не стартует из-за устаревшего pid-файла.
                # Лечится пересборкой образа со свежим ntp/Dockerfile (rm pid-файла в CMD).
                log "ПРОБЛЕМА: $cn в рестарт-цикле (баг pid-файла chronyd)"
                act "пересобрать и перезапустить ntp" docker compose up -d --build ntp
            else
                log "ПРОБЛЕМА: $cn в рестарт-цикле — сам не чиню (причину покажут логи ниже; runbook §1–§4):"
                docker logs --tail 10 "$cn" 2>&1 | sed 's/^/    | /'
                PROBLEMS=1
            fi
            ;;
        missing)
            log "ПРОБЛЕМА: контейнер $cn отсутствует"
            act "создать и запустить $svc" docker compose up -d "$svc"
            ;;
        *)
            log "ПРОБЛЕМА: $cn в состоянии «$state»"
            act "запустить $svc" docker compose up -d "$svc"
            ;;
    esac
done

# --- 2. «Чёрная панель»: api жив, но через nginx 502 -------------------------
# Штатно этого больше не случается: в nginx.conf апстрим задан переменной
# ($ds_api) при живом resolver 127.0.0.11, поэтому имя перепроверяется на ходу.
# Проверка остаётся страховкой — на случай старого nginx.conf на сервере или
# другой причины, по которой api виден изнутри, но не через прокси.
inside_ok=0;  docker exec ds_api curl -fsS -m 3 http://localhost:8000/health >/dev/null 2>&1 && inside_ok=1
outside_ok=0; curl -fsS -m 3 http://localhost/health >/dev/null 2>&1 && outside_ok=1

if [ "$inside_ok" = 1 ] && [ "$outside_ok" = 0 ]; then
    log "ПРОБЛЕМА: api отвечает изнутри, но через nginx — нет (устаревший IP апстрима → «чёрная панель»)"
    act "перезапустить nginx" docker restart ds_nginx
    if [ "$DRY" = 0 ]; then
        sleep 3
        curl -fsS -m 3 http://localhost/health >/dev/null 2>&1 \
            && log "  панель снова доступна" \
            || { log "  всё ещё 502 — смотрите: docker logs --tail=50 ds_nginx (runbook §1)"; PROBLEMS=1; }
    fi
elif [ "$inside_ok" = 0 ]; then
    log "ПРОБЛЕМА: api не отвечает на /health даже изнутри — смотрите: docker logs --tail=100 ds_api (runbook §1)"
    PROBLEMS=1
fi

# --- 3. Часы хоста ------------------------------------------------------------
# chronyd в ds_ntp знает смещение системных часов относительно эталона
# (часы контейнера = часы хоста). Сами часы НЕ трогаем — печатаем команду:
# внезапный скачок времени задел бы расписание/отчёты в момент работы.
HOST_OFF=$(docker exec ds_ntp chronyc tracking 2>/dev/null \
    | awk '/System time/{v=$4; if($6=="slow") v=-v; printf "%.0f", v}')
if [ -n "$HOST_OFF" ]; then
    ABS_OFF=${HOST_OFF#-}
    if [ "$ABS_OFF" -ge 5 ] 2>/dev/null; then
        log "ПРОБЛЕМА: часы хоста расходятся с эталоном на ${HOST_OFF} с"
        log "  лечение: sudo date -s \"now $((-HOST_OFF)) seconds\"   (и настройте chrony на хосте — 01_УСТАНОВКА_СЕРВЕР, «Часы хоста»)"
        [ "$ABS_OFF" -ge 60 ] && PROBLEMS=1
    fi
fi

# --- 4. Диск хоста ------------------------------------------------------------
df -P / /var/lib/docker 2>/dev/null | awk 'NR>1 && !seen[$6]++' | while read -r _ _ _ _ pct mnt; do
    p="${pct%\%}"
    [ "$p" -ge 90 ] 2>/dev/null && log "ПРЕДУПРЕЖДЕНИЕ: диск $mnt занят на $pct — освободите место (старые бэкапы, docker system prune)"
done

if [ "$PROBLEMS" = 0 ]; then
    log "Все проверки пройдены."
else
    log "Остались проблемы — см. сообщения выше и docs/Техдокументация/03_RUNBOOK_ИНЦИДЕНТЫ.md"
fi
exit "$PROBLEMS"
