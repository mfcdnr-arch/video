"""
ds_cleanup.py — очистка устаревших файлов
"""
import logging
from pathlib import Path
from ds_downloader import Downloader

log = logging.getLogger(__name__)


class Cleanup:
    def __init__(self, downloader: Downloader, screen_id: int, media_dir: str):
        self.downloader = downloader
        self.screen_id = screen_id
        self.media_dir = Path(media_dir)

    def run(self):
        """Удалить файлы, которые не нужны согласно серверному списку.

        Если список получить не удалось (нет сети, ответ не 200, не JSON, не
        список) — очистка пропускается, файлы остаются: «не знаем, что нужно»
        не то же самое, что «ничего не нужно». Успешный пустой список — это
        «ничего не нужно». Возвращает True, если очистка выполнена, False —
        если пропущена."""
        try:
            needed = {item["filename"]
                      for item in self.downloader.get_file_list(self.screen_id)}
        except Exception as e:
            log.error(f"Очистка пропущена, файлы не тронуты: не удалось получить список нужных файлов: {e}")
            return False

        removed = 0
        for path in self.media_dir.iterdir():
            # Скрытые файлы (начинающиеся с '.') не трогаем:
            # .schedule_cache.json нужен для офлайн-работы агента.
            if path.is_file() and not path.name.startswith('.') and path.name not in needed:
                log.info(f"Удаляем устаревший файл: {path.name}")
                path.unlink()
                removed += 1

        if removed:
            log.info(f"Очистка завершена: удалено {removed} файлов")
        return True
