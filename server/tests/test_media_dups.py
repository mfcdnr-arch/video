"""Дубли в медиатеке (server/routers/media.py): группировка роликов с одним
md5, текст ответа 409 и проверка в POST /media/upload.

group_duplicates и _duplicate_detail — чистые функции. Загрузка зовётся
напрямую на подменной сессии БД и временной папке: так проверяется главное —
«отклонённая загрузка не оставляет ничего» (ни файла на диске, ни миниатюры,
ни записи в БД) и «документы и allow_duplicate проверку дубля не проходят».
Сами SQL-запросы (LEFT JOIN, подсчёт плейлистов) проверяются только в
smoke_test.sh на живом стенде.
"""
import asyncio
import json
import os
from datetime import datetime
from types import SimpleNamespace as R

import pytest
from fastapi import HTTPException

import media_check
from routers import media as media_mod
from routers.media import group_duplicates


# --- group_duplicates -----------------------------------------------------------

def test_group_duplicates():
    rows = [R(id=1, title="A", md5_hash="x"), R(id=2, title="A (2)", md5_hash="x"),
            R(id=3, title="B", md5_hash="y"), R(id=4, title="C", md5_hash=None)]
    assert group_duplicates(rows) == {1: [{"id": 2, "title": "A (2)"}], 2: [{"id": 1, "title": "A"}]}


def test_group_duplicates_empty_input():
    assert group_duplicates([]) == {}


def test_group_duplicates_all_unique_gives_nothing():
    rows = [R(id=1, title="A", md5_hash="x"), R(id=2, title="A", md5_hash="y")]
    assert group_duplicates(rows) == {}


def test_group_duplicates_three_in_group_each_sees_the_other_two():
    rows = [R(id=5, title="a", md5_hash="h"), R(id=6, title="b", md5_hash="h"), R(id=7, title="c", md5_hash="h")]
    assert group_duplicates(rows) == {
        5: [{"id": 6, "title": "b"}, {"id": 7, "title": "c"}],
        6: [{"id": 5, "title": "a"}, {"id": 7, "title": "c"}],
        7: [{"id": 5, "title": "a"}, {"id": 6, "title": "b"}]}


def test_group_duplicates_two_separate_groups_do_not_mix():
    rows = [R(id=1, title="a", md5_hash="x"), R(id=2, title="b", md5_hash="y"),
            R(id=3, title="c", md5_hash="x"), R(id=4, title="d", md5_hash="y"), R(id=5, title="e", md5_hash="z")]
    assert group_duplicates(rows) == {1: [{"id": 3, "title": "c"}], 3: [{"id": 1, "title": "a"}],
                                      2: [{"id": 4, "title": "d"}], 4: [{"id": 2, "title": "b"}]}


@pytest.mark.parametrize("empty", [None, "", "   "])
def test_group_duplicates_empty_hash_is_never_a_duplicate(empty):
    rows = [R(id=1, title="a", md5_hash=empty), R(id=2, title="b", md5_hash=empty),
            R(id=3, title="c", md5_hash="x")]
    assert group_duplicates(rows) == {}


def test_group_duplicates_same_title_different_hash_is_not_duplicate():
    rows = [R(id=1, title="Ролик", md5_hash="x"), R(id=2, title="Ролик", md5_hash="y")]
    assert group_duplicates(rows) == {}


def test_group_duplicates_result_is_json_ready():
    # ключи — id роликов (int); в JSON они станут строками "<id>"
    out = json.loads(json.dumps(group_duplicates([R(id=1, title="A", md5_hash="x"),
                                                  R(id=2, title="B", md5_hash="x")])))
    assert out == {"1": [{"id": 2, "title": "B"}], "2": [{"id": 1, "title": "A"}]}


# --- _duplicate_detail: тело ответа 409 ----------------------------------------

def dup_row(**over):
    base = dict(id=7, title="Старый ролик", filename="1_ab_old.mp4", folder="Акции", advertiser="Кофейня",
                created_at=datetime(2026, 10, 1, 12, 30, 5), duration_seconds=12.5, playlists=2)
    base.update(over)
    return R(**base)


def test_duplicate_detail_has_message_and_all_fields():
    d = media_mod._duplicate_detail(dup_row())
    assert d == {"message": "Такой ролик уже есть в медиатеке",
                 "duplicate_of": {"id": 7, "title": "Старый ролик", "filename": "1_ab_old.mp4",
                                  "folder": "Акции", "advertiser": "Кофейня",
                                  "created_at": "2026-10-01T12:30:05", "duration_seconds": 12.5, "playlists": 2}}


def test_duplicate_detail_survives_plain_json_dumps():
    # HTTPException(detail=...) уходит в JSONResponse без jsonable_encoder:
    # datetime там упал бы с TypeError — дата должна быть уже строкой
    json.dumps(media_mod._duplicate_detail(dup_row()))


def test_duplicate_detail_keeps_missing_values():
    d = media_mod._duplicate_detail(dup_row(folder=None, advertiser=None, created_at=None,
                                            duration_seconds=None, playlists=None))["duplicate_of"]
    assert d["folder"] is None and d["advertiser"] is None and d["created_at"] is None
    assert d["duration_seconds"] is None
    assert d["playlists"] == 0                       # «нет плейлистов» — число, а не null


# --- POST /media/upload: подменная сессия и временная папка ---------------------

DUP = dup_row()


class _Result:
    def __init__(self, rows=(), scalar=None):
        self._rows, self._scalar = list(rows), scalar

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows

    def scalar(self):
        return self._scalar


class ScriptSession:
    """Вместо Session(engine): отвечает по тексту запроса, пишет всё выполненное."""
    log = []                      # (sql, params) всех запросов
    existing = [DUP]              # что «лежит в медиатеке» с таким md5
    folder_name = None            # ответ на SELECT name FROM media_folders

    def __init__(self, bind=None):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def commit(self):
        ScriptSession.log.append(("COMMIT", None))

    def execute(self, query, params=None):
        sql = " ".join(str(query).split())
        ScriptSession.log.append((sql, params))
        if "md5_hash = :" in sql:
            return _Result(ScriptSession.existing[:1])
        if "FROM media_folders WHERE id" in sql:
            return _Result(scalar=ScriptSession.folder_name)
        if sql.startswith("INSERT INTO media"):
            return _Result([(99,)])
        return _Result()


class FakeUpload:
    def __init__(self, filename="clip.mp4", data=b"same bytes"):
        self.filename, self._data, self._pos = filename, data, 0

    async def read(self, n=-1):
        chunk = self._data[self._pos:self._pos + (n if n > 0 else len(self._data))]
        self._pos += len(chunk)
        return chunk


@pytest.fixture
def stand(tmp_path, monkeypatch):
    """Подменяет сессию БД и каталоги медиа/миниатюр; ffprobe не вызывается."""
    media_dir, thumbs = tmp_path / "media", tmp_path / "thumbs"
    media_dir.mkdir()
    thumbs.mkdir()
    ScriptSession.log, ScriptSession.existing, ScriptSession.folder_name = [], [DUP], None
    monkeypatch.setattr(media_mod, "Session", ScriptSession)
    monkeypatch.setattr(media_mod, "MEDIA_PATH", str(media_dir))
    monkeypatch.setattr(media_mod, "THUMBS_DIR", str(thumbs))
    monkeypatch.setattr(media_check, "check_video", lambda path: {"ok": True, "duration": 3.0})
    monkeypatch.setattr(media_mod, "_make_thumbnail", lambda path, mid: str(thumbs / f"{mid}.jpg"))
    return R(media_dir=media_dir, thumbs=thumbs)


def upload(file=None, **over):
    """Прямой вызов эндпоинта: значения Query(...) по умолчанию подставляем сами."""
    args = dict(file=file or FakeUpload(), title="Новый", valid_from=None, valid_until=None,
                advertiser_id=None, folder_id=None, image_seconds=None, category="service",
                age_rating=None, disclaimer_text=None, license_number=None, is_filler=False,
                allow_duplicate=False, admin={"username": "t"})
    args.update(over)
    return asyncio.run(media_mod.upload_media(**args))


def sql_log():
    return [s for s, _ in ScriptSession.log]


def test_upload_duplicate_is_409_with_details_and_leaves_nothing(stand):
    with pytest.raises(HTTPException) as e:
        upload()
    assert e.value.status_code == 409
    assert e.value.detail == media_mod._duplicate_detail(DUP)
    assert e.value.detail["duplicate_of"]["id"] == 7
    json.dumps(e.value.detail)                                   # готово к отдаче как есть
    assert os.listdir(stand.media_dir) == []                     # записанный файл удалён
    assert os.listdir(stand.thumbs) == []                        # миниатюры нет
    assert not any(s.startswith("INSERT") for s in sql_log())    # в БД ничего не добавлено
    assert "COMMIT" not in sql_log()


def test_upload_duplicate_lookup_is_by_md5_of_the_file_and_skips_documents(stand):
    with pytest.raises(HTTPException):
        upload(file=FakeUpload(data=b"abc"))
    lookups = [(s, p) for s, p in ScriptSession.log if "md5_hash = :" in s]
    assert len(lookups) == 1
    sql, params = lookups[0]
    assert "<> 'document'" in sql
    assert list(params.values()) == ["900150983cd24fb0d6963f7d28e17f72"]    # md5("abc")


def test_upload_filler_duplicate_is_409_too(stand):
    with pytest.raises(HTTPException) as e:
        upload(is_filler=True, category="other")
    assert e.value.status_code == 409
    assert os.listdir(stand.media_dir) == []
    assert not any(s.startswith("INSERT") for s in sql_log())


def test_upload_new_file_passes_the_check_and_is_saved(stand):
    ScriptSession.existing = []
    out = upload()
    assert out["id"] == 99 and out["status"] == "ready"
    assert len(os.listdir(stand.media_dir)) == 1                 # файл остался
    assert any(s.startswith("INSERT INTO media") for s in sql_log())


def test_upload_allow_duplicate_skips_the_lookup(stand):
    out = upload(allow_duplicate=True)
    assert out["id"] == 99
    assert not any("md5_hash = :" in s for s in sql_log())       # даже не искали
    assert len(os.listdir(stand.media_dir)) == 1


def test_upload_document_is_not_checked_for_duplicates(stand):
    ScriptSession.folder_name = "Документы"
    out = upload(file=FakeUpload("scan.pdf", b"%PDF"), folder_id=5)
    assert out["status"] == "document" and out["id"] == 99
    assert not any("md5_hash = :" in s for s in sql_log())
    assert len(os.listdir(stand.media_dir)) == 1


def test_upload_bad_file_still_gets_400_after_the_duplicate_check_passes(stand, monkeypatch):
    ScriptSession.existing = []
    monkeypatch.setattr(media_check, "check_video", lambda path: {"ok": False, "error": "битый"})
    with pytest.raises(HTTPException) as e:
        upload()
    assert e.value.status_code == 400
    assert os.listdir(stand.media_dir) == []
