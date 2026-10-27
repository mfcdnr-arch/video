"""Защита от стирания медиа: сбой запроса списка файлов ≠ пустой список.

Раньше Downloader.get_file_list на любой ошибке отдавал [], а Cleanup.run по
такому «пустому» списку удалял ВСЕ ролики мини-ПК (очистка идёт при старте
агента и раз в сутки, так что один неудачный запрос после OTA-перезапуска
стирал диск). Теперь сбой — исключение FileListError, очистка и синхронизация
на нём пропускаются, а успешный пустой список остаётся законным «ничего не
нужно».

Без сети и без ds_agent (тот читает конфиг при импорте): Downloader собирается
минуя __init__, вместо requests-сессии — заглушка, диск — временная папка.
"""
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# ds_downloader импортирует requests; в тестовом образе его может не быть —
# подкладываем пустой модуль (Session здесь не создаётся, см. make_downloader).
try:
    import requests  # noqa: F401
except ImportError:
    sys.modules["requests"] = types.ModuleType("requests")

from ds_cleanup import Cleanup                         # noqa: E402
from ds_downloader import Downloader, FileListError, md5_file   # noqa: E402


class Resp:
    def __init__(self, status=200, body=None, bad_json=False):
        self.status_code = status
        self._body = body
        self._bad_json = bad_json

    def json(self):
        if self._bad_json:
            raise ValueError("не JSON")
        return self._body


class Session:
    """requests.Session-заглушка: отдаёт заданный ответ или бросает исключение."""
    def __init__(self, resp=None, exc=None):
        self.resp, self.exc, self.calls = resp, exc, []

    def get(self, url, **kw):
        self.calls.append(url)
        if self.exc is not None:
            raise self.exc
        return self.resp


def make_downloader(tmp_path, session):
    d = Downloader.__new__(Downloader)       # без __init__: он создаёт настоящую requests.Session
    d.server = "http://srv"
    d.token = "t"
    d.media_dir = tmp_path
    d.session = session
    d.downloaded = []                        # что просили скачать

    def fake_download(media_id, filename, md5, size):
        d.downloaded.append(filename)
        return True
    d.download_file = fake_download
    return d


def fill_media(tmp_path):
    """Диск мини-ПК: два нужных ролика, один лишний, скрытый кеш расписания и подпапка."""
    for name in ("keep1.mp4", "keep2.mp4", "stale.mp4", ".schedule_cache.json"):
        (tmp_path / name).write_bytes(b"x")
    (tmp_path / "subdir").mkdir()
    (tmp_path / "subdir" / "inner.mp4").write_bytes(b"x")


def names(tmp_path):
    return sorted(p.name for p in tmp_path.iterdir())


ALL_FILES = [".schedule_cache.json", "keep1.mp4", "keep2.mp4", "stale.mp4", "subdir"]
LISTED = [{"media_id": 1, "filename": "keep1.mp4", "md5_hash": "a", "filesize": 1},
          {"media_id": 2, "filename": "keep2.mp4", "md5_hash": "b", "filesize": 1}]


# ── какие ответы считаются сбоем ─────────────────────────────────────────────

@pytest.mark.parametrize("session", [
    Session(exc=ConnectionError("сети нет")),
    Session(exc=TimeoutError("таймаут")),
    Session(resp=Resp(500, body={"detail": "boom"})),
    Session(resp=Resp(404, body={"detail": "нет экрана"})),
    Session(resp=Resp(401, body={"detail": "токен"})),
    Session(resp=Resp(204, body=None)),                    # 2xx, но не 200
    Session(resp=Resp(200, bad_json=True)),                # 200, но тело не JSON
    Session(resp=Resp(200, body={"files": []})),           # не список
    Session(resp=Resp(200, body=None)),                    # null
    Session(resp=Resp(200, body="[]")),                    # строка, а не список
    Session(resp=Resp(200, body=[{"media_id": 1}])),       # запись без filename
    Session(resp=Resp(200, body=["keep1.mp4"])),           # запись — не словарь
    Session(resp=Resp(200, body=[{"filename": ""}])),      # пустое имя
    Session(resp=Resp(200, body=[{"filename": None}])),
])
def test_get_file_list_failure_raises(tmp_path, session):
    d = make_downloader(tmp_path, session)
    with pytest.raises(FileListError):
        d.get_file_list(7)


def test_get_file_list_empty_successful_list_is_legitimate(tmp_path):
    d = make_downloader(tmp_path, Session(resp=Resp(200, body=[])))
    assert d.get_file_list(7) == []


def test_get_file_list_returns_records(tmp_path):
    d = make_downloader(tmp_path, Session(resp=Resp(200, body=LISTED)))
    assert d.get_file_list(7) == LISTED
    assert d.session.calls == ["http://srv/api/files/list/7"]


# ── очистка ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("session", [
    Session(exc=ConnectionError("сети нет")),
    Session(resp=Resp(500, body={"detail": "boom"})),
    Session(resp=Resp(200, bad_json=True)),
    Session(resp=Resp(200, body={"files": LISTED})),       # не список
])
def test_cleanup_skipped_when_list_not_fetched(tmp_path, session):
    fill_media(tmp_path)
    c = Cleanup(make_downloader(tmp_path, session), 7, str(tmp_path))
    assert c.run() is False
    assert names(tmp_path) == ALL_FILES                    # ничего не удалено
    assert (tmp_path / "subdir" / "inner.mp4").exists()


def test_cleanup_deletes_only_unlisted_visible_files(tmp_path):
    fill_media(tmp_path)
    c = Cleanup(make_downloader(tmp_path, Session(resp=Resp(200, body=LISTED))), 7, str(tmp_path))
    assert c.run() is True
    # нужные остались, лишний удалён, скрытый кеш расписания и подпапка не тронуты
    assert names(tmp_path) == [".schedule_cache.json", "keep1.mp4", "keep2.mp4", "subdir"]
    assert (tmp_path / "subdir" / "inner.mp4").exists()


def test_cleanup_with_successful_empty_list_removes_unneeded_but_keeps_hidden(tmp_path):
    fill_media(tmp_path)
    c = Cleanup(make_downloader(tmp_path, Session(resp=Resp(200, body=[]))), 7, str(tmp_path))
    assert c.run() is True                                 # пустой, но успешный список — «ничего не нужно»
    assert names(tmp_path) == [".schedule_cache.json", "subdir"]


def test_cleanup_survives_unexpected_downloader_error(tmp_path):
    """Любое исключение из get_file_list (не только FileListError) — тоже без удаления."""
    fill_media(tmp_path)
    d = make_downloader(tmp_path, Session())
    d.get_file_list = lambda sid: (_ for _ in ()).throw(RuntimeError("странный сбой"))
    assert Cleanup(d, 7, str(tmp_path)).run() is False
    assert names(tmp_path) == ALL_FILES


def test_downloader_cleanup_unused_same_guard(tmp_path):
    fill_media(tmp_path)
    d = make_downloader(tmp_path, Session(exc=ConnectionError("сети нет")))
    d.cleanup_unused(7)
    assert names(tmp_path) == ALL_FILES
    d2 = make_downloader(tmp_path, Session(resp=Resp(200, body=LISTED)))
    d2.cleanup_unused(7)
    assert names(tmp_path) == [".schedule_cache.json", "keep1.mp4", "keep2.mp4", "subdir"]


# ── синхронизация ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("session", [
    Session(exc=ConnectionError("сети нет")),
    Session(resp=Resp(503, body={"detail": "недоступно"})),
    Session(resp=Resp(200, body={"oops": 1})),
])
def test_sync_files_on_error_downloads_nothing_and_does_not_crash(tmp_path, session):
    d = make_downloader(tmp_path, session)
    assert d.sync_files(7) == (0, 0)
    assert d.downloaded == []


def test_sync_files_downloads_only_missing_or_changed(tmp_path):
    (tmp_path / "keep1.mp4").write_bytes(b"hello")
    md5 = md5_file(str(tmp_path / "keep1.mp4"))
    listed = [{"media_id": 1, "filename": "keep1.mp4", "md5_hash": md5, "filesize": 5},   # уже есть
              {"media_id": 2, "filename": "new.mp4", "md5_hash": "zzz", "filesize": 9}]   # нужно скачать
    d = make_downloader(tmp_path, Session(resp=Resp(200, body=listed)))
    assert d.sync_files(7) == (1, 0)
    assert d.downloaded == ["new.mp4"]


def test_sync_files_empty_successful_list(tmp_path):
    d = make_downloader(tmp_path, Session(resp=Resp(200, body=[])))
    assert d.sync_files(7) == (0, 0)
    assert d.downloaded == []

