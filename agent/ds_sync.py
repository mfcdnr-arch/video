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
            self.player.stop()
            self.ack_command(cmd_id)
            return True

        elif cmd_type == "play":
            filename = payload.get("filename")
            if filename:
                log.info("Команда: PLAY " + filename)
                self.player.clear_playlist()
                self.player.play(filename)
                self.ack_command(cmd_id)
                return True

        elif cmd_type == "ticker_show":
            message = payload.get("message", "")
            color   = payload.get("color", "#ffd34d")
            log.info("Команда: TICKER_SHOW")
            try:
                self.player.set_marquee(message, color)
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
