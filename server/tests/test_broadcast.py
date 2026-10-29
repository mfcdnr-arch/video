"""Общий эфир сети: срок («до отмены» / «до ЧЧ:00»), правила, запись в БД, истечение.

Три уровня, как у показов:
  broadcast_rules  — чистая логика (разбор часа окончания, «идёт ли эфир сейчас»);
  broadcast_store  — SQL на фейк-БД RoutedDB (строки по подстроке SQL, а не по
                     порядку вызовов), commit внутри не делается;
  routers.broadcast — разбор тела POST /broadcast/on и вид ответа.
SQL против живой схемы гоняет smoke_test.sh на стенде (шаг «Общий эфир»).

Все моменты — наивное МСК: 15:00 МСК — это 12:00 UTC, а контейнер Postgres в UTC.
"""
from datetime import datetime
from types import SimpleNamespace as Row

import pytest
from fastapi import HTTPException

import broadcast_store as store
from broadcast_rules import effective_on, parse_until_hour
from routers.broadcast import broadcast_json, parse_on_request

N = datetime(2026, 10, 28, 14, 30)


# --- правила: час окончания ---------------------------------------------------

def test_until_none_means_until_cancel():
    assert parse_until_hour(None, N) is None


def test_until_hour_today_and_24():
    assert parse_until_hour(16, N) == datetime(2026, 10, 28, 16, 0)
    assert parse_until_hour(24, N) == datetime(2026, 10, 29, 0, 0)


@pytest.mark.parametrize("bad", [14, 13, 0, 25, "x", 15.5])
def test_until_hour_rejected(bad):
    with pytest.raises(ValueError):
        parse_until_hour(bad, N)


@pytest.mark.parametrize("bad", [True, False, "16", 16.0, [], {}, -1])
def test_until_hour_rejects_junk_types(bad):
    """Молча превращать мусор в «до отмены» нельзя: только None — это «до отмены»."""
    with pytest.raises(ValueError):
        parse_until_hour(bad, N)


def test_until_hour_messages_are_russian():
    with pytest.raises(ValueError, match="Час окончания — целое от 1 до 24"):
        parse_until_hour(25, N)
    with pytest.raises(ValueError, match="Час окончания должен быть позже текущего"):
        parse_until_hour(14, N)


def test_until_hour_on_the_dot_still_needs_a_later_hour():
    """Ровно 14:00 — «до 14:00» уже прошло, ближайший допустимый час — 15."""
    on_dot = datetime(2026, 10, 28, 14, 0)
    with pytest.raises(ValueError):
        parse_until_hour(14, on_dot)
    assert parse_until_hour(15, on_dot) == datetime(2026, 10, 28, 15, 0)


def test_until_24_rolls_over_month_and_year():
    assert parse_until_hour(24, datetime(2026, 12, 31, 23, 10)) == datetime(2027, 1, 1, 0, 0)
    assert parse_until_hour(24, datetime(2026, 2, 28, 0, 5)) == datetime(2026, 3, 1, 0, 0)


def test_until_24_is_valid_at_23_and_first_hour_of_day_can_be_1():
    assert parse_until_hour(24, datetime(2026, 10, 28, 23, 59, 59)) == datetime(2026, 10, 29, 0, 0)
    assert parse_until_hour(1, datetime(2026, 10, 28, 0, 0)) == datetime(2026, 10, 28, 1, 0)


# --- правила: идёт ли эфир сейчас ---------------------------------------------

def test_effective_on_expires_exactly_at_until_msk():
    row = {"is_on": True, "playlist_id": 5, "until_at": datetime(2026, 10, 28, 15, 0)}
    assert effective_on(row, datetime(2026, 10, 28, 14, 59, 59))
    assert not effective_on(row, datetime(2026, 10, 28, 15, 0))


def test_effective_on_false_when_playlist_deleted():
    assert not effective_on({"is_on": True, "playlist_id": None, "until_at": None}, N)


def test_effective_on_until_cancel_never_expires():
    row = {"is_on": True, "playlist_id": 5, "until_at": None}
    assert effective_on(row, datetime(2030, 1, 1)) is True


def test_effective_on_false_when_off_or_no_row():
    assert effective_on({"is_on": False, "playlist_id": 5, "until_at": None}, N) is False
    assert effective_on({"is_on": None, "playlist_id": 5, "until_at": None}, N) is False
    assert effective_on(None, N) is False


# --- хранилище на фейк-БД -----------------------------------------------------

class _Result:
    def __init__(self, rows):
        self._rows = list(rows)
        self.rowcount = len(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows


class RoutedDB:
    """Мок сессии: строки по подстроке SQL (пробелы схлопнуты), без совпадения — пусто.

    Все вызовы копятся в calls; commit() считается: хранилище коммитить не должно.
    """

    def __init__(self, routes):
        self.routes = routes
        self.calls: list[tuple[str, dict]] = []
        self.commits = 0

    def execute(self, query, params=None):
        sql = " ".join(str(query).split())
        self.calls.append((sql, params or {}))
        for needle, rows in self.routes:
            if needle in sql:
                return _Result(rows)
        return _Result([])

    def commit(self):
        self.commits += 1

    def params(self, needle):
        return [p for sql, p in self.calls if needle in sql]

    def sqls(self, needle):
        return [sql for sql, _ in self.calls if needle in sql]


UNTIL = datetime(2026, 10, 28, 16, 0)
SET_AT = datetime(2026, 10, 28, 9, 15)


def STATE(is_on=True, playlist_id=5, name="Акция", until_at=UNTIL, by="admin", at=SET_AT):
    return Row(is_on=is_on, playlist_id=playlist_id, playlist_name=name, until_at=until_at,
               enabled_by=by, enabled_at=at)


def bc_db(state=None, playlist="Акция"):
    """Фейк-БД: состояние эфира (None — строки нет) и (необязательно) плейлист по id."""
    routes = [("FROM network_broadcast", [] if state is None else [state])]
    if playlist is not None:
        routes.append(("FROM playlists", [Row(name=playlist)]))
    return RoutedDB(routes)


def audit(db):
    """Записи журнала: [(title, detail, actor)]."""
    return [(p["t"], p["d"], p["a"]) for p in db.params("INSERT INTO audit_log")]


def test_get_broadcast_when_on():
    out = store.get_broadcast(bc_db(STATE()), N)
    assert out == {"is_on": True, "playlist_id": 5, "playlist_name": "Акция", "until_at": UNTIL,
                   "enabled_by": "admin", "enabled_at": SET_AT}


def test_get_broadcast_until_cancel_has_no_until():
    out = store.get_broadcast(bc_db(STATE(until_at=None)), N)
    assert out["is_on"] is True and out["until_at"] is None


def test_get_broadcast_expired_row_reads_as_off_before_celery_catches_up():
    out = store.get_broadcast(bc_db(STATE(until_at=datetime(2026, 10, 28, 14, 0))), N)
    assert out == {"is_on": False, "playlist_id": None, "playlist_name": None, "until_at": None,
                   "enabled_by": None, "enabled_at": None}


@pytest.mark.parametrize("state", [None, STATE(is_on=False, playlist_id=None, name=None, until_at=None),
                                   STATE(playlist_id=None, name=None)])
def test_get_broadcast_off_shape(state):
    out = store.get_broadcast(bc_db(state), N)
    assert out["is_on"] is False
    assert out["playlist_id"] is None and out["playlist_name"] is None and out["until_at"] is None


def test_enable_when_idle_writes_state_and_journals_enabled():
    db = bc_db(STATE(is_on=False, playlist_id=None, name=None, until_at=None))
    store.enable(db, 7, UNTIL, "admin", now=N)
    (w,) = db.params("INSERT INTO network_broadcast")
    assert (w["pid"], w["until"], w["who"], w["now"]) == (7, UNTIL, "admin", N)
    assert audit(db) == [("Общий эфир включён", "Плейлист: «Акция» · до 16:00", "admin")]
    assert db.commits == 0


def test_enable_until_cancel_detail():
    db = bc_db(None)
    store.enable(db, 7, None, "admin", now=N)
    assert audit(db) == [("Общий эфир включён", "Плейлист: «Акция» · до отмены", "admin")]
    assert db.params("INSERT INTO network_broadcast")[0]["until"] is None


def test_enable_until_midnight_is_written_as_24():
    db = bc_db(None)
    store.enable(db, 7, datetime(2026, 10, 29, 0, 0), "admin", now=N)
    assert audit(db)[0][1] == "Плейлист: «Акция» · до 24:00"


def test_enable_while_running_journals_changed():
    db = bc_db(STATE())          # идёт «Акция» до 16:00
    store.enable(db, 8, None, "admin", now=N)
    assert audit(db) == [("Общий эфир сменён", "Плейлист: «Акция» · до отмены", "admin")]


def test_enable_over_expired_row_is_a_fresh_start_not_a_change():
    """Срок вышел, Celery ещё не успел выключить — для журнала эфир уже не идёт."""
    db = bc_db(STATE(until_at=datetime(2026, 10, 28, 14, 0)))
    store.enable(db, 8, UNTIL, "admin", now=N)
    assert audit(db)[0][0] == "Общий эфир включён"


def test_enable_writes_enabled_at_in_msk_from_python_not_postgres_now():
    db = bc_db(None)
    store.enable(db, 7, None, "admin", now=N)
    sql = db.sqls("INSERT INTO network_broadcast")[0]
    assert "NOW()" not in sql.upper()
    assert db.params("INSERT INTO network_broadcast")[0]["now"] == N


def test_enable_default_now_is_msk(monkeypatch):
    monkeypatch.setattr(store, "now_msk", lambda: SET_AT)
    db = bc_db(None)
    store.enable(db, 7, None, "admin")
    assert db.params("INSERT INTO network_broadcast")[0]["now"] == SET_AT


def test_enable_unknown_playlist_changes_nothing():
    db = bc_db(STATE(), playlist=None)
    with pytest.raises(LookupError):
        store.enable(db, 99, None, "admin", now=N)
    assert not db.params("INSERT INTO network_broadcast") and not audit(db)


def test_disable_clears_state_and_journals():
    db = bc_db(STATE())
    store.disable(db, "admin")
    (sql,) = db.sqls("UPDATE network_broadcast")
    assert "is_on = FALSE" in sql and "playlist_id = NULL" in sql and "until_at = NULL" in sql
    assert audit(db) == [("Общий эфир выключен", "Плейлист: «Акция»", "admin")]
    assert db.commits == 0


def test_disable_custom_title_and_no_detail_when_nothing_was_on():
    db = bc_db(None)
    store.disable(db, "система", "Общий эфир завершён по времени")
    assert audit(db) == [("Общий эфир завершён по времени", None, "система")]


def test_expire_due_switches_off_after_until_and_journals_it():
    db = bc_db(STATE(until_at=UNTIL))
    assert store.expire_due(db, datetime(2026, 10, 28, 16, 0, 30)) is True
    assert db.sqls("UPDATE network_broadcast") and "is_on = FALSE" in db.sqls("UPDATE network_broadcast")[0]
    assert audit(db) == [("Общий эфир завершён по времени", "Плейлист: «Акция»", "система")]
    assert db.commits == 0


def test_expire_due_at_the_exact_until_moment():
    db = bc_db(STATE(until_at=UNTIL))
    assert store.expire_due(db, UNTIL) is True


@pytest.mark.parametrize("state", [
    STATE(until_at=UNTIL),                                # срок ещё впереди
    STATE(until_at=None),                                 # до отмены
    STATE(is_on=False, playlist_id=None, name=None, until_at=UNTIL),   # уже выключен
    None,                                                 # строки нет
])
def test_expire_due_does_nothing_before_until(state):
    db = bc_db(state)
    assert store.expire_due(db, datetime(2026, 10, 28, 15, 59, 59)) is False
    assert not db.sqls("UPDATE network_broadcast") and not audit(db)


def test_expire_due_locks_the_row_so_a_fresh_enable_is_not_switched_off():
    """Админ включил новый эфир ровно в момент проверки: ждём его и перечитываем строку."""
    db = bc_db(STATE(until_at=UNTIL))
    store.expire_due(db, UNTIL)
    assert any("FOR UPDATE OF b" in s for s in db.sqls("FROM network_broadcast"))


# --- роутер: разбор запроса и вид ответа --------------------------------------

def test_on_request_without_body_is_until_cancel_backward_compat():
    assert parse_on_request(None, 5, N) == (5, None)


def test_on_request_json_body_with_until_hour():
    assert parse_on_request({"playlist_id": 5, "until_hour": 16}, None, N) == (5, datetime(2026, 10, 28, 16, 0))
    assert parse_on_request({"playlist_id": 5, "until_hour": 24}, None, N) == (5, datetime(2026, 10, 29, 0, 0))


@pytest.mark.parametrize("body", [{"playlist_id": 5}, {"playlist_id": 5, "until_hour": None}])
def test_on_request_missing_or_null_until_hour_is_until_cancel(body):
    assert parse_on_request(body, None, N) == (5, None)


def test_on_request_body_wins_over_query_but_query_fills_a_missing_playlist():
    assert parse_on_request({"playlist_id": 6}, 5, N)[0] == 6
    assert parse_on_request({"until_hour": 16}, 5, N) == (5, datetime(2026, 10, 28, 16, 0))


@pytest.mark.parametrize("body,query", [
    (None, None), ({}, None), ({"playlist_id": None}, None), ({"playlist_id": True}, None),
    ({"playlist_id": "x"}, None), ({"playlist_id": 0}, None), ({"playlist_id": -3}, None),
    ({"playlist_id": 5.5}, None), ("строка", None), ([1], None), (None, 0),
])
def test_on_request_rejects_bad_playlist_with_400(body, query):
    with pytest.raises(HTTPException) as e:
        parse_on_request(body, query, N)
    assert e.value.status_code == 400


@pytest.mark.parametrize("hour", [14, 13, 0, 25, "x", 15.5, True, "16"])
def test_on_request_bad_until_hour_is_400_with_russian_text(hour):
    with pytest.raises(HTTPException) as e:
        parse_on_request({"playlist_id": 5, "until_hour": hour}, None, N)
    assert e.value.status_code == 400
    assert "Час окончания" in e.value.detail


def test_broadcast_json_serializes_dates_as_iso():
    out = broadcast_json({"is_on": True, "playlist_id": 5, "playlist_name": "Акция", "until_at": UNTIL,
                          "enabled_by": "admin", "enabled_at": SET_AT})
    assert out["until_at"] == "2026-10-28T16:00:00" and out["enabled_at"] == "2026-10-28T09:15:00"
    assert out["is_on"] is True and out["playlist_name"] == "Акция"


def test_broadcast_json_off_keeps_nulls():
    out = broadcast_json({"is_on": False, "playlist_id": None, "playlist_name": None, "until_at": None,
                          "enabled_by": None, "enabled_at": None})
    assert out["is_on"] is False and out["until_at"] is None and out["enabled_at"] is None
