#!/bin/bash
# install.sh — установка ds-agent на мини ПК (Astra Linux 1.7 SE)
# Использование: sudo bash install.sh <URL_СЕРВЕРА> <ТОКЕН> <SCREEN_ID> [ИМЯ_ПОЛЬЗОВАТЕЛЯ]
# Пример:        sudo bash install.sh http://10.0.119.100 abc123 1 toor
set -euo pipefail

# ── Хелперы самодиагностики ──────────────────────────────────────────────────
PASS=0; FAIL=0; WARN=0
pass(){ echo "  ✓ $1"; PASS=$((PASS+1)); }
bad(){  echo "  ✗ $1"; FAIL=$((FAIL+1)); }
warn(){ echo "  ⚠ $1"; WARN=$((WARN+1)); }
hint(){ echo "      └─ $1"; }
die(){  echo; echo "ОСТАНОВ: $1"; [ -n "${2:-}" ] && hint "$2"; exit 1; }

# ══════════════════════════════════════════════════════════════════════════════
# 1. АРГУМЕНТЫ
# ══════════════════════════════════════════════════════════════════════════════

SERVER_URL="${1:?Укажите URL сервера (первый аргумент). Пример: http://10.0.119.100}"
TOKEN="${2:?Укажите токен устройства (второй аргумент)}"
SCREEN_ID="${3:?Укажите ID экрана (третий аргумент, число)}"

# Пользователь X-сессии, под которым РАБОТАЕТ агент (НЕ root). По умолчанию toor.
# Сам скрипт запускается через sudo (нужен root для apt/systemd/etc), но служба
# ds-agent и плеер работают от этого пользователя.
REAL_USER="${4:-toor}"

# ══════════════════════════════════════════════════════════════════════════════
# 2. ВАЛИДАЦИЯ ВХОДНЫХ ПАРАМЕТРОВ
# ══════════════════════════════════════════════════════════════════════════════

if ! [[ "$SCREEN_ID" =~ ^[0-9]+$ ]]; then
    echo "ОШИБКА: SCREEN_ID должен быть числом, получено: '$SCREEN_ID'"
    exit 1
fi

if ! [[ "$SERVER_URL" =~ ^https?:// ]]; then
    echo "ОШИБКА: SERVER_URL должен начинаться с http:// или https://"
    echo "Пример: http://10.0.119.100"
    exit 1
fi

if [ -z "$REAL_USER" ] || [ "$REAL_USER" = "root" ]; then
    echo "ОШИБКА: Нужен реальный пользователь X-сессии, не root."
    echo "Пример: sudo bash install.sh http://10.0.119.100 ТОКЕН ID toor"
    exit 1
fi

if ! id "$REAL_USER" &>/dev/null; then
    echo "ОШИБКА: пользователь '$REAL_USER' не найден в системе"
    exit 1
fi

if [ "$EUID" -ne 0 ]; then
    echo "ОШИБКА: скрипт нужно запускать от root:"
    echo "  sudo bash install.sh http://10.0.119.100 ТОКЕН ID toor"
    exit 1
fi

# ── Логирование всей установки в файл (для разбора постфактум) ───────────────
INSTALL_LOG="/var/log/ds-agent-install.log"
exec > >(tee -a "$INSTALL_LOG") 2>&1

# ── Preflight: подходящая ли это система ─────────────────────────────────────
command -v apt-get  >/dev/null 2>&1 || die "apt-get не найден — скрипт рассчитан на Astra/Debian." "Используйте подходящий дистрибутив или поставьте зависимости вручную."
command -v systemctl >/dev/null 2>&1 || die "systemctl не найден — требуется systemd."

# ══════════════════════════════════════════════════════════════════════════════
# 3. ПЕРЕМЕННЫЕ
# ══════════════════════════════════════════════════════════════════════════════

INSTALL_DIR="/opt/ds-agent"
CONFIG_DIR="/etc/ds-agent"
LOG_DIR="/var/log/ds-agent"
MEDIA_DIR="/opt/ds-agent/media"
USER_HOME="$(eval echo "~$REAL_USER")"
USER_GROUP="$(id -gn "$REAL_USER")"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

SCHEME="$(echo "$SERVER_URL" | sed -E 's#^(https?)://.*#\1#')"
HOST_ONLY="$(echo "$SERVER_URL" | sed -E 's#^https?://##' | sed -E 's#[:/].*$##')"
PORT_ONLY="$(echo "$SERVER_URL" | sed -nE 's#^https?://[^/:]+:([0-9]+).*$#\1#p')"

if [ -z "${PORT_ONLY:-}" ]; then
    if [ "$SCHEME" = "https" ]; then
        PORT_ONLY="443"
    else
        PORT_ONLY="80"
    fi
fi

echo "=========================================="
echo " Digital Signage Agent — установка"
echo " Сервер:    $SERVER_URL"
echo " Схема:     $SCHEME"
echo " Хост:      $HOST_ONLY"
echo " Порт:      $PORT_ONLY"
echo " Screen ID: $SCREEN_ID"
echo " Пользователь X-сессии: $REAL_USER (группа: $USER_GROUP)"
echo "=========================================="

# ══════════════════════════════════════════════════════════════════════════════
# ШАГ 0: Проверка наличия всех файлов агента
# ══════════════════════════════════════════════════════════════════════════════
echo "[0/10] Проверка файлов агента..."
MISSING=0
for f in \
    ds_agent.py \
    ds_downloader.py \
    ds_player.py \
    ds_sync.py \
    ds_heartbeat.py \
    ds_cleanup.py \
    ds_ws_client.py \
    ds_ota_updater.py \
    requirements.txt \
    ds-agent.service
do
    if [ ! -f "$SCRIPT_DIR/agent/$f" ]; then
        echo "  ОШИБКА: файл не найден: $SCRIPT_DIR/agent/$f"
        MISSING=1
    fi
done

if [ "$MISSING" = "1" ]; then
    echo ""
    echo "Убедитесь что папка agent/ полностью скопирована на мини ПК."
    exit 1
fi

echo "Все нужные файлы найдены."

# ══════════════════════════════════════════════════════════════════════════════
# ШАГ 1: Системные пакеты
# ══════════════════════════════════════════════════════════════════════════════
echo "[1/10] Установка системных пакетов..."
apt-get update -q || warn "apt-get update завершился с ошибкой — установка пакетов может не удаться."
if ! DEBIAN_FRONTEND=noninteractive apt-get install -y -q \
    python3 python3-pip python3-venv \
    mpv chrony curl; then
    die "Не удалось установить системные пакеты." "Проверьте apt-источники и доступ к репозиториям (сеть/зеркало Astra)."
fi
# Проверяем, что критичные бинарники РЕАЛЬНО встали (особенно плеер mpv —
# без него не будет воспроизведения).
for bin in python3 mpv chronyc curl; do
    if command -v "$bin" >/dev/null 2>&1; then
        pass "$bin установлен"
    else
        bad "$bin НЕ установлен"
        [ "$bin" = mpv ] && hint "Без mpv плеер не запустится. Поставьте вручную: apt-get install -y mpv"
    fi
done
[ "$FAIL" -eq 0 ] || die "Не установлены обязательные пакеты (см. ✗ выше)."

# ══════════════════════════════════════════════════════════════════════════════
# ШАГ 2: Директории
# ══════════════════════════════════════════════════════════════════════════════
echo "[2/10] Создание директорий..."
mkdir -p "$INSTALL_DIR" "$CONFIG_DIR" "$LOG_DIR" "$MEDIA_DIR"

chown -R "$REAL_USER:$USER_GROUP" "$INSTALL_DIR" "$LOG_DIR"
chown "$REAL_USER:$USER_GROUP" "$MEDIA_DIR"

chmod 750 "$INSTALL_DIR"
chmod 775 "$MEDIA_DIR"

# ══════════════════════════════════════════════════════════════════════════════
# ШАГ 3: Файлы агента
# ══════════════════════════════════════════════════════════════════════════════
echo "[3/10] Копирование файлов агента..."
for f in \
    ds_agent.py \
    ds_downloader.py \
    ds_player.py \
    ds_sync.py \
    ds_heartbeat.py \
    ds_cleanup.py \
    ds_ws_client.py \
    ds_ota_updater.py \
    requirements.txt
do
    cp "$SCRIPT_DIR/agent/$f" "$INSTALL_DIR/"
done

chown -R "$REAL_USER:$USER_GROUP" "$INSTALL_DIR"

# ══════════════════════════════════════════════════════════════════════════════
# ШАГ 4: Python virtual environment
# ══════════════════════════════════════════════════════════════════════════════
echo "[4/10] Установка Python-зависимостей..."
sudo -u "$REAL_USER" python3 -m venv "$INSTALL_DIR/venv" \
    || die "Не удалось создать venv." "Установлен ли пакет python3-venv?"
sudo -u "$REAL_USER" "$INSTALL_DIR/venv/bin/pip" install --upgrade pip -q \
    || warn "Не удалось обновить pip (продолжаем)."
sudo -u "$REAL_USER" "$INSTALL_DIR/venv/bin/pip" install -r "$INSTALL_DIR/requirements.txt" -q \
    || die "Не удалось установить Python-зависимости." "Проверьте сеть и доступ к PyPI/зеркалу."
# Проверяем, что ключевая зависимость реально импортируется.
if sudo -u "$REAL_USER" "$INSTALL_DIR/venv/bin/python" -c "import requests" 2>/dev/null; then
    pass "Python-зависимости установлены (requests импортируется)"
else
    die "Пакет requests не установился." "Проверьте лог pip выше и доступ к PyPI."
fi

# ══════════════════════════════════════════════════════════════════════════════
# ШАГ 5: Проверка доступности сервера
# ══════════════════════════════════════════════════════════════════════════════
echo "[5/10] Проверка доступности сервера..."
HEALTH_URL="${SCHEME}://${HOST_ONLY}:${PORT_ONLY}/health"

if curl -fsS --max-time 10 "$HEALTH_URL" >/dev/null; then
    echo "Сервер доступен: $HEALTH_URL"
else
    echo "ПРЕДУПРЕЖДЕНИЕ: сервер пока не ответил на $HEALTH_URL"
    echo "Установка продолжится, но агент может не подключиться сразу."
fi

# ══════════════════════════════════════════════════════════════════════════════
# ШАГ 6: Конфигурация
# ══════════════════════════════════════════════════════════════════════════════
echo "[6/10] Создание конфига..."

cat > "$CONFIG_DIR/config.ini" << INIEOF
[server]
host = $HOST_ONLY
port = $PORT_ONLY
token = $TOKEN

[player]
media_dir = $MEDIA_DIR
player_bin = /usr/bin/mpv
# Видео поверх окон окружения и без рамки (fly-wm перекрывает fullscreen).
player_ontop = true

[schedule]
poll_interval = 60
heartbeat_interval = 30
command_poll_interval = 5

[sync]
sync_delay = 3

[logging]
level = INFO
file = $LOG_DIR/ds-agent.log
max_bytes = 10485760
backup_count = 5
INIEOF

chown "root:$USER_GROUP" "$CONFIG_DIR/config.ini"
chmod 640 "$CONFIG_DIR/config.ini"

echo "$SCREEN_ID" > "$CONFIG_DIR/screen_id"
chown "root:$USER_GROUP" "$CONFIG_DIR/screen_id"
chmod 644 "$CONFIG_DIR/screen_id"

chown "root:$USER_GROUP" "$CONFIG_DIR"
chmod 750 "$CONFIG_DIR"

# ══════════════════════════════════════════════════════════════════════════════
# ШАГ 7: sudoers для reboot и restart
# ══════════════════════════════════════════════════════════════════════════════
echo "[7/10] Настройка sudo для перезагрузки и рестарта..."

SUDOERS_FILE="/etc/sudoers.d/ds-agent-reboot"
SUDOERS_TMP="$(mktemp /tmp/ds-sudoers.XXXXXX)"

# ВАЖНО: путь в правиле должен совпадать с тем, как sudo резолвит команду.
# На разных системах systemctl/reboot лежат в /bin или /usr/bin (/sbin или
# /usr/sbin). Если путь не совпадёт — sudo запросит пароль, и в неинтерактивном
# режиме (OTA-самоперезапуск) команда молча провалится. Поэтому перечисляем оба.
printf '%s\n' \
    "$REAL_USER ALL=(ALL) NOPASSWD: /sbin/reboot" \
    "$REAL_USER ALL=(ALL) NOPASSWD: /usr/sbin/reboot" \
    "$REAL_USER ALL=(ALL) NOPASSWD: /bin/systemctl restart ds-agent" \
    "$REAL_USER ALL=(ALL) NOPASSWD: /usr/bin/systemctl restart ds-agent" \
    > "$SUDOERS_TMP"

if visudo -cf "$SUDOERS_TMP" &>/dev/null; then
    cp "$SUDOERS_TMP" "$SUDOERS_FILE"
    chmod 440 "$SUDOERS_FILE"
    chown root:root "$SUDOERS_FILE"
    echo "sudoers правило добавлено и синтаксис проверен."
else
    echo "ПРЕДУПРЕЖДЕНИЕ: visudo не принял синтаксис. Правило reboot/restart НЕ добавлено."
fi

rm -f "$SUDOERS_TMP"

# ══════════════════════════════════════════════════════════════════════════════
# ШАГ 8: NTP
# ══════════════════════════════════════════════════════════════════════════════
echo "[8/10] Настройка NTP..."

# Источники времени:
#  1) Российские серверы точного времени (ВНИИФТРИ — государственный
#     эталон времени РФ, и MSK-IX) — основные, если есть выход в интернет;
#  2) Сервер Digital Signage ($HOST_ONLY) — NTP-контейнер из docker-compose,
#     единственный источник в изолированной сети (мини-ПК видит только его).
# chrony сам выбирает лучший доступный источник и ПЛАВНО корректирует часы;
# makestep разрешает разовый скачок при большом расхождении (первые 3 замера).
cat > /etc/chrony/chrony.conf << NCONF
server ntp1.vniiftri.ru iburst
server ntp2.vniiftri.ru iburst
server ntp.msk-ix.ru iburst
server $HOST_ONLY iburst
driftfile /var/lib/chrony/chrony.drift
logdir /var/log/chrony
makestep 1.0 3
rtcsync
NCONF

# systemd-timesyncd конфликтует с chrony — выключаем, если есть.
if systemctl list-unit-files systemd-timesyncd.service &>/dev/null; then
    systemctl disable --now systemd-timesyncd 2>/dev/null || true
fi

CHRONY_SVC="chrony"
if ! systemctl list-unit-files chrony.service &>/dev/null 2>&1; then
    if systemctl list-unit-files chronyd.service &>/dev/null 2>&1; then
        CHRONY_SVC="chronyd"
    fi
fi

systemctl enable "$CHRONY_SVC"
systemctl restart "$CHRONY_SVC"

echo "Ожидание первичной NTP-синхронизации (15 секунд)..."
sleep 15

echo "--- NTP статус ---"
if chronyc tracking; then
    OFFSET="$(chronyc tracking 2>/dev/null | grep 'System time' | awk '{print $4}' | sed 's/-//')"
    echo "Текущее смещение системного времени: ${OFFSET:-?} секунд"
else
    echo "NTP ещё не синхронизировался — подождите 1-2 минуты."
fi

# ══════════════════════════════════════════════════════════════════════════════
# ШАГ 9: Автологин и X-сессия
# ══════════════════════════════════════════════════════════════════════════════
echo "[9/10] Настройка автологина..."

LIGHTDM_CONF="/etc/lightdm/lightdm.conf"
if [ -f "$LIGHTDM_CONF" ]; then
    cp "$LIGHTDM_CONF" "${LIGHTDM_CONF}.bak.$(date +%s)" 2>/dev/null || true

    if grep -q "autologin-user=" "$LIGHTDM_CONF"; then
        sed -i "s/^autologin-user=.*/autologin-user=$REAL_USER/" "$LIGHTDM_CONF"
    elif grep -q "\\[Seat:\\\*\\]" "$LIGHTDM_CONF"; then
        sed -i "/\\[Seat:\\\*\\]/a autologin-user=$REAL_USER\nautologin-user-timeout=0" "$LIGHTDM_CONF"
    else
        cat >> "$LIGHTDM_CONF" << LEOF

[Seat:*]
autologin-user=$REAL_USER
autologin-user-timeout=0
LEOF
    fi

    echo "Автологин настроен для пользователя $REAL_USER"
elif command -v fly-dm >/dev/null 2>&1 || [ -e /etc/X11/fly-dm ] || [ -d /etc/X11/fly-dm ]; then
    warn "Дисплей-менеджер Astra (fly-dm) — автологин НЕ настраивается автоматически."
    hint "Включите ВРУЧНУЮ: Панель управления Fly → «Менеджер входа в систему» (fly-admin-dm)"
    hint "→ «Автоматический вход» для пользователя $REAL_USER, задержка 0."
    hint "ВАЖНО: правка /etc/X11/fly-dm/fly-dmrc через sed на Astra НЕ работает — только GUI."
    hint "Не включайте «повторный автовход» (AutoLoginAgain) — виснет окно отсчёта."
    hint "Без автологина после перезагрузки не поднимется X-сессия и плеер (чёрный экран)."
else
    warn "LightDM не найден — автологин НЕ настроен."
    hint "Настройте автоматический вход для пользователя $REAL_USER средствами вашего"
    hint "дисплей-менеджера, иначе после перезагрузки графическая сессия не поднимется."
fi

# ── Signage-режим экрана: не блокировать/не гасить по простою ──
# Блокировщик KDE/fly (kscreenlocker) отключаем автоматически для сессии
# пользователя. Гашение/DPMS дополнительно снимает агент (xset) при старте mpv.
KSCR_CFG="$USER_HOME/.config/kscreenlockerrc"
mkdir -p "$USER_HOME/.config"
cat > "$KSCR_CFG" << KEOF
[Daemon]
Autolock=false
LockOnResume=false
Timeout=0
KEOF
chown -R "$REAL_USER:$USER_GROUP" "$USER_HOME/.config" 2>/dev/null || true
echo "Блокировка экрана по простою отключена (kscreenlockerrc)."
hint "Питание: Панель управления Fly → «Управление питанием» → «От сети»: экран и сон = «Никогда»."

XINITRC="$USER_HOME/.xinitrc"
if [ ! -f "$XINITRC" ]; then
    cat > "$XINITRC" << XEOF
#!/bin/sh
if command -v startxfce4 &>/dev/null; then
    exec startxfce4
elif command -v openbox-session &>/dev/null; then
    exec openbox-session
else
    exec xterm
fi
XEOF
    chown "$REAL_USER:$USER_GROUP" "$XINITRC"
    chmod 644 "$XINITRC"
    echo ".xinitrc создан"
fi

# ══════════════════════════════════════════════════════════════════════════════
# ШАГ 10: systemd сервис
# ══════════════════════════════════════════════════════════════════════════════
echo "[10/10] Регистрация systemd-сервиса..."

sed "s|User=user|User=$REAL_USER|g;
     s|Group=user|Group=$USER_GROUP|g;
     s|/home/user|$USER_HOME|g" \
    "$SCRIPT_DIR/agent/ds-agent.service" > /etc/systemd/system/ds-agent.service

systemctl daemon-reload
systemctl enable ds-agent
systemctl restart ds-agent

sleep 3

# ── SELF-TEST: сводная проверка результата установки ─────────────────────────
echo
echo "▶ Самопроверка установки"

if systemctl is-active --quiet ds-agent; then
    pass "служба ds-agent запущена (active)"
else
    bad "служба ds-agent НЕ запущена"
    hint "Диагностика: systemctl status ds-agent; journalctl -u ds-agent -n 50"
    systemctl status ds-agent --no-pager 2>/dev/null | head -12 || true
    journalctl -u ds-agent -n 30 --no-pager 2>/dev/null || true
fi

[ -x /usr/bin/mpv ]              && pass "плеер mpv на месте (/usr/bin/mpv)"        || { bad "нет /usr/bin/mpv — воспроизведение не заработает"; hint "apt-get install -y mpv"; }
[ -f "$CONFIG_DIR/config.ini" ]  && pass "конфиг создан ($CONFIG_DIR/config.ini)"   || bad "нет config.ini"
[ -f "$CONFIG_DIR/screen_id" ]   && pass "screen_id записан ($(cat "$CONFIG_DIR/screen_id" 2>/dev/null))" || bad "нет screen_id"
if command -v chronyc >/dev/null 2>&1 && chronyc tracking >/dev/null 2>&1; then
    pass "NTP (chrony) работает"
else
    warn "NTP ещё не синхронизировался — подождите 1–2 минуты (важно для расписания)"
fi
if curl -fsS --max-time 10 "$HEALTH_URL" >/dev/null 2>&1; then
    pass "сервер доступен ($HEALTH_URL)"
else
    warn "сервер сейчас недоступен — агент подключится, когда сеть/сервер поднимутся"
fi

echo
echo "=========================================="
if [ "$FAIL" -eq 0 ]; then
    echo " ✅ Установка завершена. Проверок пройдено: $PASS, предупреждений: $WARN."
else
    echo " ❌ Установка с ошибками: провалено $FAIL, пройдено $PASS, предупреждений $WARN."
    echo "    Разберите строки ✗ выше по подсказкам."
fi
echo " Пользователь сервиса: $REAL_USER (группа: $USER_GROUP)"
echo " Сервер: $HOST_ONLY:$PORT_ONLY"
echo " Статус:  systemctl status ds-agent"
echo " Логи:    journalctl -u ds-agent -f   |   tail -f $LOG_DIR/ds-agent.log"
echo " Лог установки: $INSTALL_LOG"
echo " ⏳ Экран должен появиться ОНЛАЙН в панели через ~30 секунд."
echo "=========================================="
[ "$FAIL" -eq 0 ] || exit 1

