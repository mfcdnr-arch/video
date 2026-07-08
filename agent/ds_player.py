"""
ds_player.py — управление mpv через IPC Unix-сокет (JSON команды)
"""
import socket, json, time, threading, subprocess, logging, os
from pathlib import Path

log = logging.getLogger(__name__)
MPV_SOCKET = '/tmp/ds-mpv.sock'


class VLCPlayer:
    """Обёртка над mpv с тем же API что был у VLC."""

    def __init__(self, rc_port=4212, vlc_bin='/usr/bin/vlc',
                 media_dir='/opt/ds-agent/media'):
        self.media_dir = Path(media_dir)
        self._mpv_proc = None
        self._current_file = None
        self._lock = threading.Lock()

    def _mpv_cmd(self, command: list):
        """Отправить JSON команду mpv через IPC-сокет."""
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(3)
            s.connect(MPV_SOCKET)
            msg = json.dumps({"command": command}) + "\n"
            s.sendall(msg.encode())
            time.sleep(0.1)
            raw = s.recv(4096).decode(errors='replace')
            s.close()
            first = raw.strip().split('\n')[0]
            return json.loads(first) if first else {}
        except Exception as e:
            log.debug(f"mpv IPC: {e}")
            return {}

    def start_vlc(self):
        """Запустить mpv в режиме ожидания, полный экран, IPC-сокет."""
        if self._mpv_proc and self._mpv_proc.poll() is None:
            return

        env = os.environ.copy()
        if 'DISPLAY' not in env:
            env['DISPLAY'] = ':0'

        sock_path = Path(MPV_SOCKET)
        if sock_path.exists():
            sock_path.unlink()

        cmd = [
            '/usr/bin/mpv',
            '--fullscreen',
            '--no-osd-bar',
            # ВАЖНО: только loop-playlist. Раньше стоял ещё --loop-file=inf,
            # из-за него первый ролик плейлиста крутился вечно и до остальных
            # очередь не доходила (loop-file имеет приоритет над playlist).
            '--loop-playlist=inf',
            # Статичные баннеры (JPG/PNG) показываются по 10 секунд.
            # Число должно совпадать с IMAGE_DURATION в server/media_check.py —
            # столько же секунд баннеру пишется в duration_seconds для биллинга.
            '--image-display-duration=10',
            '--no-terminal',
            '--keep-open=yes',
            '--idle=yes',
            f'--input-ipc-server={MPV_SOCKET}',
        ]
        log.info("Запуск mpv")
        self._mpv_proc = subprocess.Popen(
            cmd, env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
        time.sleep(3)

    def stop_vlc(self):
        if self._mpv_proc:
            try:
                self._mpv_proc.terminate()
                self._mpv_proc.wait(timeout=5)
            except Exception:
                self._mpv_proc.kill()
            self._mpv_proc = None

    def is_vlc_running(self):
        return self._mpv_proc is not None and self._mpv_proc.poll() is None

    def watchdog(self, current_file=None):
        """Перезапустить mpv если упал."""
        if not self.is_vlc_running():
            log.warning("mpv не запущен — перезапускаем")
            Path(MPV_SOCKET).unlink(missing_ok=True)
            self.start_vlc()
            time.sleep(3)
            if current_file and self.is_vlc_running():
                time.sleep(1)
                self.play(current_file)

    def play(self, filename):
        """Загрузить и воспроизвести файл из media_dir."""
        path = self.media_dir / filename
        if not path.exists():
            log.error(f"Файл не найден: {path}")
            return False
        self._mpv_cmd(["loadfile", str(path), "replace"])
        self._current_file = filename
        log.info(f"▶ Воспроизведение: {filename}")
        return True

    def play_verified(self, filename, settle=2.0):
        """Воспроизвести файл и проверить что mpv действительно играет."""
        path = self.media_dir / filename
        if not path.exists():
            log.error(f"Файл не найден: {path}")
            return False
        self._mpv_cmd(["loadfile", str(path), "replace"])
        self._current_file = filename
        time.sleep(settle)
        st = self.get_status()
        if not st.get("playing"):
            log.warning(f"Воспроизведение не началось: {filename}")
            return False
        log.info(f"▶ Воспроизведение: {filename}")
        return True

    def play_at(self, filename, timestamp):
        """Начать воспроизведение ровно в timestamp (UNIX UTC float)."""
        wait = timestamp - time.time()
        if wait > 0:
            log.info(f"Sync: жду {wait:.3f}с до старта {filename}")
            time.sleep(wait)
        self.play(filename)

    def load_playlist(self, filenames, settle=2.0, image_durations=None):
        """Загрузить список файлов как плейлист mpv (крутится по кругу
        через --loop-playlist). image_durations: {filename: секунды} —
        индивидуальная длительность баннеров-картинок (per-file опция mpv,
        перекрывает общий --image-display-duration=10).
        Возвращает True, если воспроизведение пошло."""
        image_durations = image_durations or {}
        existing = [f for f in filenames if (self.media_dir / f).exists()]
        if not existing:
            log.warning("load_playlist: ни одного файла нет локально")
            return False

        def _load(fn, mode):
            cmd = ["loadfile", str(self.media_dir / fn), mode]
            dur = image_durations.get(fn)
            if dur:
                cmd.append("image-display-duration={}".format(int(dur)))
            self._mpv_cmd(cmd)

        _load(existing[0], "replace")
        for fn in existing[1:]:
            _load(fn, "append")
        self._current_file = existing[0]
        log.info("▶ Плейлист загружен: {} файл(ов): {}".format(
            len(existing), ", ".join(existing[:5]) + ("…" if len(existing) > 5 else "")))
        time.sleep(settle)
        return self.get_status().get("playing", False)

    def get_current_filename(self):
        """Имя файла, который mpv играет прямо сейчас (или None)."""
        r = self._mpv_cmd(["get_property", "filename"])
        if r.get("error") == "success":
            return r.get("data")
        return None

    def get_time_pos(self):
        """Текущая позиция воспроизведения в секундах (или None)."""
        r = self._mpv_cmd(["get_property", "time-pos"])
        if r.get("error") == "success":
            return r.get("data")
        return None

    def set_volume(self, volume: int):
        """Громкость 0-100."""
        self._mpv_cmd(["set_property", "volume", max(0, min(100, int(volume)))])
        log.info(f"Громкость: {volume}")

    def stop(self):
        self._mpv_cmd(["stop"])
        self._current_file = None

    def clear_playlist(self):
        self._mpv_cmd(["playlist-clear"])

    def get_status(self):
        r_pause = self._mpv_cmd(["get_property", "pause"])
        r_idle = self._mpv_cmd(["get_property", "idle-active"])
        paused = r_pause.get("data", True)
        idle = r_idle.get("data", True)
        playing = (not paused) and (not idle)
        return {"playing": playing, "filename": self._current_file}

    def set_marquee(self, text, color="#ffd34d"):
        """Показать текст поверх видео через OSD mpv."""
        safe = text.replace('"', "'")
        self._mpv_cmd(["show-text", safe, 10000])
        log.info(f"OSD: {safe[:60]}")

    def clear_marquee(self):
        """Скрыть OSD."""
        self._mpv_cmd(["show-text", "", 1])
