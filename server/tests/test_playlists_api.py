"""API плейлиста (server/routers/playlists.py): разбор списка роликов, проверка
ролика перед эфиром, создание и замена роликов одним запросом.

normalize_items и _check_media — чистые функции. Эндпоинты /playlists/check,
/playlists/full и PUT /playlists/{id}/items зовутся напрямую на ScriptDB —
подменной сессии, которая отвечает по тексту запроса и записывает всё
выполненное: так проверяются порядок записи, позиции 1…N и главное —
«одна транзакция: при ошибке проверки в БД не пишется ничего». Сами SQL-запросы
(включая ANY(:ids)) проходят только в smoke_test.sh на живом стенде.
"""
import pytest
from fastapi import HTTPException

from conftest import db_row
from routers.playlists import (_check_media, add_to_playlist, check_playlist, create_playlist_full,
                               list_playlists, normalize_items, replace_playlist_items)


# --- normalize_items ----------------------------------------------------------

def test_normalize_ok():
    assert normalize_items([{"media_id": 3, "repeat_count": 2}, {"media_id": "4"}]) == \
        [{"media_id": 3, "repeat_count": 2}, {"media_id": 4, "repeat_count": 1}]


@pytest.mark.parametrize("raw", [[], [{"media_id": 3}, {"media_id": 3}], [{"media_id": 3, "repeat_count": 0}],
                                 [{"media_id": 3, "repeat_count": 100}], [{"media_id": "x"}]])
def test_normalize_rejects(raw):
    with pytest.raises(HTTPException) as e:
        normalize_items(raw)
    assert e.value.status_code == 400


@pytest.mark.parametrize("raw", [
    None, "x", {"media_id": 3},                          # не список
    [3], ["3"], [None],                                  # элемент не объект
    [{}], [{"media_id": None}],                          # нет media_id
    [{"media_id": True}], [{"media_id": 3.5}], [{"media_id": 3.0}],
    [{"media_id": "²"}], [{"media_id": "1_0"}], [{"media_id": "٣"}], [{"media_id": ""}],
    [{"media_id": 0}], [{"media_id": -4}], [{"media_id": 2 ** 31}],
    [{"media_id": 3, "repeat_count": True}], [{"media_id": 3, "repeat_count": 2.5}],
    [{"media_id": 3, "repeat_count": "1.5"}], [{"media_id": 3, "repeat_count": "²"}],
    [{"media_id": 3, "repeat_count": -1}], [{"media_id": 3, "repeat_count": []}],
    [{"media_id": 3}, {"media_id": "3"}],                # повтор media_id в другой записи
])
def test_normalize_rejects_junk(raw):
    with pytest.raises(HTTPException) as e:
        normalize_items(raw)
    assert e.value.status_code == 400


def test_normalize_boundaries_order_and_defaults():
    got = normalize_items([{"media_id": 9, "repeat_count": 99}, {"media_id": " 2 ", "repeat_count": "1"},
                           {"media_id": 5, "repeat_count": None}, {"media_id": 1}])
    assert got == [{"media_id": 9, "repeat_count": 99}, {"media_id": 2, "repeat_count": 1},
                   {"media_id": 5, "repeat_count": 1}, {"media_id": 1, "repeat_count": 1}]


def test_normalize_ignores_extra_keys():
    assert normalize_items([{"media_id": 3, "title": "x", "position": 7}]) == [{"media_id": 3, "repeat_count": 1}]


def test_normalize_duplicate_message_asks_for_repeat():
    with pytest.raises(HTTPException) as e:
        normalize_items([{"media_id": 3}, {"media_id": 3}])
    assert "дважды" in e.value.detail and "повтор" in e.value.detail


# --- _check_media: 38-ФЗ, одни и те же тексты для старого и нового эндпоинтов -------

def media(mid, title="Ролик", status="ready", review="approved", dur=10.0, md5=None, filename="f.mp4"):
    return db_row(id=mid, title=title, filename=filename, status=status, review_status=review,
                  duration_seconds=dur, md5_hash=md5 or f"h{mid}")


def test_check_media_approved_passes():
    assert _check_media(media(1)) is None


def test_check_media_document_409():
    with pytest.raises(HTTPException) as e:
        _check_media(media(1, "Договор", status="document"))
    assert e.value.status_code == 409
    assert e.value.detail == "«Договор» — документ, а не ролик: в плейлист добавить нельзя"


@pytest.mark.parametrize("review,ru", [("pending", "ещё на модерации"), ("rejected", "отклонён модератором"),
                                       ("weird", "weird")])
def test_check_media_not_approved_409(review, ru):
    with pytest.raises(HTTPException) as e:
        _check_media(media(1, "Реклама", review=review))
    assert e.value.status_code == 409
    assert e.value.detail == f"Ролик «Реклама» {ru} — в плейлист можно добавлять только одобренные ролики"


def test_check_media_document_wins_over_review_status():
    with pytest.raises(HTTPException) as e:
        _check_media(media(1, "Скан", status="document", review="pending"))
    assert "документ" in e.value.detail


class _OneClipDB:
    """Старый POST /playlists/{id}/items читает один ролик (SELECT ... WHERE id = :id)."""
    def __init__(self, clip):
        self.clip = clip
        self.calls = []

    def execute(self, query, params=None):
        self.calls.append(" ".join(str(query).split()))
        return _Result([self.clip] if self.clip else [])


def test_old_add_endpoint_uses_same_checks_and_texts():
    for clip, status, text_part in [
            (None, 404, "Ролик не найден"),
            (media(1, "Договор", status="document"), 409, "«Договор» — документ, а не ролик"),
            (media(1, "Реклама", review="pending"), 409, "Ролик «Реклама» ещё на модерации"),
            (media(1, "Реклама", review="rejected"), 409, "Ролик «Реклама» отклонён модератором")]:
        db = _OneClipDB(clip)
        with pytest.raises(HTTPException) as e:
            add_to_playlist(5, 1, 1, {}, db)
        assert e.value.status_code == status and text_part in e.value.detail
        assert len(db.calls) == 1                    # дальше проверки дубля и записи не идут


# --- ScriptDB: сессия, отвечающая по тексту запроса ----------------------------

class _Result:
    def __init__(self, rows):
        self._rows = list(rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows


class ScriptDB:
    def __init__(self, clips=(), playlist_exists=True, new_id=7, fail_on=None):
        self.clips = {c.id: c for c in clips}
        self.playlist_exists = playlist_exists
        self.new_id = new_id
        self.fail_on = fail_on
        self.calls = []            # (sql в одну строку, params)
        self.commits = 0
        self.rollbacks = 0

    def execute(self, query, params=None):
        sql = " ".join(str(query).split())
        self.calls.append((sql, params))
        if self.fail_on and self.fail_on in sql:
            raise RuntimeError("сбой БД")
        if "FROM media" in sql:
            return _Result([self.clips[i] for i in params["ids"] if i in self.clips])
        if sql.startswith("INSERT INTO playlists"):
            return _Result([(self.new_id,)])
        if "FROM playlists" in sql:
            return _Result([(params["id"],)] if self.playlist_exists else [])
        return _Result([])

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    # --- что записано ---
    def writes(self):
        return [(s, p) for s, p in self.calls if s.split()[0] in ("INSERT", "UPDATE", "DELETE")]

    def inserted_items(self):
        return [(p["mid"], p["pos"], p["rc"]) for s, p in self.calls if s.startswith("INSERT INTO playlist_items")]


CLIPS = [media(1, "Акция", dur=10), media(2, "Скидки", dur=5), media(3, "Хит", dur=20, filename="hit.mp4")]


def items(*pairs):
    return [{"media_id": m, "repeat_count": r} for m, r in pairs]


def status_of(fn, *args, **kwargs):
    with pytest.raises(HTTPException) as e:
        fn(*args, **kwargs)
    return e.value.status_code


# --- POST /playlists/check -----------------------------------------------------

def test_check_empty_is_allowed_and_touches_no_db():
    db = ScriptDB()
    assert check_playlist({"items": []}, {}, db) == {"warnings": [], "total_seconds": 0.0, "plays": 0}
    assert db.calls == []


def test_check_totals_and_warnings():
    db = ScriptDB(CLIPS)
    r = check_playlist({"items": items((1, 4), (2, 1))}, {}, db)
    # 10 с × 4 + 5 с = 45 с, 5 показов; «Акция» ×4 подряд и 40 из 45 с — две мягкие подсказки
    assert r["total_seconds"] == 45.0 and r["plays"] == 5
    assert any("«Акция» ×4 подряд" in w for w in r["warnings"])
    assert any("«Акция» занимает 40 из 45 с" in w for w in r["warnings"])
    assert db.writes() == [] and db.commits == 0


def test_check_single_clip_no_warnings():
    r = check_playlist({"items": items((3, 2))}, {}, ScriptDB(CLIPS))
    assert r == {"warnings": [], "total_seconds": 40.0, "plays": 2}


def test_check_same_file_under_two_clips_warns():
    clips = [media(1, "Акция", md5="same"), media(2, "Акция копия", md5="same")]
    r = check_playlist({"items": items((1, 1), (2, 1))}, {}, ScriptDB(clips))
    assert "«Акция» и «Акция копия» — это один и тот же файл" in r["warnings"]


def test_check_uses_filename_when_title_is_empty_and_zero_for_missing_duration():
    clips = [media(1, title="", filename="a.mp4", dur=None), media(2, title="Б", dur=10)]
    r = check_playlist({"items": items((1, 5), (2, 1))}, {}, ScriptDB(clips))
    assert r["total_seconds"] == 10.0 and r["plays"] == 6
    assert "«a.mp4» ×5 подряд — больше 3 повторов" in r["warnings"]


def test_check_does_not_block_unapproved_clip():
    # это подсказка в окне сборки: 409 «не одобрен» приходит при сохранении, не здесь
    r = check_playlist({"items": items((1, 1))}, {}, ScriptDB([media(1, review="pending")]))
    assert r["plays"] == 1


def test_check_unknown_clip_404_and_junk_400():
    assert status_of(check_playlist, {"items": items((1, 1), (99, 1))}, {}, ScriptDB(CLIPS)) == 404
    assert status_of(check_playlist, {"items": items((1, 0))}, {}, ScriptDB(CLIPS)) == 400
    assert status_of(check_playlist, {}, {}, ScriptDB(CLIPS)) == 400
    assert status_of(check_playlist, [], {}, ScriptDB(CLIPS)) == 400


# --- POST /playlists/full --------------------------------------------------------

def test_full_creates_playlist_with_items_in_one_transaction():
    db = ScriptDB(CLIPS, new_id=42)
    r = create_playlist_full({"name": "  Утро  ", "items": items((3, 1), (1, 4), (2, 2))}, {}, db)
    assert r["id"] == 42 and r["name"] == "Утро"
    assert any("«Акция» ×4 подряд" in w for w in r["warnings"])
    assert db.inserted_items() == [(3, 1, 1), (1, 2, 4), (2, 3, 2)]     # position 1…N, порядок как в запросе
    assert db.commits == 1 and db.rollbacks == 0
    assert db.calls[-1][0].startswith("INSERT INTO playlist_items")     # коммит после последней записи


@pytest.mark.parametrize("body", [{"items": items((1, 1))}, {"name": "", "items": items((1, 1))},
                                  {"name": "   ", "items": items((1, 1))}, {"name": None, "items": items((1, 1))},
                                  {"name": 5, "items": items((1, 1))}, {"name": "я" * 129, "items": items((1, 1))},
                                  {"name": "Х", "items": []}, {"name": "Х"}, {"name": "Х", "items": "1,2"},
                                  {"name": "Х", "items": items((1, 100))}, {"name": "Х", "items": items((1, 1), (1, 2))}])
def test_full_rejects_bad_body_and_writes_nothing(body):
    db = ScriptDB(CLIPS)
    assert status_of(create_playlist_full, body, {}, db) == 400
    assert db.writes() == [] and db.commits == 0


def test_full_name_of_128_chars_is_ok():
    db = ScriptDB(CLIPS)
    assert create_playlist_full({"name": "я" * 128, "items": items((1, 1))}, {}, db)["id"] == 7


def test_full_unknown_clip_404_writes_nothing():
    db = ScriptDB(CLIPS)
    assert status_of(create_playlist_full, {"name": "Х", "items": items((1, 1), (99, 1))}, {}, db) == 404
    assert db.writes() == [] and db.commits == 0


def test_full_document_409_writes_nothing():
    db = ScriptDB(CLIPS + [media(5, "Договор", status="document")])
    with pytest.raises(HTTPException) as e:
        create_playlist_full({"name": "Х", "items": items((1, 1), (5, 1))}, {}, db)
    assert e.value.status_code == 409
    assert e.value.detail == "«Договор» — документ, а не ролик: в плейлист добавить нельзя"
    assert db.writes() == [] and db.commits == 0      # ролик 1 тоже не записан — плейлиста нет вовсе


def test_full_unapproved_409_writes_nothing():
    db = ScriptDB(CLIPS + [media(5, "Реклама", review="pending")])
    with pytest.raises(HTTPException) as e:
        create_playlist_full({"name": "Х", "items": items((5, 1), (1, 1))}, {}, db)
    assert e.value.status_code == 409
    assert e.value.detail == "Ролик «Реклама» ещё на модерации — в плейлист можно добавлять только одобренные ролики"
    assert db.writes() == [] and db.commits == 0


def test_full_db_failure_rolls_back_and_reraises():
    db = ScriptDB(CLIPS, fail_on="INSERT INTO playlist_items")
    with pytest.raises(RuntimeError):
        create_playlist_full({"name": "Х", "items": items((1, 1), (2, 1))}, {}, db)
    assert db.commits == 0 and db.rollbacks == 1


# --- PUT /playlists/{id}/items --------------------------------------------------

def test_replace_deletes_old_items_and_inserts_new_in_one_transaction():
    db = ScriptDB(CLIPS)
    r = replace_playlist_items(5, {"items": items((2, 3), (1, 1), (3, 1))}, {}, db)   # 15 + 10 + 20 с: без подсказок
    assert r == {"status": "ok", "warnings": []}
    kinds = [s.split(" WHERE")[0].split(" (")[0] for s, _ in db.writes()]
    assert kinds == ["DELETE FROM playlist_items"] + ["INSERT INTO playlist_items"] * 3
    assert db.writes()[0][1] == {"pid": 5}
    assert db.inserted_items() == [(2, 1, 3), (1, 2, 1), (3, 3, 1)]
    assert all(p["pid"] == 5 for _, p in db.writes())
    assert db.commits == 1 and db.rollbacks == 0


def test_replace_without_name_does_not_rename():
    db = ScriptDB(CLIPS)
    replace_playlist_items(5, {"items": items((1, 1))}, {}, db)
    assert not any(s.startswith("UPDATE playlists") for s, _ in db.writes())
    db2 = ScriptDB(CLIPS)
    replace_playlist_items(5, {"name": None, "items": items((1, 1))}, {}, db2)
    assert not any(s.startswith("UPDATE playlists") for s, _ in db2.writes())


def test_replace_with_name_renames_before_replacing_items():
    db = ScriptDB(CLIPS)
    replace_playlist_items(5, {"name": " Вечер ", "items": items((1, 1))}, {}, db)
    first = db.writes()[0]
    assert first[0].startswith("UPDATE playlists SET name") and first[1] == {"n": "Вечер", "id": 5}
    assert db.commits == 1


def test_replace_returns_warnings():
    r = replace_playlist_items(5, {"items": items((1, 4), (2, 1))}, {}, ScriptDB(CLIPS))
    assert any("×4 подряд" in w for w in r["warnings"])


def test_replace_unknown_playlist_404_writes_nothing():
    db = ScriptDB(CLIPS, playlist_exists=False)
    assert status_of(replace_playlist_items, 5, {"items": items((1, 1))}, {}, db) == 404
    assert db.writes() == [] and db.commits == 0


@pytest.mark.parametrize("body", [{"items": []}, {}, {"items": items((1, 1), (1, 1))}, {"items": items((1, 0))},
                                  {"name": "", "items": items((1, 1))}, {"name": "  ", "items": items((1, 1))},
                                  {"name": 7, "items": items((1, 1))}, {"name": "я" * 129, "items": items((1, 1))}])
def test_replace_rejects_bad_body_and_keeps_old_items(body):
    db = ScriptDB(CLIPS)
    assert status_of(replace_playlist_items, 5, body, {}, db) == 400
    assert db.writes() == [] and db.commits == 0


def test_replace_unknown_clip_404_keeps_old_items():
    db = ScriptDB(CLIPS)
    assert status_of(replace_playlist_items, 5, {"items": items((1, 1), (99, 1))}, {}, db) == 404
    assert db.writes() == [] and db.commits == 0


def test_replace_unapproved_409_keeps_old_items_and_name():
    db = ScriptDB(CLIPS + [media(5, "Реклама", review="rejected")])
    with pytest.raises(HTTPException) as e:
        replace_playlist_items(5, {"name": "Новое", "items": items((1, 1), (5, 1))}, {}, db)
    assert e.value.status_code == 409 and "отклонён модератором" in e.value.detail
    assert db.writes() == [] and db.commits == 0


def test_replace_db_failure_rolls_back_and_reraises():
    db = ScriptDB(CLIPS, fail_on="INSERT INTO playlist_items")
    with pytest.raises(RuntimeError):
        replace_playlist_items(5, {"name": "Х", "items": items((1, 1))}, {}, db)
    assert db.commits == 0 and db.rollbacks == 1


# --- GET /playlists: флаг playable ---------------------------------------------

class _ListDB:
    """Сессия для GET /playlists: список плейлистов и id плейлистов с проигрываемыми роликами."""

    def __init__(self, playlists, playable_ids):
        self.playlists, self.playable_ids, self.calls = playlists, playable_ids, []

    def execute(self, query, params=None):
        sql = " ".join(str(query).split())
        self.calls.append((sql, params))
        if "FROM playlist_items" in sql:
            return _Result([db_row(playlist_id=i) for i in self.playable_ids])
        return _Result([db_row(_mapping={"id": i, "name": n}, id=i) for i, n in self.playlists])


def test_list_playlists_marks_playlists_with_nothing_to_play():
    db = _ListDB([(1, "Акции"), (2, "Пустой"), (3, "Всё на модерации")], playable_ids=[1])
    assert list_playlists({}, db) == [
        {"id": 1, "name": "Акции", "playable": True},
        {"id": 2, "name": "Пустой", "playable": False},
        {"id": 3, "name": "Всё на модерации", "playable": False}]


def test_list_playlists_asks_playable_once_for_the_most_permissive_venue():
    """Одним запросом, для площадки с алколицензией: реклама алкоголя тоже «может идти»."""
    db = _ListDB([(1, "А"), (2, "Б")], playable_ids=[1, 2])
    list_playlists({}, db)
    playable_calls = [p for s, p in db.calls if "FROM playlist_items" in s]
    assert playable_calls == [{"venue": "store_alcohol"}]
