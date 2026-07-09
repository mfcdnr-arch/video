#!/bin/bash
# collect_diag.sh — собрать диагностику СЕРВЕРА в один файл для разбора/поддержки.
# Секреты (пароли БД/MinIO, SECRET_KEY, токены) в файле замаскированы.
# Использование:  bash collect_diag.sh   → создаёт diag_server_<дата>.txt
set -uo pipefail
cd "$(dirname "$0")"

# Значения для запросов к БД (из .env). В сам отчёт .env НЕ пишется.
set -a; . ./.env 2>/dev/null || true; set +a
: "${POSTGRES_USER:=display_user}"; : "${POSTGRES_DB:=display_system}"

OUT="diag_server_$(date +%Y%m%d_%H%M%S).txt"
{
  echo "================ ДИАГНОСТИКА СЕРВЕРА $(date '+%Y-%m-%d %H:%M:%S') ================"
  echo; echo "### Версия проекта (git) ###"; git log --oneline -1 2>/dev/null || echo "(git недоступен)"
  echo; echo "### Контейнеры ###"; docker compose ps 2>&1
  echo; echo "### Ресурсы (снимок) ###"; docker stats --no-stream --format 'table {{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}' 2>&1
  echo; echo "### Диск ###"; df -h 2>&1
  echo; echo "### Доступность панели/API ###"
  curl -s -o /dev/null -w 'GET /        -> HTTP %{http_code}\n' http://localhost/ 2>&1
  curl -s -o /dev/null -w 'GET /health  -> HTTP %{http_code}\n' http://localhost/health 2>&1
  echo; echo "### Миграции (кол-во применённых) ###"
  docker compose exec -T postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc "SELECT count(*) FROM schema_migrations" 2>&1
  echo; echo "### Логи API (хвост) ###";      docker compose logs --tail=80 api 2>&1
  echo; echo "### Логи nginx (хвост) ###";    docker compose logs --tail=25 nginx 2>&1
  echo; echo "### Логи PostgreSQL (хвост) ###"; docker compose logs --tail=25 postgres 2>&1
  echo; echo "### Логи Celery (хвост) ###";   docker compose logs --tail=25 celery 2>&1
  echo; echo "### NTP ###"; docker exec ds_ntp chronyc tracking 2>&1
} > "$OUT" 2>&1

# Маскируем секреты, если они случайно попали в логи.
for secret in "${DB_PASSWORD:-}" "${MINIO_PASSWORD:-}" "${SECRET_KEY:-}"; do
    [ -n "$secret" ] && sed -i.bak "s/${secret}/<REDACTED>/g" "$OUT" 2>/dev/null
done
rm -f "$OUT.bak"

echo "Готово: $OUT"
echo "Пришлите этот файл на разбор (секреты в нём замаскированы)."
