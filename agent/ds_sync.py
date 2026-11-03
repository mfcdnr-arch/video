"""
ds_sync.py — обработка команд синхронного воспроизведения от сервера
"""
import time, logging, requests
from ds_player import VLCPlayer

log = logging.getLogger(__name__)

# Команды, после которых процесс агента (или весь мини-ПК) перезапускается:
# следующую команду забирает уже новый процесс.
_PROCESS_ENDING = ("restart_agent", "reboot", "update_agent")


# Сколько команд разбирается за один проход главного цикла.
DRAIN_MAX = 10

# Пока просьба «перечитай ленту» (ticker_show / ticker_hide) не выполнена, пробуем не чаще раза в столько секунд.
FEED_RETRY = 5


def need_poll(ws_triggered, ws_connected, since_last_poll, cmd_poll, backlog):
    """Пора ли спрашивать у сервера команды в этом проходе главного цикла.

    Да, если: пришёл WS-сигнал; или WS не подключён и прошёл интервал опроса
    cmd_poll; или прошлый проход упёрся в лимит DRAIN_MAX (backlog) и в очереди
    могли остаться команды. Последнее нужно при подключённом WS: сигналы
    сливаются в один флаг, а цикл бывает занят долго (скачивание файлов), так
    что накопленный остаток иначе ждал бы нового push — он может и не прийти.
    """
    return bool(ws_triggered or backlog or (not ws_connected and since_last_poll >= cmd_poll))


def command_pass(sync_hdl, ws_triggered, ws_connected, since_last_poll, cmd_poll, backlog):
    """Часть прохода главного цикла «команды с сервера»: (опрашивали ли, остался ли остаток).

    Остаток есть, когда разобрано ровно DRAIN_MAX команд (больше сервера не
    спрашивали): следующий проход опросит снова, не дожидаясь сигнала. Остановка
    по повторной неподтверждённой команде или после restart_agent / reboot /
    update_agent остатком не считается — иначе цикл опрашивал бы вхолостую.
    """
    if not need_poll(ws_triggered, ws_connected, since_last_poll, cmd_poll, backlog):
        return False, False
    return True, drain_commands(sync_hdl, DRAIN_MAX) >= DRAIN_MAX


def drain_commands(sync_hdl, max_n=DRAIN_MAX):
    """Обработать ожидающие команды за одно пробуждение (WS-будильник или тик
    опроса). Сервер отдаёт по одной команде за запрос, а «Стоп» и «Показ»
    приходят парой с ticker_hide / ticker_show — одной команды за пробуждение
    мало. Не больше max_n за вызов; если разобрано ровно max_n, команды могли
    остаться: command_pass опросит снова в следующем проходе цикла (сама по себе
    эта функция остаток не дожидается).

    Останавливаемся, когда команд нет; когда сервер вернул ту же команду, что
    уже разбирали (её не удалось подтвердить — иначе будем разбирать вечно); и
    после restart_agent / reboot / update_agent, которые заканчивают процесс.
    Возвращает, сколько команд разобрано.
    """
    handled, seen = 0, set()
    while handled < max_n:
        resp = sync_hdl.poll_command()
        cmd = resp.get("command") if resp else None
        if not cmd:
            break
        cmd_id = cmd.get("id")
        if cmd_id is not None and cmd_id in seen:
            break
        seen.add(cmd_id)
        log.info("Получена команда: {} (id={})".format(cmd.get("type"), cmd_id))
        sync_hdl.handle(cmd)
        handled += 1
        if cmd.get("type") in _PROCESS_ENDING:
            break
    return handled


class SyncHandler:
    def __init__(self, server, token, player, screen_id):
        self.server = server.rstrip("/")
        self.token = token
        self.player = player
        self.screen_id = screen_id
        self.session = requests.Session()
        self.session.headers["X-Token"] = token
        # Команда ticker_show / ticker_hide в режиме «строки из ленты» только
        # поднимает флаг; главный цикл сразу перечитывает ленту и сбрасывает его.
        self.refresh_requested = False

    # Просьба «перечитай ленту» живёт, пока лента не прочитана: флаг снимает feed_loaded() после УДАЧНОЙ
    # загрузки, а не главный цикл перед попыткой — иначе один неудачный GET откладывал срочную правку
    # до очередного опроса через минуту. Пока флаг поднят, пробуем не чаще FEED_RETRY секунд.
    _refresh_tried_at = 0.0

    def feed_refresh_due(self, now):
        """Пора ли читать ленту по просьбе бегущей строки: флаг поднят и с прошлой попытки прошло не меньше
        FEED_RETRY секунд (первая попытка — сразу). `now` — time.time() главного цикла; часы отмотали
        назад (синхронизация времени) — не ждём «вечно», пробуем сразу. Одна попытка за проход цикла."""
        if not self.refresh_requested:
            return False
        if now < self._refresh_tried_at or now - self._refresh_tried_at >= FEED_RETRY:
            self._refresh_tried_at = now
            return True
        return False

    def feed_loaded(self):
        """Лента прочитана: просьба выполнена, ограничение частоты снято (следующая команда строки не ждёт)."""
        self.refresh_requested = False
        self._refresh_tried_at = 0.0

    def _url(self, path):
        return self.server + path

    def poll_command(self):
        """
        Опросить сервер. Возвращает {"command": {...}} или {"command": None}.
        Вызывающий код должен делать resp.get("command") чтобы получить саму команду.
        """
        try:
            r = self.session.get(
                self._url("/api/command/poll/{}".format(self.screen_id)),
                timeout=10
            )
            r.raise_for_status()
            return r.json()
        except Exception as e:
            log.debug("Ошибка опроса команд: {}".format(e))
            return {"command": None}

    def ack_command(self, command_id):
        try:
            self.session.post(
                self._url("/api/command/ack/{}".format(command_id)),
                timeout=5
            )
        except Exception as e:
            log.warning("Не удалось подтвердить команду {}: {}".format(command_id, e))

    def handle(self, command):
        """
        Обрабатывает команду. command — это уже сам объект команды (dict с id, type, payload),
        НЕ обёртка {"command": ...}.
        """
        cmd_type = command.get("type")
        payload  = command.get("payload") or {}
        cmd_id   = command.get("id")

        if cmd_type == "sync_play":
            self.player.user_stopped = False   # явный показ снимает «Стоп»
            filename = payload.get("filename")
            play_at  = payload.get("play_at")  # UNIX timestamp (float)

            if not filename or play_at is None:
                log.error("Неверный payload sync_play: {}".format(payload))
                self.ack_command(cmd_id)
                return False

            # ИСПРАВЛЕНО: play_at — это уже float (UNIX timestamp), не ISO строка
            try:
                play_at_ts = float(play_at)
            except (TypeError, ValueError):
                log.error("Неверный play_at: {}".format(play_at))
                self.ack_command(cmd_id)
                return False

            wait = play_at_ts - time.time()
            if wait < -5:
                log.warning("Команда sync_play опоздала на {:.1f}с, пропускаем".format(-wait))
                self.ack_command(cmd_id)
                return False

            log.info("Sync play: {} через {:.2f}с".format(filename, max(0, wait)))
            self.player.clear_playlist()
            self.player.play_at(filename, play_at_ts)
            self.ack_command(cmd_id)
            return True

        elif cmd_type == "stop":
            log.info("Команда: STOP")
            self.player.user_stopped = True   # держим остановку до «Показ»/play
            self.player.stop()
            self.ack_command(cmd_id)
            return True

        elif cmd_type == "resume":
            log.info("Команда: RESUME")
            self.player.user_stopped = False  # снять «Стоп» — цикл сам возобновит показ
            self.ack_command(cmd_id)
            return True

        elif cmd_type == "play":
            self.player.user_stopped = False   # явный показ снимает «Стоп»
            filename = payload.get("filename")
            if filename:
                log.info("Команда: PLAY " + filename)
                self.player.clear_playlist()
                self.player.play(filename)
                self.ack_command(cmd_id)
                return True

        elif cmd_type == "ticker_show":
            if self.player.tickers_from_feed:
                # Агент 1.4.0 берёт строки из ленты: команда лишь «обнови сейчас».
                # Текст из payload не показываем — лента точнее (срок, очередь).
                log.info("Команда: TICKER_SHOW — обновляю строки из ленты")
                self.refresh_requested = True
                self.ack_command(cmd_id)
                return True
            message  = payload.get("message", "")
            color    = payload.get("color", "#ffd34d")
            speed    = payload.get("speed")
            duration = payload.get("duration", 0)
            log.info("Команда: TICKER_SHOW")
            try:
                self.player.set_marquee(message, color, speed, duration)
            except Exception as e:
                log.warning("Не удалось показать бегущую строку: {}".format(e))
            self.ack_command(cmd_id)
            return True

        elif cmd_type == "ticker_hide":
            if self.player.tickers_from_feed:
                log.info("Команда: TICKER_HIDE — обновляю строки из ленты")
                self.refresh_requested = True
                self.ack_command(cmd_id)
                return True
            log.info("Команда: TICKER_HIDE")
            try:
                self.player.clear_marquee()
            except Exception as e:
                log.warning("Не удалось снять бегущую строку: {}".format(e))
            self.ack_command(cmd_id)
            return True

        elif cmd_type == "collect_diag":
            # Панель запросила архив диагностики: собираем и загружаем на
            # сервер. ack — в любом случае, чтобы команда не зациклилась.
            log.info("Команда: COLLECT_DIAG")
            self.ack_command(cmd_id)
            try:
                self._collect_and_upload_diag()
            except Exception as e:
                log.warning("Диагностика не собрана: {}".format(e))
            return True

        elif cmd_type == "update_agent":
            log.info("Команда: UPDATE_AGENT")
            self.ack_command(cmd_id)
            try:
                from ds_ota_updater import apply_update
                result = apply_update(self.server, self.token, payload)
                if not result:
                    log.error("OTA-обновление не удалось")
            except Exception as e:
                log.error(f"OTA: необработанная ошибка: {e}", exc_info=True)
            return True

        elif cmd_type == "reboot":
            log.info("Команда: REBOOT")
            self.ack_command(cmd_id)
            import subprocess
            subprocess.run(["sudo", "reboot"])
            return True

        elif cmd_type == "restart_agent":
            log.info("Команда: RESTART_AGENT — перезапуск агента")
            self.ack_command(cmd_id)
            import subprocess, sys, os
            # Перезапуск через systemd если возможно, иначе — exec самого себя
            try:
                result = subprocess.run(
                    ["sudo", "systemctl", "restart", "ds-agent"],
                    capture_output=True, timeout=5
                )
                if result.returncode != 0:
                    raise RuntimeError("systemctl вернул {}".format(result.returncode))
                log.info("Агент перезапущен через systemctl")
            except Exception as e:
                log.warning("systemctl недоступен ({}), перезапуск через exec".format(e))
                os.execv(sys.executable, [sys.executable] + sys.argv)
            return True

        else:
            log.warning("Неизвестная команда: " + str(cmd_type))
            self.ack_command(cmd_id)

        return False

    # ── Диагностика по запросу из панели ────────────────────────────────────

    def _collect_and_upload_diag(self):
        """Собрать архив диагностики (та же логика, что collect_diag_agent.sh)
        и загрузить на сервер. Только stdlib; каждый источник — независимо,
        ошибка одного не срывает остальные."""
        import io, os, subprocess, tarfile, time

        def run(cmd, timeout=15):
            try:
                r = subprocess.run(cmd, capture_output=True, timeout=timeout)
                out = r.stdout.decode(errors="ignore")
                err = r.stderr.decode(errors="ignore")
                return out + (("\n[stderr]\n" + err) if err.strip() else "")
            except Exception as e:
                return "ошибка выполнения {}: {}\n".format(cmd, e)

        parts = {
            "journalctl_ds-agent.txt": run(
                ["journalctl", "-u", "ds-agent", "-n", "500", "--no-pager"]),
            "system.txt": (run(["uname", "-a"]) + "\n" + run(["uptime"]) +
                           "\n" + run(["df", "-h"]) + "\n" + run(["free", "-m"])),
            "player.txt": (run(["pgrep", "-a", "mpv"]) +
                           "\n" + run(["mpv", "--version"]).split("\n")[0]),
            "clock.txt": run(["chronyc", "tracking"]) + "\n" + run(["date"]),
        }
        try:
            with open("/var/log/ds-agent/ds-agent.log", "rb") as f:
                f.seek(0, 2)
                f.seek(max(0, f.tell() - 200_000))     # хвост ~200 КБ
                parts["ds-agent.log.tail.txt"] = f.read().decode(errors="ignore")
        except Exception as e:
            parts["ds-agent.log.tail.txt"] = "лог недоступен: {}".format(e)
        try:
            with open(os.getenv("DS_CONFIG", "/etc/ds-agent/config.ini")) as f:
                cfg = []
                for line in f:
                    # токен устройства в архив не попадает
                    cfg.append("token = ***СКРЫТ***\n"
                               if line.strip().lower().startswith("token") else line)
                parts["config.ini.txt"] = "".join(cfg)
        except Exception as e:
            parts["config.ini.txt"] = "конфиг недоступен: {}".format(e)

        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            for name, content in parts.items():
                data = content.encode(errors="ignore")
                info = tarfile.TarInfo(name)
                info.size = len(data)
                info.mtime = int(time.time())
                tar.addfile(info, io.BytesIO(data))
        buf.seek(0)

        r = self.session.post(
            self._url("/api/diagnostics/upload/{}".format(self.screen_id)),
            files={"file": ("diag.tar.gz", buf, "application/gzip")},
            timeout=60)
        r.raise_for_status()
        log.info("Диагностика загружена на сервер: {}".format(
            r.json().get("filename", "?")))
