"""
ds_downloader.py — загрузка медиафайлов с сервера с поддержкой resume и MD5-проверки
"""
from __future__ import annotations  # Python 3.7 совместимость: list[dict], tuple[int,int]
import os, hashlib, logging, time, requests
from pathlib import Path

log = logging.getLogger(__name__)


def md5_file(path: str) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


class Downloader:
    def __init__(self, server: str, token: str, media_dir: str):
        self.server = server.rstrip("/")
        self.token = token
        self.media_dir = Path(media_dir)
        self.media_dir.mkdir(parents=True, exist_ok=True)
        self.session = requests.Session()
        self.session.headers["X-Token"] = token

    def _url(self, path):
        return f"{self.server}{path}"

    def get_file_list(self, screen_id: int) -> list[dict]:
        """Получить список файлов, которые должны быть на этом устройстве."""
        try:
            r = self.session.get(self._url(f"/api/files/list/{screen_id}"), timeout=15)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            log.error(f"Не удалось получить список файлов: {e}")
            return []

    def download_file(self, media_id: int, filename: str, expected_md5: str,
                      expected_size: int) -> bool:
        """Скачать файл с поддержкой resume. Возвращает True если успешно."""
        dest = self.media_dir / filename
        url = self._url(f"/api/files/download/{media_id}")

        # Проверить — вдруг уже скачан
        if dest.exists() and dest.stat().st_size == expected_size:
            actual_md5 = md5_file(str(dest))
            if actual_md5 == expected_md5:
                log.debug(f"{filename} уже в кеше, MD5 совпадает")
                return True
            else:
                log.warning(f"{filename}: MD5 не совпадает, перекачиваем")
                dest.unlink()

        # Поддержка resume
        downloaded = 0
        if dest.exists():
            downloaded = dest.stat().st_size

        headers = {}
        if downloaded > 0:
            headers["Range"] = f"bytes={downloaded}-"
            log.info(f"Возобновляем загрузку {filename} с байта {downloaded}")

        for attempt in range(3):
            try:
                mode = "ab" if downloaded > 0 else "wb"
                with self.session.get(url, headers=headers, stream=True, timeout=60) as r:
                    if r.status_code not in (200, 206):
                        log.error(f"Сервер вернул {r.status_code} для {filename}")
                        return False
                    with open(dest, mode) as f:
                        for chunk in r.iter_content(chunk_size=65536):
                            f.write(chunk)

                # Проверяем MD5
                actual_md5 = md5_file(str(dest))
                if actual_md5 != expected_md5:
                    log.error(f"{filename}: MD5 после загрузки не совпадает! "
                              f"Ожидалось {expected_md5}, получено {actual_md5}")
                    dest.unlink()
                    downloaded = 0
                    headers = {}
                    continue

                log.info(f"Загружен {filename} ({dest.stat().st_size} байт), MD5 ОК")
                return True

            except Exception as e:
                log.warning(f"Попытка {attempt+1}/3 для {filename} провалилась: {e}")
                time.sleep(5 * (attempt + 1))

        return False

    def sync_files(self, screen_id: int) -> tuple[int, int]:
        """
        Синхронизировать файлы: скачать нужные, вернуть (downloaded, failed).
        """
        needed = self.get_file_list(screen_id)
        downloaded = 0
        failed = 0

        for item in needed:
            fname = item["filename"]
            dest = self.media_dir / fname
            if dest.exists():
                if md5_file(str(dest)) == item.get("md5_hash", ""):
                    continue  # Уже есть
            log.info(f"Нужно скачать: {fname}")
            ok = self.download_file(
                item["media_id"], fname,
                item.get("md5_hash", ""), item.get("filesize", 0)
            )
            if ok:
                downloaded += 1
            else:
                failed += 1

        return downloaded, failed

    def cleanup_unused(self, screen_id: int):
        """Удалить файлы, которые больше не нужны для расписания."""
        needed = {item["filename"] for item in self.get_file_list(screen_id)}
        for path in self.media_dir.iterdir():
            # Скрытые файлы (начинающиеся с '.') не трогаем:
            # .schedule_cache.json нужен для офлайн-работы агента.
            if path.is_file() and not path.name.startswith('.') and path.name not in needed:
                log.info(f"Удаляем ненужный файл: {path.name}")
                path.unlink()
