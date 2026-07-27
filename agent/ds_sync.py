"""
ds_sync.py — обработка команд синхронного воспроизведения от сервера
"""
import time, logging, requests
from ds_player import VLCPlayer

log = logging.getLogger(__name__)


class SyncHandler:
    def __init__(self, server, token, player, screen_id):
        self.server = server.rstrip("/")
        self.token = token
        self.player = player
        self.screen_id = screen_id
        self.session = requests.Session()
        self.session.headers["X-Token"] = token

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
