"""
ds_heartbeat.py — отправка heartbeat на сервер
"""
import shutil, logging, requests, time, subprocess, re

log = logging.getLogger(__name__)


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
        log.info("Версия ОС: {} · VLC: {}".format(self.os_version, self.vlc_version))

    def _disk_free_gb(self) -> float:
        usage = shutil.disk_usage(self.media_dir)
        return round(usage.free / (1024 ** 3), 2)

    def _disk_total_gb(self) -> float:
        usage = shutil.disk_usage(self.media_dir)
        return round(usage.total / (1024 ** 3), 2)

    def send(self, status: str = "online", playing_file: str = None) -> bool:
        payload = {
            "status": status,
            "playing_file": playing_file,
            "disk_free_gb": self._disk_free_gb(),
            "disk_total_gb": self._disk_total_gb(),
            "agent_version": "1.1.2",
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
