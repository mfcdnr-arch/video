"""
ds_player.py — управление mpv через IPC Unix-сокет (JSON команды)
"""
from __future__ import annotations
import socket, json, time, threading, subprocess, logging, os
from datetime import datetime, timedelta, timezone
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


# ── Очередь бегущих строк (агент 1.4.0) ──────────────────────────────────────
# Сервер отдаёт в ленте экрана `tickers` (по порядку запуска); агент сам знает
# срок каждой (`until_at`, наивное МСК) и крутит живые по очереди.

_MSK = timezone(timedelta(hours=3))     # у Москвы нет сезонного перевода часов

TICKER_FRAME = 0.03     # пауза между кадрами прокрутки (~33 кадра/с)
TICKER_IDLE = 0.25      # очередь пуста или пауза: состояние проверяем 4 раза в секунду
TICKER_RETRY = 0.4      # mpv не отвечает (перезапуск): пауза перед новой попыткой
TICKER_DRAWN_WINDOW = 2.0   # «бежит» в отчёте, только если кадр дошёл до mpv не позже, чем эти секунды назад


def _msk_now() -> datetime:
    """Часы агента для сроков строк: наивное МСК."""
    return datetime.now(_MSK).replace(tzinfo=None)


def _ticker_text(message) -> str:
    """Текст строки одной линией; пустая строка — показывать нечего."""
    if not isinstance(message, str):
        return ""
    return message.replace("\r", " ").replace("\n", " ").strip()


def live_tickers(items, now: datetime) -> list:
    """Живые строки из ленты: `until_at` (наивное МСК) ещё не наступил по часам
    агента `now` (aware-время тоже годится: tzinfo отбрасывается, как в
    active_expired). Порядок сохраняется. Битые элементы — не словарь, id не
    целое, пустой текст, неразборный или нестроковый `until_at` — молча
    пропускаются: лента приходит по сети, цикл из-за мусора падать не должен.
    """
    if not isinstance(items, (list, tuple)):
        return []
    now = now.replace(tzinfo=None)
    live = []
    for item in items:
        if not isinstance(item, dict):
            continue
        tid = item.get("id")
        if not isinstance(tid, int) or isinstance(tid, bool):
            continue
        if not _ticker_text(item.get("message")):
            continue
        raw = item.get("until_at")
        if raw is not None:
            if not isinstance(raw, str):
                continue
            try:
                until = datetime.fromisoformat(raw).replace(tzinfo=None)
            except ValueError:
                continue
            if now >= until:
                continue
        live.append(item)
    return live


def next_ticker_index(live: list, last_id, last_pos: int):
    """Номер строки в `live`, чей проход следующий; None — очередь пуста.

    Идём по кругу после только что прошедшей строки `last_id` (стояла на месте
    `last_pos`). Не было ни одной (`last_id` None) — первая. Прошедшую строку
    сняли или у неё вышел срок — следом идёт та, что встала на её место: снятие
    не сбрасывает круг к началу и не пропускает преемницу.
    """
    if not live:
        return None
    if last_id is None:
        return 0
    for i, item in enumerate(live):
        if item.get("id") == last_id:
            return (i + 1) % len(live)
    return last_pos % len(live)


def _ticker_look(item) -> tuple:
    """Всё, что меняет вид строки на экране: правка любого поля = новый проход."""
    return (_ticker_text(item.get("message")), str(item.get("color")), str(item.get("speed")))


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
        # Очередь бегущих строк из ленты (агент 1.4.0). tickers_from_feed
        # включается, когда лента впервые принесла ключ `tickers`, и больше не
        # сбрасывается: с этого момента ticker_show / ticker_hide означают
        # «обнови ленту», а не «покажи / убери этот текст».
        self.tickers_from_feed = False
        self._tk_lock = threading.Lock()
        self._tk_items = []             # последний список из ленты (как пришёл)
        self._tk_paused = False         # «Стоп» или монитор погашен: строку не рисуем
        self._tk_stopped = False
        self._tk_screen_off = False
        self._ticker_clock = _msk_now   # часы агента (наивное МСК); в тестах подменяются
        self._ticker_mono = time.monotonic   # монотонные часы: давность последнего кадра; в тестах подменяются
        self._tk_drawn_at = None        # monotonic последней УДАЧНОЙ отправки кадра в mpv (None — ещё не было)
        self._tk_thread = None
        self._tk_sock = None            # постоянное соединение с mpv для кадров
        self._tk_cur = None             # проход, который идёт сейчас (только поток очереди)
        self._tk_last = (None, 0)       # (id, место) последней прошедшей строки
        self._tk_shown = False          # нарисован ли оверлей строки

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

    MARQUEE_FONT_SIZE = 42

    @classmethod
    def _marquee_span(cls, safe):
        """Полный путь одного прохода (пиксели): от правого края кадра до
        полного ухода строки за левый. `safe` — уже обезвреженный текст."""
        text_w = int(len(safe) * cls.MARQUEE_FONT_SIZE * 0.6) + 80    # грубая ширина строки
        return cls.MARQUEE_RES_X + text_w

    def _marquee_overlay_cmd(self, x, ass_color, safe):
        """Команда mpv: кадр строки в позиции x. Один вид для старой строки
        (set_marquee) и очереди из ленты: ASS-оверлей у нижнего края (an1 =
        низ-лево), шрифт 42 на 1280×720, чёрная обводка."""
        y = self.MARQUEE_RES_Y - 12
        tags = ("{\\an1\\pos(%.1f,%d)\\fs%d\\c%s\\bord2\\3c&H000000&\\shad1}"
                % (x, y, self.MARQUEE_FONT_SIZE, ass_color))
        return ["osd-overlay", self.MARQUEE_OVERLAY_ID, "ass-events", tags + safe,
                self.MARQUEE_RES_X, self.MARQUEE_RES_Y]

    def _marquee_loop(self, text, ass_color, speed, duration):
        RES_X = self.MARQUEE_RES_X
        pps = self._speed_to_pps(speed)             # пикселей в секунду
        interval = 0.03               # ~33 кадра/сек
        safe = self._ass_escape(text)
        span = self._marquee_span(safe)              # полный путь одного прохода
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
                self._marquee_send(sock, self._marquee_overlay_cmd(x, ass_color, safe))
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

    # ── Очередь бегущих строк из ленты (агент 1.4.0) ────────────────────────
    # Лента экрана несёт список строк (`tickers`), агент крутит живые по очереди:
    # по одному полному проходу каждой её цветом и скоростью, в порядке запуска,
    # и так по кругу. Вид — тот же ASS-оверлей, что у set_marquee. Срок агент
    # проверяет сам по своим часам (live_tickers), поэтому строка выпадает и без
    # связи с сервером; на «Стопе» и при погашенном мониторе оверлей убран.
    #
    # Поток только вызывает _ticker_step() в цикле. Шаг получает время аргументами
    # и рисует через _tk_send, поэтому очередь проверяется тестами без mpv.

    def apply_feed_tickers(self, schedule) -> bool:
        """Принять строки из загруженной ленты. True — ключ `tickers` был в ленте.

        Есть ключ со списком (даже пустым) — он заменяет очередь и включает режим
        «из ленты» насовсем. Нет ключа (сервер не прочитал строки или он старый) —
        очередь остаётся как есть: пустой список снял бы живые объявления из-за
        сбоя, а возврат к командам — вернул бы режим, где строка не переживает
        перезагрузку.
        """
        if not isinstance(schedule, dict) or not isinstance(schedule.get("tickers"), list):
            return False
        if not self.tickers_from_feed:
            # строка от прежней команды ticker_show делит оверлей с очередью — снять
            if self._marquee_thread is not None:
                self.clear_marquee()
            self.tickers_from_feed = True
            log.info("Бегущие строки: режим «из ленты»")
        self.set_tickers(schedule["tickers"])
        return True

    def set_tickers(self, items) -> None:
        """Заменить список строк. Тот же список (лента перечитывается каждую
        минуту) ничего не трогает — прокрутка не перезапускается."""
        items = [dict(t) if isinstance(t, dict) else t for t in items] \
            if isinstance(items, (list, tuple)) else []
        with self._tk_lock:
            if items == self._tk_items:
                return
            self._tk_items = items
        log.info("Бегущие строки из ленты: {}".format(
            [t.get("id") for t in items if isinstance(t, dict)]))

    def set_tickers_paused(self, paused: bool, stopped: bool, screen_off: bool) -> None:
        """Пауза показа строк: «Стоп» (stopped) или погашенный монитор (screen_off).
        Вызывается каждый проход главного цикла. Строки остаются в очереди и
        побегут снова сами, как только пауза снимется."""
        with self._tk_lock:
            self._tk_paused = bool(paused)
            self._tk_stopped = bool(stopped)
            self._tk_screen_off = bool(screen_off)

    def ticker_state(self) -> dict:
        """Отчёт для heartbeat: какие строки у экрана бегут сейчас (id живых по
        часам агента), остановлен ли показ, погашен ли монитор. На паузе
        бежать нечему: ids пуст, причина — в флагах.

        «Бежит» — это «нарисовано», а не «живая по часам»: если последний
        удачный кадр ушёл в mpv раньше TICKER_DRAWN_WINDOW секунд назад (mpv
        лежит или перезапускается, поток очереди умер или завис) либо кадров ещё
        не было, ids пуст — панель покажет «не подтвердил», а не ложное «бежит».
        """
        with self._tk_lock:
            items = self._tk_items
            paused, stopped, screen_off = self._tk_paused, self._tk_stopped, self._tk_screen_off
            drawn_at = self._tk_drawn_at
        drawing = drawn_at is not None and self._ticker_mono() - drawn_at <= TICKER_DRAWN_WINDOW
        ids = [] if paused or not drawing else [t["id"] for t in live_tickers(items, self._ticker_clock())]
        return {"ids": ids, "stopped": stopped, "screen_off": screen_off}

    def start_ticker_queue(self) -> None:
        """Запустить поток очереди (один на плеер; повторный вызов ничего не делает).
        Пока лента строк не принесла, поток оверлея не трогает — старые
        ticker_show / set_marquee работают как раньше."""
        if self._tk_thread is not None and self._tk_thread.is_alive():
            return
        self._tk_thread = threading.Thread(
            target=self._ticker_loop, name="ticker-queue", daemon=True)
        self._tk_thread.start()

    def _ticker_loop(self):
        while True:
            try:
                delay = self._ticker_step(self._ticker_mono(), self._ticker_clock())
            except Exception:
                log.exception("Очередь бегущих строк: сбой шага")
                delay = 1.0
            time.sleep(delay)

    def _tk_send(self, command) -> bool:
        """Отправить команду mpv по постоянному соединению потока очереди.
        Сбой (mpv перезапущен) закрывает соединение — следующая попытка
        подключится заново."""
        try:
            if self._tk_sock is None:
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                sock.settimeout(2)
                sock.connect(MPV_SOCKET)
                self._tk_sock = sock
            self._marquee_send(self._tk_sock, command)
            return True
        except Exception:
            try:
                if self._tk_sock:
                    self._tk_sock.close()
            except Exception:
                pass
            self._tk_sock = None
            return False

    def _ticker_hide(self) -> float:
        """Убрать оверлей очереди, если он нарисован. Возвращает паузу до
        следующего шага. Не получилось (mpv недоступен) — пробуем снова."""
        if not self._tk_shown:
            return TICKER_IDLE
        if self._tk_send(["osd-overlay", self.MARQUEE_OVERLAY_ID, "none", ""]):
            self._tk_shown = False
            return TICKER_IDLE
        return TICKER_RETRY

    def _ticker_pass(self, item, pos, mono):
        """Начать проход строки `item` (стоит на месте `pos` среди живых) в момент `mono`."""
        safe = self._ass_escape(_ticker_text(item.get("message")))
        return {"id": item["id"], "pos": pos, "look": _ticker_look(item), "start": mono,
                "ass_color": self._hex_to_ass(str(item.get("color") or "")), "text": safe,
                "pps": self._speed_to_pps(item.get("speed")), "span": self._marquee_span(safe)}

    def _ticker_step(self, mono, wall) -> float:
        """Один шаг очереди: что сейчас на экране, нарисовать кадр. Возвращает
        паузу до следующего шага. mono — монотонное время (прокрутка), wall —
        часы агента, наивное МСК (сроки). Правка и снятие подхватываются уже на
        следующем шаге, то есть за доли секунды."""
        with self._tk_lock:
            items, paused = self._tk_items, self._tk_paused
        if paused:
            self._tk_cur = None
            return self._ticker_hide()
        live = live_tickers(items, wall)
        cur = self._tk_cur
        if cur is not None:
            idx = next((i for i, t in enumerate(live) if t["id"] == cur["id"]), None)
            if idx is None:                                   # снята или вышел срок
                self._tk_last = (cur["id"], cur["pos"])
                cur = None
            elif _ticker_look(live[idx]) != cur["look"]:      # изменена: заново с правого края
                cur = self._ticker_pass(live[idx], idx, mono)
            else:
                cur["pos"] = idx
        if cur is not None and (mono - cur["start"]) * cur["pps"] >= cur["span"]:
            self._tk_last = (cur["id"], cur["pos"])           # проход закончен
            cur = None
        if cur is None:
            nxt = next_ticker_index(live, *self._tk_last)
            if nxt is None:
                self._tk_cur = None
                return self._ticker_hide()
            cur = self._ticker_pass(live[nxt], nxt, mono)
        self._tk_cur = cur
        x = self.MARQUEE_RES_X - (mono - cur["start"]) * cur["pps"]
        if not self._tk_send(self._marquee_overlay_cmd(x, cur["ass_color"], cur["text"])):
            return TICKER_RETRY
        self._tk_shown = True
        with self._tk_lock:
            self._tk_drawn_at = mono                          # кадр дошёл до mpv: на это опирается отчёт «бежит»
        return TICKER_FRAME
