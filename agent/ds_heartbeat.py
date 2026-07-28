"""
ds_heartbeat.py — отправка heartbeat на сервер
"""
import os, shutil, logging, requests, time, subprocess, re

log = logging.getLogger(__name__)

# Единственное место, где живёт версия агента: её шлёт heartbeat и отчёт
# о перезапуске (ds_agent.py импортирует отсюда). Панель сравнивает её с
# целевой версией и по ней же решают, доступна ли функция при приёмке.
AGENT_VERSION = "1.2.0"

# Каталог, где ядро публикует состояние видеовыходов (DRM-коннекторы).
DRM_PATH = "/sys/class/drm"
# Служебные коннекторы: не физические выходы, статус у них ни о чём не говорит.
_SKIP_CONNECTORS = ("writeback", "virtual")


def read_display_status():
    """
    Подключён ли монитор к видеовыходу — по /sys/class/drm/<cardN-ВЫХОД>/status.

    Возвращает (connected, outputs):
      connected — True, если хотя бы один физический выход в состоянии
                  `connected`; False, если все известные выходы отключены;
                  None, если статус узнать нельзя (нет /sys/class/drm, все
                  выходы в `unknown` — так ведут себя VGA/DVI на части драйверов).
      outputs   — человекочитаемая сводка «HDMI-A-1:connected, DP-1:disconnected».

    Это детект КАБЕЛЯ/EDID, а не «картинка видна»: монитор, выключенный
    кнопкой, обычно продолжает отвечать по DDC и остаётся `connected`.
    """
    try:
        entries = sorted(os.listdir(DRM_PATH))
    except Exception as e:
        log.debug("Видеовыходы: {} недоступен ({})".format(DRM_PATH, e))
        return None, ""

    parts, any_connected, any_known = [], False, False
    for entry in entries:
        # Коннектор именуется card0-HDMI-A-1 / card0-DP-1; сама cardN и
        # renderD128 не содержат дефиса и отсеиваются.
        if "-" not in entry:
            continue
        name = entry.split("-", 1)[1]
        if any(skip in name.lower() for skip in _SKIP_CONNECTORS):
            continue
        try:
            with open(os.path.join(DRM_PATH, entry, "status"), "r", encoding="utf-8") as f:
                status = f.read().strip().lower()
        except Exception:
            continue
        parts.append("{}:{}".format(name, status))
        if status in ("connected", "disconnected"):
            any_known = True
        if status == "connected":
            any_connected = True

    outputs = ", ".join(parts)[:200]
    if not any_known:
        return None, outputs
    return any_connected, outputs


def _read_os_version() -> str:
    """Версия ОС из /etc/os-release (поле VERSION_ID или VERSION)."""
    try:
        data = {}
        with open("/etc/os-release", "r", encoding="utf-8") as f:
            for line in f:
                if "=" in line:
                    k, v = line.rstrip("\n").split("=", 1)
                    data[k] = v.strip().strip('"')
        # Для Astra Linux полезнее VERSION_ID (например, "1.7"),
        # при отсутствии — PRETTY_NAME.
        return data.get("VERSION_ID") or data.get("VERSION") or data.get("PRETTY_NAME") or "неизвестно"
    except Exception as e:
        log.debug("Не удалось прочитать версию ОС: {}".format(e))
        return "неизвестно"


def _read_vlc_version() -> str:
    """Версия ПЛЕЕРА из вывода `mpv --version` (первое число вида X.Y[.Z]).
    Имя функции/колонки в БД историческое (vlc_version) — плеер давно mpv."""
    for cmd in (["mpv", "--version"], ["/usr/bin/mpv", "--version"]):
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=8)
            text_out = (out.stdout or "") + (out.stderr or "")
            m = re.search(r"\bv?(\d+\.\d+(?:\.\d+)?)\b", text_out)
            if m:
                return m.group(1)
        except Exception:
            continue
    return "неизвестно"


class Heartbeat:
    def __init__(self, server: str, token: str, screen_id: int, media_dir: str):
        self.server = server.rstrip("/")
        self.screen_id = screen_id
        self.media_dir = media_dir
        self.session = requests.Session()
        self.session.headers["X-Token"] = token
        # Версии читаем один раз при старте (они не меняются в процессе работы).
        self.os_version = _read_os_version()
        self.vlc_version = _read_vlc_version()
        # Последнее известное состояние видеовыхода — чтобы писать в лог
        # только смену состояния, а не каждый heartbeat.
        self._last_display = "не читали"
        log.info("Версия ОС: {} · VLC: {}".format(self.os_version, self.vlc_version))

    def _disk_free_gb(self) -> float:
        usage = shutil.disk_usage(self.media_dir)
        return round(usage.free / (1024 ** 3), 2)

    def _disk_total_gb(self) -> float:
        usage = shutil.disk_usage(self.media_dir)
        return round(usage.total / (1024 ** 3), 2)

    def send(self, status: str = "online", playing_file: str = None) -> bool:
        display_connected, display_outputs = read_display_status()
        if display_connected != self._last_display:
            log.info("Видеовыход: {} ({})".format(
                {True: "монитор подключён", False: "МОНИТОР ОТКЛЮЧЁН",
                 None: "статус неизвестен"}[display_connected], display_outputs or "нет данных"))
            self._last_display = display_connected
        payload = {
            "status": status,
            "playing_file": playing_file,
            "disk_free_gb": self._disk_free_gb(),
            "disk_total_gb": self._disk_total_gb(),
            # Подключён ли монитор к видеовыходу (None = определить нельзя).
            "display_connected": display_connected,
            "display_outputs": display_outputs,
            "agent_version": AGENT_VERSION,
            "os_version": self.os_version,
            "vlc_version": self.vlc_version,
            # Время агента (UNIX, UTC) — сервер сравнивает со своим и хранит
            # дрейф часов; уплывшие часы ломают окна питания и синхропоказ.
            "agent_time": time.time(),
        }
        try:
            r = self.session.post(
                f"{self.server}/api/heartbeat/{self.screen_id}",
                json=payload, timeout=10
            )
            r.raise_for_status()
            return True
        except Exception as e:
            log.warning(f"Heartbeat не отправлен: {e}")
            return False
