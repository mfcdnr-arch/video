"""placements_store: разбор строк БД в формат placements_logic и запись показа.

Настоящего Postgres здесь нет. RoutedDB отдаёт строки по куску текста SQL
(а не по порядку вызовов), поэтому тесты не зависят от того, в какой
последовательности модуль делает запросы. Проверяется то, что легко сломать
незаметно: развёртывание групповых переопределений, приоритет экрана над
группой на ту же дату, «все экраны» на момент чтения, наивные МСК-даты.
Сами SQL-запросы против живой схемы гоняет smoke_test.sh на стенде.
"""
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace as Row

import placements_store as store


class _Result:
    def __init__(self, rows):
        self._rows = list(rows)
        self.rowcount = len(self._rows)        # для UPDATE/DELETE: «затронуто строк» = строк в маршруте

    def fetchall(self):
        return self._rows

    def scalar(self):
        return self._rows[0] if self._rows else None


class RoutedDB:
    """Мок сессии: строки выбираются по подстроке SQL (пробелы схлопнуты).

    routes — [(подстрока, строки)]; первая совпавшая подстрока побеждает,
    без совпадения — пустой результат. Все вызовы копятся в calls.
    """

    def __init__(self, routes):
        self.routes = routes
        self.calls: list[tuple[str, dict]] = []

    def execute(self, query, params=None):
        sql = " ".join(str(query).split())
        self.calls.append((sql, params or {}))
        for needle, rows in self.routes:
            if needle in sql:
                return _Result(rows)
        return _Result([])

    def params(self, needle):
        return [p for sql, p in self.calls if needle in sql]


D1, D2 = date(2026, 11, 1), date(2026, 11, 2)

# экраны 1 и 2 — в группе 7, экран 3 — в группе 8, экран 4 — без группы
GROUP_MEMBERS = [Row(id=1, group_id=7), Row(id=2, group_id=7), Row(id=3, group_id=8)]


def OV(id, on_date, screen_id=None, group_id=None, playlist_id=None, is_off=False):
    return Row(id=id, screen_id=screen_id, group_id=group_id, on_date=on_date, playlist_id=playlist_id,
               playlist_name=None if playlist_id is None else f"PL{playlist_id}", is_off=is_off)


def overrides_db(rows):
    return RoutedDB([("FROM schedule_overrides", rows),
                     ("SELECT id, group_id FROM screens", GROUP_MEMBERS)])


# --- переопределения ---------------------------------------------------------

def test_group_override_expands_to_every_screen_of_group():
    out = store.load_overrides(overrides_db([OV(10, D1, group_id=7, playlist_id=5)]), D1, D2)
    assert [(o["id"], o["screen_id"], o["on_date"], o["playlist_id"]) for o in out] == [
        (10, 1, D1, 5), (10, 2, D1, 5)]
    assert all(set(o) == {"id", "screen_id", "on_date", "playlist_id", "playlist_name", "is_off"} for o in out)


def test_screen_override_replaces_group_one_on_same_date_for_that_screen_only():
    rows = [OV(11, D1, screen_id=2, is_off=True),            # своё у экрана 2 на D1 (раньше группового по id)
            OV(10, D1, group_id=7, playlist_id=5),           # группа 7 на D1
            OV(12, D2, group_id=7, playlist_id=6)]           # группа 7 на D2 — у экрана 2 своего нет
    out = {(o["screen_id"], o["on_date"]): o for o in store.load_overrides(overrides_db(rows), D1, D2)}
    assert set(out) == {(1, D1), (2, D1), (1, D2), (2, D2)}
    assert out[(1, D1)]["id"] == 10 and out[(1, D1)]["playlist_id"] == 5
    assert out[(2, D1)]["id"] == 11 and out[(2, D1)]["is_off"] is True and out[(2, D1)]["playlist_id"] is None
    assert out[(2, D2)]["id"] == 12


def test_screen_override_without_group_and_empty_group():
    rows = [OV(13, D1, screen_id=4, playlist_id=9), OV(14, D1, group_id=99, playlist_id=9)]
    out = store.load_overrides(overrides_db(rows), D1, D2)
    assert [(o["id"], o["screen_id"]) for o in out] == [(13, 4)]


# --- показы ------------------------------------------------------------------

SAVED = datetime(2026, 10, 26, 16, 57, 46)      # наивное МСК, как отдаёт psycopg2 из TIMESTAMP
STOPPED = datetime(2026, 11, 2, 10, 37)


def PL(id, all_screens=False, stopped_at=None, saved_at=SAVED):
    return Row(id=id, playlist_id=100 + id, playlist_name=f"PL{100 + id}", all_screens=all_screens,
               date_from=D1, date_to=D2, hour_from=9, hour_to=13, stopped_at=stopped_at,
               saved_at=saved_at, created_by="admin", created_at=saved_at)


def placements_db(rows, placement_screens, screens):
    return RoutedDB([("FROM placements pl", rows),
                     ("FROM placement_screens", placement_screens),
                     ("SELECT id FROM screens", [Row(id=s) for s in screens])])


def test_all_screens_placement_gets_every_screen_at_read_time():
    db = placements_db([PL(1), PL(2, all_screens=True)],
                       [Row(placement_id=1, screen_id=1)],   # у «всех экранов» строк нет
                       screens=[1, 2, 5])                    # экран 5 — нигде не указан явно
    by_id = {p["id"]: p for p in store.load_placements(db, D1, D2)}
    assert by_id[1]["screens"] == {1} and by_id[1]["all_screens"] is False
    assert by_id[2]["screens"] == {1, 2, 5} and by_id[2]["all_screens"] is True


def test_screens_sets_are_independent_copies():
    db = placements_db([PL(1, all_screens=True), PL(2, all_screens=True)], [], screens=[1, 2])
    a, b = store.load_placements(db, D1, D2)
    a["screens"].add(99)
    assert b["screens"] == {1, 2}


def test_loaded_placement_has_naive_saved_at_and_stopped_at():
    db = placements_db([PL(1, stopped_at=STOPPED)], [Row(placement_id=1, screen_id=3)], screens=[3])
    (p,) = store.load_placements(db, D1, D2)
    assert p["saved_at"] == SAVED and p["saved_at"].tzinfo is None
    assert p["stopped_at"] == STOPPED and p["stopped_at"].tzinfo is None
    assert {"id", "playlist_id", "playlist_name", "date_from", "date_to", "hour_from", "hour_to",
            "stopped_at", "saved_at", "screens", "all_screens", "created_by", "created_at"} <= set(p)


def test_load_placements_playlist_filter_and_empty():
    db = placements_db([], [], screens=[1])
    assert store.load_placements(db, D1, D2, playlist_id=7) == []
    sql, params = db.calls[0]
    assert "pl.playlist_id = :pid" in sql and params["pid"] == 7
    assert len(db.calls) == 1                          # без показов экраны не запрашиваются


def test_load_decisions():
    db = RoutedDB([("FROM placement_decisions", [Row(placement_id=3, screen_id=2, other_kind="placement",
                                                     other_id=1, mode="alternate")])])
    assert store.load_decisions(db, [3]) == {(3, 2, "placement", 1): "alternate"}
    assert store.load_decisions(RoutedDB([]), []) == {}


# --- запись ------------------------------------------------------------------

def _msk_now_naive():
    return datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=3)


DATA = {"playlist_id": 5, "all_screens": False, "screens": {3, 1}, "date_from": D1, "date_to": D2,
        "hour_from": 9, "hour_to": 13}


def test_now_msk_is_naive_moscow_time():
    before, now, after = _msk_now_naive(), store.now_msk(), _msk_now_naive()
    assert now.tzinfo is None and before <= now <= after


def test_save_new_placement_writes_saved_at_msk_screens_decisions_audit():
    db = RoutedDB([("INSERT INTO placements", [42]), ("SELECT name FROM playlists", ["PL5"])])
    before = _msk_now_naive()
    pid = store.save_placement(db, DATA, [{"screen_id": 1, "other_kind": "placement", "other_id": 7,
                                           "mode": "replace"}], "admin")
    after = _msk_now_naive()
    assert pid == 42
    (ins,) = db.params("INSERT INTO placements")
    assert ins["now"].tzinfo is None and before <= ins["now"] <= after and ins["by"] == "admin"
    assert [p["sid"] for p in db.params("INSERT INTO placement_screens")] == [1, 3]
    assert db.params("INSERT INTO placement_decisions") == [
        {"id": 42, "sid": 1, "kind": "placement", "oid": 7, "mode": "replace"}]
    (audit,) = db.params("INSERT INTO audit_log")
    assert audit["t"] == "Показ: создан" and audit["a"] == "admin"


def test_edit_placement_bumps_saved_at_keeps_stopped_at_all_screens_has_no_rows():
    db = RoutedDB([("SELECT name FROM playlists", ["PL5"])])
    pid = store.save_placement(db, {**DATA, "all_screens": True, "screens": {1, 2, 3}}, [], "admin",
                               placement_id=8)
    assert pid == 8
    update = [sql for sql, _ in db.calls if sql.startswith("UPDATE placements")]
    assert len(update) == 1 and "saved_at = :now" in update[0] and "stopped_at" not in update[0]
    (params,) = db.params("UPDATE placements")
    assert params["id"] == 8 and params["now"].tzinfo is None
    assert db.params("DELETE FROM placement_screens") == [{"id": 8}]
    assert db.params("INSERT INTO placement_screens") == []
    assert db.params("INSERT INTO placements") == []
    assert db.params("INSERT INTO audit_log")[0]["t"] == "Показ: изменён"


# --- решения по пересечениям в журнале (§4.7) ----------------------------------

def DEC(screen_id, kind, other_id, mode):
    return {"screen_id": screen_id, "other_kind": kind, "other_id": other_id, "mode": mode}


def test_decisions_text_names_screen_mode_and_the_other_source():
    text = store.decisions_text(
        [DEC(2, "override", 30, "alternate"), DEC(1, "placement", 7, "replace")],
        {1: "Холл", 2: "Касса"},
        {("placement", 7): ("Утро", None), ("override", 30): ("Осень", D1)})
    # по названию экрана: «Касса» раньше «Холл»
    assert text == ("экран «Касса»: по очереди с переопределением 01.11 «Осень»; "
                    "экран «Холл»: заменяет показ #7 «Утро»")


def test_decisions_text_without_names_falls_back_to_numbers():
    text = store.decisions_text([DEC(5, "placement", 7, "alternate"), DEC(5, "override", 9, "replace")], {}, {})
    assert text == "экран «#5»: заменяет переопределение #9; экран «#5»: по очереди с показом #7"


def test_decisions_text_yield_names_who_stays():
    text = store.decisions_text(
        [DEC(1, "placement", 7, "yield"), DEC(2, "override", 30, "yield")],
        {1: "Холл", 2: "Касса"},
        {("placement", 7): ("Утро", None), ("override", 30): ("Осень", D1)})
    assert text == ("экран «Касса»: уступает переопределению 01.11 «Осень»; "
                    "экран «Холл»: уступает показу #7 «Утро»")
    assert store.decisions_text([DEC(5, "placement", 7, "yield")], {}, {}) == "экран «#5»: уступает показу #7"


def test_decisions_text_is_cut_with_and_more():
    decisions = [DEC(i, "placement", 7, "replace") for i in range(1, 9)]
    text = store.decisions_text(decisions, {i: f"Э{i}" for i in range(1, 9)}, {("placement", 7): ("Утро", None)},
                                shown=3)
    assert text.count("заменяет показ #7 «Утро»") == 3 and text.endswith("; и ещё 5")
    assert store.decisions_text(decisions[:3], {}, {}, shown=3).count("и ещё") == 0


def test_save_writes_readable_decisions_into_audit():
    db = RoutedDB([("INSERT INTO placements", [42]), ("SELECT name FROM playlists", ["PL5"]),
                   ("SELECT id, name FROM screens", [Row(id=1, name="Холл"), Row(id=3, name="Касса")]),
                   ("FROM placements pl JOIN playlists", [Row(id=7, name="Утро")]),
                   ("FROM schedule_overrides", [Row(id=30, on_date=D2, name="Осень")])])
    store.save_placement(db, DATA, [DEC(1, "placement", 7, "replace"), DEC(3, "override", 30, "alternate")],
                         "admin")
    (audit,) = db.params("INSERT INTO audit_log")
    assert audit["d"].endswith("; решения по пересечениям (2): экран «Касса»: по очереди с переопределением "
                               "02.11 «Осень»; экран «Холл»: заменяет показ #7 «Утро»")
    assert db.params("SELECT id, name FROM screens") == [{"ids": [1, 3]}]


def test_save_without_decisions_queries_no_names():
    db = RoutedDB([("INSERT INTO placements", [42]), ("SELECT name FROM playlists", ["PL5"])])
    store.save_placement(db, DATA, [], "admin")
    assert "решени" not in db.params("INSERT INTO audit_log")[0]["d"]
    assert db.params("FROM screens") == [] and db.params("FROM schedule_overrides") == []


# --- чтение одного показа, слоты шаблона, названия -----------------------------

def test_get_placement_and_missing():
    row = Row(id=4, playlist_id=9, playlist_name="Утро", date_from=D1, date_to=D2, hour_from=9, hour_to=13,
              stopped_at=None)
    db = RoutedDB([("FROM placements pl", [row])])
    assert store.get_placement(db, 4) == {"id": 4, "playlist_id": 9, "playlist_name": "Утро", "date_from": D1,
                                          "date_to": D2, "hour_from": 9, "hour_to": 13, "stopped_at": None}
    assert db.params("FROM placements pl") == [{"id": 4}]
    assert store.get_placement(RoutedDB([]), 4) is None


def test_load_template_slots_reads_the_whole_template_in_one_query():
    rows = [Row(screen_id=5, group_id=None, day_of_week=0, hour=9, playlist_id=2),
            Row(screen_id=None, group_id=7, day_of_week=1, hour=None, playlist_id=3),
            Row(screen_id=None, group_id=None, day_of_week=2, hour=None, playlist_id=1)]
    db = RoutedDB([("FROM schedule_slots", rows)])
    assert store.load_template_slots(db) == [
        {"screen_id": 5, "group_id": None, "day_of_week": 0, "hour": 9, "playlist_id": 2},
        {"screen_id": None, "group_id": 7, "day_of_week": 1, "hour": None, "playlist_id": 3},
        {"screen_id": None, "group_id": None, "day_of_week": 2, "hour": None, "playlist_id": 1}]
    assert len(db.calls) == 1 and "WHERE" not in db.calls[0][0]          # весь шаблон, без фильтра по экрану


def test_load_screen_slots_keeps_level_priority_for_the_screen():
    rows = [Row(screen_id=None, group_id=None, day_of_week=0, hour=None, playlist_id=1),
            Row(screen_id=5, group_id=None, day_of_week=0, hour=9, playlist_id=2),
            Row(screen_id=None, group_id=7, day_of_week=0, hour=10, playlist_id=3),
            Row(screen_id=None, group_id=8, day_of_week=0, hour=10, playlist_id=4),
            Row(screen_id=6, group_id=None, day_of_week=0, hour=9, playlist_id=5)]
    db = RoutedDB([("FROM schedule_slots", rows), ("SELECT group_id FROM screens", [7])])
    assert store.load_screen_slots(db, 5) == [
        {"day_of_week": 0, "hour": None, "playlist_id": 1, "prio": 1},
        {"day_of_week": 0, "hour": 9, "playlist_id": 2, "prio": 3},
        {"day_of_week": 0, "hour": 10, "playlist_id": 3, "prio": 2}]       # чужие группа и экран — мимо
    assert db.params("SELECT group_id FROM screens") == [{"sid": 5}]


def test_load_playlist_names():
    db = RoutedDB([("FROM playlists", [Row(id=1, name="Общий"), Row(id=2, name="Утро")])])
    assert store.load_playlist_names(db, {2, 1, 2}) == {1: "Общий", 2: "Утро"}
    assert db.params("FROM playlists") == [{"ids": [1, 2]}]
    empty = RoutedDB([])
    assert store.load_playlist_names(empty, set()) == {} and empty.calls == []        # без id в БД не ходим


# --- остановка и отмена --------------------------------------------------------

PLC = {"id": 8, "playlist_id": 5, "playlist_name": "Утро", "date_from": D1, "date_to": D2,
       "hour_from": 9, "hour_to": 13, "stopped_at": None}
NOW = datetime(2026, 11, 1, 10, 37)


def test_stop_writes_python_msk_now_only_if_not_stopped_yet_and_audits():
    db = RoutedDB([("UPDATE placements", [1])])
    assert store.stop_placement(db, PLC, "admin", NOW) is True
    (upd,) = db.params("UPDATE placements")
    assert upd == {"now": NOW, "id": 8}
    sql = db.calls[0][0]
    assert "stopped_at = :now" in sql and "stopped_at IS NULL" in sql and "NOW()" not in sql.upper()
    (audit,) = db.params("INSERT INTO audit_log")
    assert audit["t"] == "Показ: остановлен" and audit["a"] == "admin"
    assert "#8" in audit["d"] and "«Утро»" in audit["d"] and "01.11.2026–02.11.2026" in audit["d"] \
        and "09:00–13:00" in audit["d"] and "01.11.2026 10:37" in audit["d"]


def test_stop_already_stopped_by_someone_else_writes_nothing_more():
    db = RoutedDB([])                                   # UPDATE ничего не затронул
    assert store.stop_placement(db, PLC, "admin", NOW) is False
    assert db.params("INSERT INTO audit_log") == []


def test_stop_without_now_uses_msk_now():
    db = RoutedDB([("UPDATE placements", [1])])
    before = _msk_now_naive()
    store.stop_placement(db, PLC, "admin")
    (upd,) = db.params("UPDATE placements")
    assert upd["now"].tzinfo is None and before <= upd["now"] <= _msk_now_naive()


def test_delete_removes_placement_and_audits_cancellation():
    db = RoutedDB([])
    store.delete_placement(db, PLC, "admin")
    assert db.params("DELETE FROM placements") == [{"id": 8}]
    (audit,) = db.params("INSERT INTO audit_log")
    assert audit["t"] == "Показ: отменён" and audit["a"] == "admin"
    assert "#8" in audit["d"] and "«Утро»" in audit["d"] and "09:00–13:00" in audit["d"]
