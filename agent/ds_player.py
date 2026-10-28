"""
ds_player.py — управление mpv через IPC Unix-сокет (JSON команды)
"""
from __future__ import annotations
import socket, json, time, threading, subprocess, logging, os
from datetime import datetime
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)
MPV_SOCKET = '/tmp/ds-mpv.sock'

# Повторный опрос ленты сразу после active_until: не чаще раза в
# EXPIRED_REPOLL_EVERY с и только первые EXPIRED_REPOLL_WINDOW с после него.
EXPIRED_REPOLL_EVERY = 5
EXPIRED_REPOLL_WINDOW = 120


def _active_until(schedule) -> datetime | None:
    """active_until ленты (наивное МСК) или None: нет поля, null или мусор."""
    raw = (schedule or {}).get("active_until")
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw).replace(tzinfo=None)
    except ValueError:
        return None


def active_expired(schedule: dict, now: datetime) -> bool:
    """Истёк ли активный источник ленты (показ — с концом часа, переопределение
    — в полночь). now — datetime.now(MSK): сравниваем московские «настенные»
    часы, tzinfo отбрасываем. Нет active_until или он кривой — не истёк
    (эфир сети, шаблон, лента старого сервера)."""
    until = _active_until(schedule)
    return until is not None and now.replace(tzinfo=None) >= until


def repoll_after_expiry(schedule: dict, now: datetime, seconds_since_poll: float) -> bool:
    """Опросить ленту раньше обычного интервала: активный источник только что истёк.

    Многочасовой показ получает active_until на конец каждого часа; без этого
    на стыке часов экран до интервала опроса играл бы шаблон и потом начинал
    показ заново. Только первые EXPIRED_REPOLL_WINDOW с после истечения и не
    чаще раза в EXPIRED_REPOLL_EVERY с — без связи сервер не долбим.
    """
    until = _active_until(schedule)
    if until is None:
        return False
    late = (now.replace(tzinfo=None) - until).total_seconds()
    return 0 <= late < EXPIRED_REPOLL_WINDOW and seconds_since_poll >= EXPIRED_REPOLL_EVERY


def build_playable(files: list[str], broken: set[str],
                   exists: Callable[[str], bool]) -> list[str]:
    """Список для mpv из расписания. Порядок и повторы сохраняются (плейлист ×N,
    круги заглушек), выкидываются только нерабочие и отсутствующие локально файлы.
    Логирование отсутствующих делает вызывающий код."""
    return [f for f in files if f not in broken and exists(f)]


# Отмотка time-pos назад больше чем на столько секунд — новый круг
# плейлиста из одной записи (mpv перезапустил тот же файл).
REWIND_SECONDS = 2


def is_new_play(prev: dict | None, cur: dict) -> bool:
    """Начался ли новый показ между двумя замерами состояния mpv.

    prev, cur — {"pos", "count", "filename", "time_pos"} из
    VLCPlayer.get_play_state(): playlist-pos, playlist-count, имя файла записи
    pos и позиция в секундах; любое поле может быть None (mpv не ответил).
    prev=None — прошлого замера нет (старт агента, только что загружен
    плейлист, показ возобновлён): играющий файл — новый показ.

    Новый показ, если:
    - mpv перешёл на другую запись плейлиста (playlist-pos сменился — и
      следующий ролик, и новый круг N-1 → 0). Так считаются картинки и
      короткие ролики, повторённые ×N: у картинки time-pos стоит около нуля,
      по отмотке времени их повторы не отличить;
    - на той же позиции другой файл или плейлист другой длины (его подменили);
    - плейлист из одной записи: mpv крутит её по кругу, новый круг виден только
      по отмотке time-pos назад. Только здесь: в длинном плейлисте отмотка
      совпадает со сменой записи, и при опросе свойств по очереди один старт
      мог посчитаться дважды.
    Без playlist-pos (старый mpv, сбой IPC) — как раньше: смена имени файла
    или отмотка time-pos.
    """
    if not cur or not cur.get("filename"):
        return False
    if not prev or not prev.get("filename"):
        return True
    pos, prev_pos = cur.get("pos"), prev.get("pos")
    if pos is not None and prev_pos is not None:
        if pos != prev_pos or cur["filename"] != prev["filename"]:
            return True
        count, prev_count = cur.get("count"), prev.get("count")
        if count is not None and prev_count is not None and count != prev_count:
            return True
        if count != 1:
            return False
    elif cur["filename"] != prev["filename"]:
        return True
    tp, prev_tp = cur.get("time_pos"), prev.get("time_pos")
    return tp is not None and prev_tp is not None and tp < prev_tp - REWIND_SECONDS


class VLCPlayer:
    """Обёртка над mpv с тем же API что был у VLC."""

    def __init__(self, player_bin='/usr/bin/mpv',
                 media_dir='/opt/ds-agent/media', ontop=True):
        self.player_bin = player_bin or '/usr/bin/mpv'
        self.media_dir = Path(media_dir)
        # Держать окно плеера поверх остальных и без рамки. На fly-wm (Astra)
        # одного --fullscreen мало: панель окружения и всплывающие окна
        # перекрывают видео. Отключается в config.ini (player_ontop = false),
        # если на конкретном железе поверх-режим мешает.
        self.ontop = ontop
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
            # mpv шлёт события ({"event": ...}) всем клиентам; на стыке роликов
            # событие может прийти раньше ответа — берём первую строку-ответ.
            for line in raw.strip().split('\n'):
                msg = json.loads(line) if line.strip() else {}
                if msg and "event" not in msg:
                    return msg
            return {}
        except Exception as e:
            log.debug(f"mpv IPC: {e}")
            return {}

    def keep_screen_awake(self, env=None):
        """Отключить гашение экрана и DPMS, чтобы signage-экран не гас и не
        уходил в блокировку по простою. Требует доступ к X (DISPLAY/.Xauthority) —
        тот же, что и для запуска mpv. Ошибки не критичны (например, нет xset)."""
        if env is None:
            env = os.environ.copy()
            env.setdefault('DISPLAY', ':0')
        for args in (["xset", "s", "off"], ["xset", "s", "noblank"], ["xset", "-dpms"]):
            try:
                subprocess.run(args, env=env, timeout=3,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception as e:
                log.debug("xset {}: {}".format(" ".join(args), e))

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

        # Не давать экрану гаснуть/блокироваться по простою (signage).
        self.keep_screen_awake(env)

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
        if self.ontop:
            # --ontop: не давать окружению перекрывать видео;
            # --no-border: без заголовка окна, если WM не отдал полный экран.
            cmd += ['--ontop', '--no-border']
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
                # kill без последующего wait оставлял зомби [mpv] <defunct>
                # до перезапуска агента (замечено при приёмке на мини-ПК)
                try:
                    self._mpv_proc.kill()
                    self._mpv_proc.wait(timeout=3)
                except Exception:
                    pass
            self._mpv_proc = None

    def is_vlc_running(self):
        return self._mpv_proc is not None and self._mpv_proc.poll() is None

    def watchdog(self, current_file=None):
        """Перезапустить mpv если упал."""
        if not self.is_vlc_running():
            log.warning("mpv не запущен — перезапускаем")
            # Path.unlink(missing_ok=...) — только с Python 3.8, на Astra 3.7
            try:
                Path(MPV_SOCKET).unlink()
            except FileNotFoundError:
                pass
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

    def _get_int_property(self, name):
        r = self._mpv_cmd(["get_property", name])
        data = r.get("data") if r.get("error") == "success" else None
        return data if isinstance(data, int) and not isinstance(data, bool) else None

    def get_play_state(self):
        """Замер для учёта показов (см. is_new_play): {"pos", "count",
        "filename", "time_pos"}, недоступное — None.

        Имя файла берём из записи playlist/<pos>/filename, а не из свойства
        filename: свойства читаются по очереди, и на стыке роликов «старая
        позиция + новое имя» посчитали бы один старт дважды. Запись по
        позиции всегда согласована с самой позицией. Нет позиции (старый mpv,
        простой) — прежнее свойство filename. time-pos нужен только плейлисту
        из одной записи (или без позиции) — в остальных случаях не спрашиваем.
        """
        pos = self._get_int_property("playlist-pos")
        if pos is not None and pos < 0:
            return {"pos": None, "count": None, "filename": None, "time_pos": None}
        count = self._get_int_property("playlist-count") if pos is not None else None
        if pos is not None:
            r = self._mpv_cmd(["get_property", "playlist/{}/filename".format(pos)])
            path = r.get("data") if r.get("error") == "success" else None
            filename = os.path.basename(path) if isinstance(path, str) and path else None
        else:
            filename = self.get_current_filename()
        time_pos = self.get_time_pos() if (pos is None or count == 1) and filename else None
        return {"pos": pos, "count": count, "filename": filename, "time_pos": time_pos}

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

    @staticmethod
    def _speed_to_pps(speed):
        """Скорость прокрутки → пикселей/сек. Принимает slow|medium|fast или число."""
        table = {"slow": 80, "medium": 140, "fast": 220}
        if speed is None:
            return table["medium"]
        if isinstance(speed, str):
            s = speed.strip().lower()
            if s in table:
                return table[s]
            try:
                return max(20, int(float(s)))
            except ValueError:
                return table["medium"]
        try:
            return max(20, int(speed))
        except (TypeError, ValueError):
            return table["medium"]

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

    def _marquee_loop(self, text, ass_color, speed, duration):
        RES_X, RES_Y = self.MARQUEE_RES_X, self.MARQUEE_RES_Y
        fs = 42                       # размер шрифта
        y = RES_Y - 12                # почти у нижнего края (an1 = низ-лево)
        pps = self._speed_to_pps(speed)             # пикселей в секунду
        interval = 0.03               # ~33 кадра/сек
        safe = self._ass_escape(text)
        text_w = int(len(safe) * fs * 0.6) + 80     # грубая ширина строки
        span = RES_X + text_w                        # полный путь одного прохода
        stop = self._marquee_stop
        start = time.monotonic()
        sock = None
        # Покадровая прокрутка: позиция от РЕАЛЬНОГО времени (ровная скорость), а
        # координата X — float → субпиксельный рендер libass сглаживает движение
        # (заметно меньше «ряби», чем при целочисленных шагах). Надёжно на любом
        # mpv (ASS-анимация \move у части сборок в оверлее не проигрывается).
        while stop is not None and not stop.is_set():
            elapsed = time.monotonic() - start
            if duration and elapsed >= duration:
                break
            x = RES_X - (elapsed * pps) % span
            try:
                if sock is None:
                    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    sock.settimeout(2)
                    sock.connect(MPV_SOCKET)
                tags = ("{\\an1\\pos(%.1f,%d)\\fs%d\\c%s\\bord2\\3c&H000000&\\shad1}"
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
        # авто-снятие по времени: сбросить состояние, если это ещё активный поток
        if duration and self._marquee_thread is threading.current_thread():
            self._marquee_thread = None
            self._marquee_stop = None
            self._marquee_current = None

    def set_marquee(self, text, color="#ffd34d", speed=None, duration=0):
        """Запустить/обновить постоянную бегущую строку внизу экрана.
        duration: секунды показа; 0/None — бессрочно (до снятия). Идемпотентно:
        если ровно та же строка (текст/цвет/скорость/время) уже бежит — не трогаем
        (повторная доставка ticker_show не должна вызывать мигание)."""
        text = (text or "").replace("\n", " ").strip()
        ass_color = self._hex_to_ass(color)
        try:
            duration = max(0, int(float(duration))) if duration else 0
        except (TypeError, ValueError):
            duration = 0
        key = (text, ass_color, str(speed), duration)
        if (text and self._marquee_thread is not None
                and self._marquee_thread.is_alive()
                and self._marquee_current == key):
            return
        self.clear_marquee()
        if not text:
            return
        self._marquee_current = key
        self._marquee_stop = threading.Event()
        self._marquee_thread = threading.Thread(
            target=self._marquee_loop, args=(text, ass_color, speed, duration), daemon=True)
        self._marquee_thread.start()
        log.info("Бегущая строка (цвет {}, скорость {}, время {}): {}".format(
            ass_color, speed, (str(duration) + "с") if duration else "∞", text[:60]))

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
