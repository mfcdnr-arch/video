"""
ds_agent.py — главный агент Digital Signage для мини ПК на Astra Linux
Координирует: загрузку файлов, воспроизведение, heartbeat, команды сервера
"""
from __future__ import annotations
import os, sys, time, json, logging, logging.handlers, configparser, subprocess, threading
from pathlib import Path
from datetime import datetime, timezone, timedelta

# Вся сеть работает по московскому времени (у Москвы нет сезонного
# перевода часов, фиксированное смещение корректно). По МСК выбираются
# часовые слоты расписания, окно питания экрана и ночная громкость.
MSK = timezone(timedelta(hours=3))


def _sd_notify(state: str) -> None:
    """
    Отправить статус systemd через $NOTIFY_SOCKET (для systemd watchdog).
    Без внешних зависимостей. Если NOTIFY_SOCKET не задан (агент запущен не под
    systemd-watchdog) — тихо ничего не делает, поэтому вызывать всегда безопасно.
    """
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return
    if addr.startswith("@"):          # абстрактное пространство имён сокетов
        addr = "\0" + addr[1:]
    try:
        import socket as _s
        sock = _s.socket(_s.AF_UNIX, _s.SOCK_DGRAM)
        sock.connect(addr)
        sock.sendall(state.encode())
        sock.close()
    except Exception:
        pass


# ── Загружаем конфиг ─────────────────────────────────────────────────────────
CONFIG_PATH = os.getenv("DS_CONFIG", "/etc/ds-agent/config.ini")

cfg = configparser.ConfigParser()
if not Path(CONFIG_PATH).exists():
    print("ОШИБКА: конфиг не найден: " + CONFIG_PATH, file=sys.stderr)
    sys.exit(1)
cfg.read(CONFIG_PATH)

SERVER_HOST = cfg["server"]["host"]
SERVER_PORT = cfg["server"].get("port", "80")
TOKEN       = cfg["server"]["token"]
SERVER_URL  = "http://{}:{}".format(SERVER_HOST, SERVER_PORT)

MEDIA_DIR   = cfg["player"]["media_dir"]
# Плеер — mpv (управляется через IPC-сокет). Путь к бинарнику берём из конфига;
# .get() с дефолтом — на случай старого config.ini без ключа player_bin.
PLAYER_BIN  = cfg["player"].get("player_bin", "/usr/bin/mpv")
# Держать видео поверх окон окружения (fly-wm иногда перекрывает fullscreen).
# По умолчанию включено; выключается строкой player_ontop = false в config.ini.
PLAYER_ONTOP = cfg["player"].get("player_ontop", "true").strip().lower() not in ("false", "0", "no")

POLL_INTERVAL = int(cfg["schedule"]["poll_interval"])
HB_INTERVAL   = int(cfg["schedule"]["heartbeat_interval"])
CMD_POLL      = int(cfg["schedule"]["command_poll_interval"])

LOG_LEVEL  = cfg["logging"].get("level", "INFO")
LOG_FILE   = cfg["logging"].get("file", "/var/log/ds-agent/ds-agent.log")
LOG_BYTES  = int(cfg["logging"].get("max_bytes", "10485760"))
LOG_COUNT  = int(cfg["logging"].get("backup_count", "5"))

# ── Логирование ───────────────────────────────────────────────────────────────
Path(LOG_FILE).parent.mkdir(parents=True, exist_ok=True)
root_log = logging.getLogger()
root_log.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
fh = logging.handlers.RotatingFileHandler(
    LOG_FILE, maxBytes=LOG_BYTES, backupCount=LOG_COUNT)
fh.setFormatter(fmt)
ch = logging.StreamHandler()
ch.setFormatter(fmt)
root_log.addHandler(fh)
root_log.addHandler(ch)
log = logging.getLogger("ds_agent")

# ── Импортируем модули ────────────────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent))
from ds_downloader import Downloader
from ds_player    import VLCPlayer
from ds_sync      import SyncHandler
from ds_heartbeat import Heartbeat, AGENT_VERSION
from ds_cleanup   import Cleanup
from ds_ws_client import WSClient

import requests

# ── Кеш расписания (офлайн-работа) ───────────────────────────────────────────
SCHEDULE_CACHE = Path(MEDIA_DIR) / ".schedule_cache.json"


def get_screen_id():
    id_file = Path("/etc/ds-agent/screen_id")
    if id_file.exists():
        return int(id_file.read_text().strip())
    raise RuntimeError("screen_id не найден. Выполните регистрацию мини ПК на сервере.")


def load_schedule_online(screen_id, sess):
    """Загрузить расписание с сервера. Возвращает dict или None."""
    try:
        r = sess.get("{}/api/schedule/minipc/{}".format(SERVER_URL, screen_id), timeout=15)
        r.raise_for_status()
        data = r.json()
        SCHEDULE_CACHE.write_text(json.dumps(data))
        return data
    except Exception as e:
        log.warning("Не удалось получить расписание: {}".format(e))
        return None


def load_schedule_cache():
    if SCHEDULE_CACHE.exists():
        try:
            return json.loads(SCHEDULE_CACHE.read_text())
        except Exception:
            pass
    return None


def current_playlist_files(schedule):
    """
    Вернуть упорядоченный список файлов плейлиста, актуального сейчас (МСК).
    Приоритет:
      1) hourly_sequence — час эфира (реклама + заглушки равномерно);
      2) active_playlist (эфир/переопределение); off = экран выключен намеренно;
      3) недельный шаблон;
      4) fallback — заглушки, если по расписанию ничего не выпало
         (чтобы в дыре между слотами не было чёрного экрана).
    """
    # 1) Часовая последовательность с заглушками
    seq = schedule.get("hourly_sequence")
    if seq:
        return list(seq)

    active = schedule.get("active_playlist")
    if active:
        if active.get("off"):
            return []   # выключено переопределением — фолбэк НЕ играем
        files = [it.get("filename") for it in (schedule.get("active_items") or []) if it.get("filename")]
        if files:
            return files

    now = datetime.now(MSK)
    weekday = now.weekday()
    hour = now.hour
    day_slot = None
    chosen = None
    for slot in schedule.get("schedule", []):
        if slot.get("day_of_week") == weekday:
            if slot.get("hour") is None:
                day_slot = slot
            elif slot.get("hour") == hour:
                chosen = slot
    slot = chosen or day_slot
    if slot:
        files = [it.get("filename") for it in (slot.get("items") or []) if it.get("filename")]
        if files:
            return files

    # 4) Дыра в расписании — заглушки вместо чёрного экрана
    return list(schedule.get("fallback") or [])


# ── Питание экрана и громкость (времена московские) ─────────────────────────

def _hhmm_to_min(value):
    """'HH:MM' → минуты от полуночи, None при ошибке/пустом значении."""
    try:
        h, m = str(value).split(":")[:2]
        return int(h) * 60 + int(m)
    except (ValueError, AttributeError, TypeError):
        return None


def _in_window(now_min, start_min, end_min):
    """Попадает ли время в окно [start, end); окно может переходить полночь."""
    if start_min is None or end_min is None or start_min == end_min:
        return False
    if start_min < end_min:
        return start_min <= now_min < end_min
    return now_min >= start_min or now_min < end_min


def screen_should_be_on(schedule, now_msk):
    """True, если монитор сейчас должен работать (окно power.on..power.off)."""
    p = (schedule or {}).get("power") or {}
    on_min = _hhmm_to_min(p.get("on"))
    off_min = _hhmm_to_min(p.get("off"))
    if on_min is None or off_min is None:
        return True   # окно не задано — работаем всегда
    return _in_window(now_msk.hour * 60 + now_msk.minute, on_min, off_min)


def target_volume(schedule, now_msk):
    """Громкость для текущего времени: ночная в окне night_from..night_to."""
    v = (schedule or {}).get("volume") or {}
    day = v.get("day", 100)
    nf = _hhmm_to_min(v.get("night_from"))
    nt = _hhmm_to_min(v.get("night_to"))
    if nf is None or nt is None:
        return day
    if _in_window(now_msk.hour * 60 + now_msk.minute, nf, nt):
        return v.get("night", day)
    return day


def set_display_power(on: bool):
    """Включить/погасить монитор через DPMS (xset)."""
    env = os.environ.copy()
    env.setdefault("DISPLAY", ":0")
    try:
        if on:
            subprocess.run(["xset", "dpms", "force", "on"], env=env, timeout=5)
            subprocess.run(["xset", "s", "reset"], env=env, timeout=5)
        else:
            subprocess.run(["xset", "dpms", "force", "off"], env=env, timeout=5)
        log.info("Монитор: {}".format("включён" if on else "погашен (DPMS off)"))
    except Exception as e:
        log.warning("Не удалось управлять питанием монитора: {}".format(e))


# ── Главный цикл ─────────────────────────────────────────────────────────────
def report_broken(sess, screen_id, filename, reason):
    """Сообщить серверу, что ролик не воспроизводится (для отчёта)."""
    try:
        sess.post("{}/api/playback/error/{}".format(SERVER_URL, screen_id),
                  json={"filename": filename, "reason": reason}, timeout=8)
    except Exception as e:
        log.debug("Не удалось отправить отчёт об ошибке ролика: {}".format(e))


def report_play(sess, screen_id, filename):
    """Сообщить серверу о факте показа ролика (для журнала показов play_log)."""
    try:
        sess.post("{}/api/playback/log/{}".format(SERVER_URL, screen_id),
                  json={"filename": filename}, timeout=8)
    except Exception as e:
        log.debug("Не удалось отправить лог показа: {}".format(e))


def main():
    log.info("=== ds-agent стартует ===")

    # Журнал перезапусков (для диагностики после сбоев питания)
    restart_count_file = Path(MEDIA_DIR) / ".restart_count"
    try:
        count = int(restart_count_file.read_text().strip()) + 1 if restart_count_file.exists() else 1
        restart_count_file.write_text(str(count))
        if count > 1:
            log.warning(f"Это перезапуск #{count} (возможно, после сбоя питания или падения)")
        else:
            log.info("Первый запуск агента")
    except Exception:
        pass

    log.info("Сервер: " + SERVER_URL)
    log.info("Медиа: " + MEDIA_DIR)

    screen_id = get_screen_id()
    log.info("Screen ID: {}".format(screen_id))

    # Сообщить серверу о перезапуске агента (для журнала диагностики)
    def _report_restart(count: int):
        try:
            requests.post(
                "{}/api/agent/restart/{}".format(SERVER_URL, screen_id),
                json={
                    "restart_count": count,
                    "reason": "unknown",   # TODO: улучшить определение причины
                    "agent_version": AGENT_VERSION,
                },
                headers={"X-Token": TOKEN},
                timeout=5
            )
        except Exception:
            pass
    # Запускаем отчёт в фоне, не блокируем старт
    import threading as _threading
    _threading.Thread(target=_report_restart, args=(count if 'count' in dir() else 1,), daemon=True).start()

    sess = requests.Session()
    sess.headers["X-Token"] = TOKEN

    player     = VLCPlayer(player_bin=PLAYER_BIN, media_dir=MEDIA_DIR, ontop=PLAYER_ONTOP)
    downloader = Downloader(SERVER_URL, TOKEN, MEDIA_DIR)
    sync_hdl   = SyncHandler(SERVER_URL, TOKEN, player, screen_id)
    hb         = Heartbeat(SERVER_URL, TOKEN, screen_id, MEDIA_DIR)
    cleanup    = Cleanup(downloader, screen_id, MEDIA_DIR)

    player.start_vlc()

    # ── WebSocket push-соединение (замена polling команд) ──
    _ws_triggered = threading.Event()

    def _on_ws_command():
        """Callback: новая команда пришла через WS — немедленно poll."""
        _ws_triggered.set()

    def _on_ws_connect():
        log.info("[WS] Push-команды активны (polling отключён)")

    def _on_ws_disconnect():
        log.info("[WS] Push недоступен — fallback на polling каждые {}с".format(CMD_POLL))

    ws_client = WSClient(
        server_url=SERVER_URL,
        screen_id=screen_id,
        token=TOKEN,
        on_command=_on_ws_command,
        on_connect=_on_ws_connect,
        on_disconnect=_on_ws_disconnect,
    )
    ws_client.start()

    last_schedule_poll = 0
    last_heartbeat     = 0
    last_cmd_poll      = 0
    last_cleanup       = 0
    last_watchdog      = 0    # systemd watchdog: как давно слали WATCHDOG=1
    last_awake         = 0    # как давно отключали гашение экрана (xset)
    schedule           = None
    current_playing    = None    # что реально играет mpv (для heartbeat и play_log)
    current_list       = []      # плейлист, загруженный в mpv в данный момент
    last_time_pos      = None    # для детекции повтора одного и того же файла
    display_on         = True    # текущее состояние монитора (DPMS)
    current_volume     = None    # применённая громкость
    broken_files       = set()   # файлы, которые не удалось воспроизвести

    log.info("Главный цикл запущен")
    _sd_notify("READY=1")   # сообщаем systemd, что агент поднялся

    while True:
        now = time.time()

        # ── systemd watchdog: подтверждаем «живость» ──
        # Шлём не реже, чем WatchdogSec в ds-agent.service (там 30 c) — с запасом.
        # Если главный цикл зависнет, WATCHDOG=1 перестанет приходить и systemd
        # перезапустит службу.
        if now - last_watchdog >= 10:
            _sd_notify("WATCHDOG=1")
            last_watchdog = now

        # ── Держим экран включённым (signage не должен гаснуть/блокироваться) ──
        # Периодически повторяем на случай, если рабочий стол снова включил
        # хранитель экрана/DPMS после событий сессии.
        if now - last_awake >= 300:
            player.keep_screen_awake()
            last_awake = now

        # ── Опрос расписания и синхронизация файлов ──
        if now - last_schedule_poll >= POLL_INTERVAL:
            new_schedule = load_schedule_online(screen_id, sess)
            if new_schedule:
                schedule = new_schedule
                log.info("Расписание обновлено")
                dl, fail = downloader.sync_files(screen_id)
                if dl:
                    log.info("Скачано файлов: {}, ошибок: {}".format(dl, fail))
            elif schedule is None:
                schedule = load_schedule_cache()
                if schedule:
                    log.warning("Используем кешированное расписание")
            last_schedule_poll = now

        # ── Watchdog плеера (mpv) ──
        player.watchdog(current_playing)

        # ── Команды с сервера ──
        # WS: если push-уведомление — реагируем немедленно
        # Polling: fallback если WS не подключён
        ws_triggered = _ws_triggered.is_set()
        if ws_triggered:
            _ws_triggered.clear()
        if ws_triggered or (not ws_client.is_connected and now - last_cmd_poll >= CMD_POLL):
            resp = sync_hdl.poll_command()
            # ИСПРАВЛЕНО: poll_command возвращает {"command": {...}} или {"command": None}
            cmd = resp.get("command") if resp else None
            if cmd:
                log.info("Получена команда: {} (id={})".format(cmd.get("type"), cmd.get("id")))
                sync_hdl.handle(cmd)
            last_cmd_poll = now

        # ── Питание экрана (окно работы, МСК) ──
        now_msk = datetime.now(MSK)
        want_on = screen_should_be_on(schedule, now_msk) if schedule else True
        if want_on != display_on:
            set_display_power(want_on)
            display_on = want_on
            if not want_on:
                # Останавливаем показ, чтобы ночные «показы» не попадали
                # в play_log и не искажали биллинг.
                log.info("Окно выключения экрана — останавливаем показ")
                player.stop()
                current_list = []
                current_playing = None

        # ── Воспроизведение по расписанию ──
        if player.user_stopped:
            # Пользователь нажал «Стоп» — держим показ остановленным, не
            # возобновляем по расписанию (иначе цикл тут же запускал заново).
            # Снимается командой play/sync_play или перезапуском («Показ»).
            if current_list or current_playing:
                player.stop()
                current_list = []
                current_playing = None
        elif schedule and display_on:
            files = current_playlist_files(schedule)
            # Только существующие локально и не помеченные нерабочими
            playable = []
            for fname in files:
                if fname in broken_files:
                    continue
                if not (Path(MEDIA_DIR) / fname).exists():
                    log.warning("Файл отсутствует локально: " + fname)
                    continue
                if fname not in playable:
                    playable.append(fname)

            status = player.get_status()
            if playable:
                # Перезагружаем плейлист mpv, если состав изменился
                # или воспроизведение остановилось.
                if playable != current_list or not status.get("playing"):
                    if player.load_playlist(playable,
                                            image_durations=schedule.get("image_durations")):
                        current_list = playable
                    else:
                        current_list = []
                        if len(playable) == 1:
                            # Единственный файл не пошёл — значит, он и нерабочий
                            fname = playable[0]
                            log.warning("Пропускаем нерабочий ролик: " + fname)
                            broken_files.add(fname)
                            report_broken(sess, screen_id, fname, "playback did not start")
            else:
                if current_playing or current_list:
                    log.info("Вне расписания и нет заглушек — останавливаем")
                    player.stop()
                    current_list = []
                    current_playing = None

            # ── Лог показов: фиксируем каждый реальный старт ролика ──
            # mpv сам листает плейлист (--loop-playlist), поэтому следим за
            # сменой имени файла; повтор одного и того же файла ловим по
            # «отмотке» time-pos назад (новый круг цикла).
            playing_now = player.get_current_filename()
            if playing_now:
                tp = player.get_time_pos()
                if playing_now != current_playing:
                    current_playing = playing_now
                    last_time_pos = tp
                    report_play(sess, screen_id, playing_now)
                elif (tp is not None and last_time_pos is not None
                      and tp < last_time_pos - 2):
                    last_time_pos = tp
                    report_play(sess, screen_id, playing_now)   # новый круг
                elif tp is not None:
                    last_time_pos = tp

            # ── Громкость (день/ночь, МСК) ──
            vol = target_volume(schedule, now_msk)
            if vol != current_volume:
                player.set_volume(vol)
                current_volume = vol

        # ── Heartbeat ──
        if now - last_heartbeat >= HB_INTERVAL:
            hb.send(status="online", playing_file=current_playing)
            last_heartbeat = now

        # ── Очистка устаревших файлов (раз в сутки) ──
        if now - last_cleanup >= 86400:
            log.info("Запуск очистки файлов")
            cleanup.run()
            last_cleanup = now

        time.sleep(1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("ds-agent остановлен")
    except Exception as e:
        log.critical("Критическая ошибка: {}".format(e), exc_info=True)
        sys.exit(1)
