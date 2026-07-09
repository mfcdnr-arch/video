#!/bin/bash
# collect_diag_agent.sh — собрать диагностику АГЕНТА (мини-ПК) в один файл.
# Токен устройства в отчёте замаскирован. Запускать на мини-ПК.
# Использование:  bash collect_diag_agent.sh   → создаёт /tmp/diag_agent_<дата>.txt
set -uo pipefail

SERVER="${1:-10.0.119.100}"   # можно передать адрес сервера первым аргументом
OUT="/tmp/diag_agent_$(date +%Y%m%d_%H%M%S).txt"
{
  echo "================ ДИАГНОСТИКА АГЕНТА $(date '+%Y-%m-%d %H:%M:%S') ================"
  echo "Пользователь: $(whoami)   Хост: $(hostname)"
  echo; echo "### Статус службы ds-agent ###"; systemctl status ds-agent --no-pager 2>&1 | head -20
  echo; echo "### journalctl ds-agent (хвост) ###"; journalctl -u ds-agent -n 120 --no-pager 2>&1
  echo; echo "### Лог агента ###"; tail -n 120 /var/log/ds-agent/ds-agent.log 2>&1
  echo; echo "### Процесс плеера mpv ###"; pgrep -a mpv 2>&1 || echo "mpv НЕ запущен"
  echo; echo "### Скачанные медиа ###"; ls -la /opt/ds-agent/media 2>&1 | head -20
  echo; echo "### Конфиг (токен скрыт) ###"; sed 's/^token *=.*/token = <REDACTED>/' /etc/ds-agent/config.ini 2>&1
  echo; echo "### screen_id ###"; cat /etc/ds-agent/screen_id 2>&1
  echo; echo "### Время (chrony) ###"; chronyc tracking 2>&1; echo "--"; chronyc sources 2>&1
  echo; echo "### Графическая сессия ###"; echo "DISPLAY=${DISPLAY:-<не задан>}"; systemctl show ds-agent -p Environment 2>&1
  echo; echo "### Watchdog ###"; systemctl show ds-agent -p WatchdogUSec 2>&1
  echo; echo "### Сеть до сервера ($SERVER) ###"
  ping -c2 "$SERVER" 2>&1
  curl -s -o /dev/null -w 'health -> HTTP %{http_code}\n' "http://$SERVER/health" 2>&1
  echo; echo "### Лог установки ###"; tail -n 40 /var/log/ds-agent-install.log 2>&1
} > "$OUT" 2>&1

echo "Готово: $OUT"
echo "Пришлите этот файл на разбор (токен устройства в нём замаскирован)."
