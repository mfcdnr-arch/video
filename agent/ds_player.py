"""
ds_player.py — управление mpv через IPC Unix-сокет (JSON команды)
"""
import socket, json, time, threading, subprocess, logging, os
from pathlib import Path

log = logging.getLogger(__name__)
MPV_SOCKET = '/tmp/ds-mpv.sock'


class VLCPlayer:
    """Обёртка над mpv с тем же API что был у VLC."""

    def __init__(self, player_bin='/usr/bin/mpv',
                 media_dir='/opt/ds-agent/media'):
        self.player_bin = player_bin or '/usr/bin/mpv'
        self.media_dir = Path(media_dir)
        self._mpv_proc = None
        self._current_file = None
        self._lock = threading.Lock()
        self._marquee_thread = None
        self._marquee_stop = None
        self._marquee_current = None
        # Устойчивый флаг «остановлено пользователем» (кнопка «Стоп» в панели).
        # Пока True — главный цикл не возобновляет показ по расписанию. Снимается
        # явной командой play/sync_play или перезапуском агента (кнопка «Показ»).
        self.user_stopped = False

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

        # Убить осиротевшие mpv от прошлых запусков агента. После systemctl restart
        # у нового процесса агента self._mpv_proc = None, поэтому он стартует свежий
        # mpv, НЕ трогая mpv, запущенные предыдущим процессом агента → экземпляры
        # копятся, дерутся за экран, а IPC-сокет захватывает лишь один из них
        # (команды/OSD уходят не в то окно, что видно на экране). Оставляем ровно один.
        try:
            subprocess.run(["pkill", "-9", "-x", "mpv"], timeout=5)
            time.sleep(0.5)
        except Exception as e:
            log.debug(f"pkill mpv: {e}")

        env = os.environ.copy()
        if 'DISPLAY' not in env:
            env['DISPLAY'] = ':0'

        sock_path = Path(MPV_SOCKET)
        if sock_path.exists():
            sock_path.unlink()

        cmd = [
            self.player_bin,
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
        # ВАЖНО: loadfile ... replace в части версий mpv заменяет только ТЕКУЩУЮ
        # запись, оставляя записи предыдущего плейлиста в очереди. С
        # --loop-playlist=inf старый плейлист продолжал крутиться вместе с новым.
        # playlist-clear убирает все записи, кроме текущей (только что загруженной).
        self._mpv_cmd(["playlist-clear"])
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

    # ── Бегущая строка (ticker) ─────────────────────────────────────────────
    # Постоянная строка ВНИЗУ экрана, прокрутка справа налево, заданным цветом.
    # Держится бессрочно (пока не вызовут clear_marquee). Реализована через
    # osd-overlay (ASS): фоновый поток раз в кадр перерисовывает текст со сдвигом
    # по X. Это заменяет прежний show-text (разовый OSD на 10 сек по центру).
    MARQUEE_OVERLAY_ID = 47
    MARQUEE_RES_X = 1280
    MARQUEE_RES_Y = 720

    def _mpv_send_quick(self, command):
        """Лёгкая отправка команды mpv: подключиться, послать, выгрести ответ,
        закрыть. Без sleep — для частой перерисовки бегущей строки. Каждый вызов
        закрывает соединение → ответы mpv не копятся в буфере."""
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(1)
            s.connect(MPV_SOCKET)
            s.sendall((json.dumps({"command": command}) + "\n").encode())
            try:
                s.recv(4096)
            except Exception:
                pass
            s.close()
            return True
        except Exception:
            return False

    @staticmethod
    def _hex_to_ass(color):
        """#RRGGBB → ASS-цвет &HBBGGRR& (порядок B,G,R). По умолчанию — жёлтый."""
        c = (color or "").strip().lstrip("#")
        if len(c) == 6:
            try:
                r, g, b = c[0:2], c[2:4], c[4:6]
                return "&H{}{}{}&".format(b, g, r).upper()
            except Exception:
                pass
        return "&H4DD3FF&"

    @staticmethod
    def _ass_escape(text):
        """Обезвредить спецсимволы ASS в пользовательском тексте."""
        return (text or "").replace("\\", "/").replace("{", "(").replace("}", ")")

    def _marquee_send(self, sock, command):
        """Отправить команду mpv по постоянному соединению: сначала вычерпать
        накопившиеся ответы/события (не блокируясь), потом послать. Иначе буфер
        ответов mpv переполнится и его event-loop может застопориться."""
        sock.setblocking(False)
        try:
            while sock.recv(65536):
                pass
        except (BlockingIOError, OSError):
            pass
        sock.setblocking(True)
        sock.settimeout(2)
        sock.sendall((json.dumps({"command": command}) + "\n").encode())

    def _marquee_loop(self, text, ass_color, speed):
        RES_X, RES_Y = self.MARQUEE_RES_X, self.MARQUEE_RES_Y
        fs = 42                       # размер шрифта
        y = RES_Y - 12                # почти у нижнего края (an1 = низ-лево)
        step = max(1, int(speed)) if speed else 8   # пикселей за кадр
        interval = 0.04               # ~25 кадров/сек
        safe = self._ass_escape(text)
        text_w = int(len(safe) * fs * 0.6) + 60     # грубая ширина для сброса
        x = RES_X
        stop = self._marquee_stop
        sock = None
        while stop is not None and not stop.is_set():
            try:
                if sock is None:
                    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    sock.settimeout(2)
                    sock.connect(MPV_SOCKET)
                tags = ("{\\an1\\pos(%d,%d)\\fs%d\\c%s\\bord2\\3c&H000000&\\shad1}"
                        % (x, y, fs, ass_color))
                self._marquee_send(sock, ["osd-overlay", self.MARQUEE_OVERLAY_ID,
                                          "ass-events", tags + safe, RES_X, RES_Y])
            except Exception:
                # соединение отвалилось (mpv перезапущен) — переподключимся
                try:
                    if sock:
                        sock.close()
                except Exception:
                    pass
                sock = None
                stop.wait(0.4)
                continue
            x -= step
            if x < -text_w:
                x = RES_X
            stop.wait(interval)
        # убрать оверлей
        try:
            if sock is None:
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                sock.settimeout(1)
                sock.connect(MPV_SOCKET)
            self._marquee_send(sock, ["osd-overlay", self.MARQUEE_OVERLAY_ID, "none", ""])
        except Exception:
            pass
        try:
            if sock:
                sock.close()
        except Exception:
            pass

    def set_marquee(self, text, color="#ffd34d", speed=None):
        """Запустить/обновить постоянную бегущую строку внизу экрана.
        Идемпотентно: если ровно та же строка уже бежит — ничего не трогаем
        (иначе повторные ticker_show на каждом опросе снимали бы и запускали
        строку заново → мигание)."""
        text = (text or "").replace("\n", " ").strip()
        ass_color = self._hex_to_ass(color)
        if (text and self._marquee_thread is not None
                and self._marquee_thread.is_alive()
                and self._marquee_current == (text, ass_color)):
            return
        self.clear_marquee()
        if not text:
            return
        self._marquee_current = (text, ass_color)
        self._marquee_stop = threading.Event()
        self._marquee_thread = threading.Thread(
            target=self._marquee_loop, args=(text, ass_color, speed), daemon=True)
        self._marquee_thread.start()
        log.info("Бегущая строка (цвет {}): {}".format(ass_color, text[:60]))

    def clear_marquee(self):
        """Остановить бегущую строку и убрать оверлей."""
        if self._marquee_stop is not None:
            self._marquee_stop.set()
        th = self._marquee_thread
        if th and th.is_alive() and th is not threading.current_thread():
            th.join(timeout=1.5)
        self._marquee_thread = None
        self._marquee_stop = None
        self._marquee_current = None
        # подстраховка: убрать оверлей, даже если поток уже мёртв
        self._mpv_send_quick(["osd-overlay", self.MARQUEE_OVERLAY_ID, "none", ""])
