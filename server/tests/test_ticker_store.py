"""ticker_store: SQL бегущей строки — запись, срок, журнал, лента экрана, команды, шаблоны.

Настоящего Postgres здесь нет: RoutedDB (из test_broadcast) отдаёт строки по куску текста SQL,
поэтому тесты не зависят от порядка запросов. Проверяется то, что легко сломать незаметно: срок
и моменты — наивное МСК из Python (не NOW() Postgres), `term_given=False` сохраняет прежний срок,
истёкшую строку нельзя править, снятие одной строки не гасит остальные, `commit` хранилище не делает.
Сами SQL-запросы и миграцию 034 против живой схемы гоняет стенд (migrate.sh, smoke_test.sh).
"""
import json
import os
import re
from datetime import datetime, timedelta
from types import SimpleNamespace as Row

import pytest

import ticker_store as store
from screen_online import offline_for_sql, online_sql
from ticker_rules import parse_ticker_body
from test_broadcast import RoutedDB

N = datetime(2026, 11, 2, 14, 30)
MIGRATIONS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "migrations")


def data(**kw):
    """Вход create/update — настоящий выход parse_ticker_body (срок считается от N)."""
    return parse_ticker_body({"message": "Касса закрыта", "target": "all", **kw}, N)


def TK(id=11, message="Касса закрыта", color="#ffd34d", speed="medium", target="all", group_ids=None,
       screen_ids=None, until_at=None, started_at=N - timedelta(minutes=5), started_by="admin",
       changed_at=None, changed_by="admin"):
    """Строка из list_live/targets_of: что отдаёт БД (массивы — списки)."""
    return Row(id=id, message=message, color=color, speed=speed, target=target,
               group_ids=group_ids or [], screen_ids=screen_ids or [], until_at=until_at,
               started_at=started_at, started_by=started_by,
               changed_at=changed_at or started_at, changed_by=changed_by)


def journal(db):
    """Записи журнала: [(title, detail, actor)]."""
    return [(p["t"], p["d"], p["a"]) for p in db.params("INSERT INTO audit_log")]


def commands(db):
    """Поставленные команды: [(screen_id, type, payload)]."""
    return [(p["sid"], p["t"], json.loads(p["p"])) for p in db.params("INSERT INTO commands")]


def test_constants():
    assert store.AUDIT_TYPE == "ticker"
    assert store.TITLE_ON == "Бегущая строка запущена"
    assert store.TITLE_CHANGED == "Бегущая строка изменена"
    assert store.TITLE_OFF == "Бегущая строка снята"
    assert store.TITLE_EXPIRED == "Бегущая строка снята по сроку"
    assert store.SYSTEM_ACTOR == "система"


# --- create -------------------------------------------------------------------

def create_db():
    return RoutedDB([("INSERT INTO tickers", [Row(id=11)]),
                     ("FROM screens WHERE id = ANY", [Row(id=1), Row(id=2)]),
                     ("FROM sync_groups WHERE id = ANY", [Row(id=7), Row(id=8)])])


def test_create_writes_msk_times_and_journals():
    db = create_db()
    d = data(target="screens", screen_ids=[2, 1], term={"kind": "minutes", "minutes": 15})
    assert store.create(db, d, "admin", N) == 11
    (p,) = db.params("INSERT INTO tickers")
    assert p["now"] == N and p["who"] == "admin"                       # started_at = changed_at = now
    assert p["until"] == N + timedelta(minutes=15)
    assert (p["msg"], p["color"], p["speed"], p["target"], p["is_all"]) == ("Касса закрыта", "#ffd34d", "medium", "screens", False)
    sql = db.sqls("INSERT INTO tickers")[0]
    assert "NOW()" not in sql.upper()                                  # моменты — из Python, не из Postgres
    for col in ("started_at", "started_by", "changed_at", "changed_by", "until_at", "target"):
        assert col in sql
    assert db.params("INSERT INTO ticker_screens") == [{"tid": 11, "sid": 1}, {"tid": 11, "sid": 2}]
    assert db.params("INSERT INTO ticker_groups") == []
    assert journal(db) == [("Бегущая строка запущена", "Касса закрыта", "admin")]
    assert db.params("INSERT INTO audit_log")[0]["type"] == "ticker"
    assert db.commits == 0 and db.rollbacks == 0


def test_create_groups_writes_ticker_groups_only():
    db = create_db()
    store.create(db, data(target="groups", group_ids=[8, 7]), "admin", N)
    assert db.params("INSERT INTO ticker_groups") == [{"tid": 11, "gid": 7}, {"tid": 11, "gid": 8}]
    assert db.params("INSERT INTO ticker_screens") == []
    assert db.params("INSERT INTO tickers")[0]["target"] == "groups"


def test_create_all_has_no_links_and_until_cancel():
    db = create_db()
    store.create(db, data(), "admin", N)
    (p,) = db.params("INSERT INTO tickers")
    assert p["target"] == "all" and p["is_all"] is True and p["until"] is None
    assert db.params("INSERT INTO ticker_screens") == [] and db.params("INSERT INTO ticker_groups") == []
    assert not db.sqls("DELETE FROM")                                    # у новой строки связей ещё нет


def test_create_unknown_screen_is_value_error_and_writes_nothing():
    db = create_db()                                                     # экраны 1 и 2 есть, 99 нет
    with pytest.raises(ValueError, match="Экран не найден"):
        store.create(db, data(target="screens", screen_ids=[1, 99]), "admin", N)
    assert not db.params("INSERT INTO tickers") and not journal(db)


def test_create_unknown_group_is_value_error_and_writes_nothing():
    db = create_db()
    with pytest.raises(ValueError, match="Группа не найдена"):
        store.create(db, data(target="groups", group_ids=[7, 99]), "admin", N)
    assert not db.params("INSERT INTO tickers") and not journal(db)


# --- update -------------------------------------------------------------------

OLD_UNTIL = N + timedelta(minutes=20)


def update_db(until=OLD_UNTIL):
    return RoutedDB([("FOR UPDATE", [Row(id=11, until_at=until)]),
                     ("FROM screens WHERE id = ANY", [Row(id=1), Row(id=2)]),
                     ("FROM sync_groups WHERE id = ANY", [Row(id=7)])])


def test_update_keeps_until_when_term_not_given():
    db = update_db()
    d = parse_ticker_body({"message": "Новый текст", "color": "#ff6b5e", "target": "all"}, N, keep_term=True)
    assert d["term_given"] is False
    assert store.update(db, 11, d, "oper", N) is None
    (p,) = db.params("UPDATE tickers")
    assert p["until"] == OLD_UNTIL
    assert (p["msg"], p["color"], p["now"], p["who"], p["id"]) == ("Новый текст", "#ff6b5e", N, "oper", 11)
    assert journal(db) == [("Бегущая строка изменена", "Новый текст", "oper")]
    assert db.commits == 0


def test_update_keeps_until_cancel_too():
    db = update_db(until=None)                                           # «до снятия» остаётся «до снятия»
    d = parse_ticker_body({"message": "x", "target": "all"}, N, keep_term=True)
    store.update(db, 11, d, "oper", N)
    assert db.params("UPDATE tickers")[0]["until"] is None


def test_update_new_term_from_save_moment():
    db = update_db()
    d = parse_ticker_body({"message": "x", "target": "all", "term": {"kind": "minutes", "minutes": 30}}, N, keep_term=True)
    store.update(db, 11, d, "oper", N)
    assert db.params("UPDATE tickers")[0]["until"] == N + timedelta(minutes=30)


def test_update_cancel_term_clears_until():
    db = update_db()
    d = parse_ticker_body({"message": "x", "target": "all", "term": {"kind": "cancel"}}, N, keep_term=True)
    assert d["term_given"] is True
    store.update(db, 11, d, "oper", N)
    assert db.params("UPDATE tickers")[0]["until"] is None


def test_update_does_not_touch_start_fields_and_uses_python_now():
    db = update_db()
    store.update(db, 11, data(), "oper", N)
    sql = db.sqls("UPDATE tickers")[0]
    assert "NOW()" not in sql.upper()
    assert "started_at" not in sql and "started_by" not in sql
    assert "changed_at" in sql and "changed_by" in sql
    assert any("FOR UPDATE" in s for s in db.sqls("FROM tickers"))      # строка под блокировкой


def test_update_rebuilds_links():
    db = update_db()
    store.update(db, 11, data(target="screens", screen_ids=[2, 1]), "oper", N)
    assert db.params("DELETE FROM ticker_screens") == [{"tid": 11}]
    assert db.params("DELETE FROM ticker_groups") == [{"tid": 11}]
    assert db.params("INSERT INTO ticker_screens") == [{"tid": 11, "sid": 1}, {"tid": 11, "sid": 2}]
    assert db.params("INSERT INTO ticker_groups") == []

    db = update_db()                                                     # ушли с экранов на группу
    store.update(db, 11, data(target="groups", group_ids=[7]), "oper", N)
    assert db.params("DELETE FROM ticker_screens") == [{"tid": 11}]
    assert db.params("INSERT INTO ticker_groups") == [{"tid": 11, "gid": 7}]
    assert db.params("INSERT INTO ticker_screens") == []

    db = update_db()                                                     # на «все» — связей нет
    store.update(db, 11, data(), "oper", N)
    assert db.params("DELETE FROM ticker_screens") and db.params("DELETE FROM ticker_groups")
    assert not db.params("INSERT INTO ticker_screens") and not db.params("INSERT INTO ticker_groups")


def test_update_expired_ticker_is_lookup_error():
    db = update_db(until=N - timedelta(minutes=1))
    with pytest.raises(LookupError, match="Бегущая строка не найдена"):
        store.update(db, 11, data(), "oper", N)
    assert not db.params("UPDATE tickers") and not journal(db)


def test_update_at_the_exact_until_moment_counts_as_expired():
    db = update_db(until=N)
    with pytest.raises(LookupError):
        store.update(db, 11, data(), "oper", N)
    assert not db.params("UPDATE tickers")


def test_update_missing_ticker_is_lookup_error():
    db = RoutedDB([])
    with pytest.raises(LookupError, match="Бегущая строка не найдена"):
        store.update(db, 11, data(), "oper", N)
    assert not db.params("UPDATE tickers") and not journal(db)


def test_update_unknown_screen_is_value_error_and_writes_nothing():
    db = update_db()
    with pytest.raises(ValueError, match="Экран не найден"):
        store.update(db, 11, data(target="screens", screen_ids=[1, 99]), "oper", N)
    assert not db.params("UPDATE tickers") and not db.sqls("DELETE FROM") and not journal(db)


# --- remove / expire_due ------------------------------------------------------

def test_remove_deletes_and_journals():
    db = RoutedDB([("DELETE FROM tickers", [Row(message="Касса закрыта")])])
    assert store.remove(db, 11, "admin", N) is None
    assert db.params("DELETE FROM tickers") == [{"id": 11}]
    assert journal(db) == [("Бегущая строка снята", "Касса закрыта", "admin")]
    assert db.commits == 0


def test_remove_missing_is_lookup_error():
    db = RoutedDB([])
    with pytest.raises(LookupError, match="Бегущая строка не найдена"):
        store.remove(db, 11, "admin", N)
    assert not journal(db)


def test_expire_due_journals_each_once():
    db = RoutedDB([("DELETE FROM tickers", [Row(id=1, message="Первая"), Row(id=2, message="Вторая")])])
    assert store.expire_due(db, N) == 2
    assert db.params("DELETE FROM tickers") == [{"now": N}]
    assert "until_at <= :now" in db.sqls("DELETE FROM tickers")[0]
    assert journal(db) == [("Бегущая строка снята по сроку", "Первая", "система"),
                           ("Бегущая строка снята по сроку", "Вторая", "система")]
    assert db.commits == 0


def test_expire_due_nothing_due_writes_nothing():
    db = RoutedDB([])
    assert store.expire_due(db, N) == 0
    assert not journal(db)


# --- чтение -------------------------------------------------------------------

def test_list_live_filters_by_python_now():
    db = RoutedDB([("FROM tickers t", [TK(until_at=N + timedelta(minutes=5)), TK(id=12)])])
    out = store.list_live(db, N)
    (sql,) = db.sqls("FROM tickers t")
    assert "NOW()" not in sql.upper()
    assert "t.until_at IS NULL OR t.until_at > :now" in sql
    assert db.params("FROM tickers t") == [{"now": N}]
    assert [t["id"] for t in out] == [11, 12]


def test_list_live_row_shape():
    row = TK(target="groups", group_ids=[7, 8], until_at=N + timedelta(minutes=5), changed_at=N - timedelta(minutes=1))
    (out,) = store.list_live(RoutedDB([("FROM tickers t", [row])]), N)
    assert out == {"id": 11, "message": "Касса закрыта", "color": "#ffd34d", "speed": "medium",
                   "target": "groups", "group_ids": [7, 8], "screen_ids": [],
                   "until_at": N + timedelta(minutes=5), "started_at": N - timedelta(minutes=5),
                   "started_by": "admin", "changed_at": N - timedelta(minutes=1), "changed_by": "admin"}


def test_list_live_null_arrays_become_empty_lists():
    row = TK()
    row.group_ids = row.screen_ids = None
    (out,) = store.list_live(RoutedDB([("FROM tickers t", [row])]), N)
    assert out["group_ids"] == [] and out["screen_ids"] == []


def test_screens_brief_shape_and_uses_the_one_online_rule():
    row = Row(id=1, name="Зал", group_id=7, group_name="Зал 1", online=True, offline_for_seconds=12,
              agent_version="1.4.0", ticker_ids=[5], ticker_stopped=False, ticker_screen_off=None)
    old = Row(id=2, name="Касса", group_id=None, group_name=None, online=False, offline_for_seconds=None,
              agent_version=None, ticker_ids=None, ticker_stopped=None, ticker_screen_off=None)
    db = RoutedDB([("FROM screens s", [row, old])])
    out = store.screens_brief(db)
    (sql,) = db.sqls("FROM screens s")
    assert online_sql("s") in sql and offline_for_sql("s") in sql        # своё «в сети» не заводим
    assert out[0] == {"id": 1, "name": "Зал", "group_id": 7, "group_name": "Зал 1", "online": True,
                      "offline_for_seconds": 12, "agent_version": "1.4.0", "ticker_ids": [5],
                      "ticker_stopped": False, "ticker_screen_off": None}
    assert out[1]["online"] is False and out[1]["ticker_ids"] is None and out[1]["group_id"] is None
    assert db.commits == 0


def test_for_screen_shape_order_and_aim():
    live = [
        TK(id=21, message="Новая", target="all", started_at=N - timedelta(minutes=1),
           until_at=N + timedelta(minutes=15)),
        TK(id=20, message="Старая", target="groups", group_ids=[7], started_at=N - timedelta(minutes=9)),
        TK(id=22, message="Чужая группа", target="groups", group_ids=[9]),
        TK(id=23, message="Чужой экран", target="screens", screen_ids=[2]),
        TK(id=24, message="Мой экран", target="screens", screen_ids=[1], started_at=N - timedelta(minutes=3)),
    ]
    db = RoutedDB([("SELECT group_id FROM screens", [Row(group_id=7)]), ("FROM tickers t", live)])
    out = store.for_screen(db, 1, N)
    assert db.params("SELECT group_id FROM screens") == [{"sid": 1}]
    assert [t["id"] for t in out] == [20, 24, 21]                        # по started_at
    assert out[0] == {"id": 20, "message": "Старая", "color": "#ffd34d", "speed": "medium", "until_at": None}
    assert out[2]["until_at"] == "2026-11-02T14:45:00"                   # ISO без пояса
    assert set(out[2]) == {"id", "message", "color", "speed", "until_at"}


def test_for_screen_drops_expired_even_if_db_returned_it():
    live = [TK(id=1, until_at=N - timedelta(seconds=1)), TK(id=2, until_at=N), TK(id=3, until_at=N + timedelta(seconds=1))]
    out = store.for_screen(RoutedDB([("FROM tickers t", live)]), 1, N)
    assert [t["id"] for t in out] == [3]


def test_for_screen_unknown_screen_sees_only_all_targets():
    live = [TK(id=1, target="all"), TK(id=2, target="groups", group_ids=[7])]
    out = store.for_screen(RoutedDB([("FROM tickers t", live)]), 99, N)
    assert [t["id"] for t in out] == [1]


def test_targets_of_resolves_at_this_moment():
    screens = [Row(id=1, group_id=7), Row(id=2, group_id=7), Row(id=3, group_id=8), Row(id=4, group_id=None)]
    for ticker, want in [(TK(target="all"), {1, 2, 3, 4}),
                         (TK(target="groups", group_ids=[7, 99]), {1, 2}),
                         (TK(target="screens", screen_ids=[3, 99]), {3})]:
        db = RoutedDB([("FROM tickers t", [ticker]), ("SELECT id, group_id FROM screens", screens)])
        assert store.targets_of(db, 11) == want
        assert db.params("FROM tickers t") == [{"id": 11}]


def test_targets_of_missing_ticker_is_empty():
    assert store.targets_of(RoutedDB([("SELECT id, group_id FROM screens", [Row(id=1, group_id=None)])]), 11) == set()


# --- отчёт экрана -------------------------------------------------------------

def test_record_report_none_is_noop():
    db = RoutedDB([])
    assert store.record_report(db, 3, None) is None
    assert db.calls == []                                                # старый агент: ничего не трогаем


def test_record_report_writes_columns():
    db = RoutedDB([])
    store.record_report(db, 3, {"ids": [5], "stopped": False, "screen_off": False})
    (p,) = db.params("UPDATE screens")
    assert p == {"sid": 3, "ids": [5], "stopped": False, "off": False}
    sql = db.sqls("UPDATE screens")[0]
    assert "ticker_ids" in sql and "ticker_stopped" in sql and "ticker_screen_off" in sql
    assert "WHERE id = :sid" in sql
    assert db.commits == 0


def test_record_report_empty_and_flags():
    db = RoutedDB([])
    store.record_report(db, 3, {"ids": [], "stopped": True, "screen_off": True})
    assert db.params("UPDATE screens")[0] == {"sid": 3, "ids": [], "stopped": True, "off": True}


# --- команды старым агентам ---------------------------------------------------

SCREENS = [Row(id=1, group_id=7), Row(id=2, group_id=7), Row(id=3, group_id=None)]


def notify_db(live):
    return RoutedDB([("FROM tickers t", live), ("SELECT id, group_id FROM screens", SCREENS)])


def test_notify_show_new_targets():
    db = notify_db([TK(id=11, message="Касса", color="#ff6b5e", speed="fast", until_at=N + timedelta(minutes=15))])
    assert store.notify_screens(db, set(), {1, 2}, N) == [1, 2]
    payload = {"message": "Касса", "color": "#ff6b5e", "speed": "fast", "duration": 900}
    assert commands(db) == [(1, "ticker_show", payload), (2, "ticker_show", payload)]
    assert db.commits == 0


def test_notify_until_cancel_is_duration_zero():
    db = notify_db([TK(until_at=None)])
    store.notify_screens(db, set(), {1}, N)
    assert commands(db)[0][2]["duration"] == 0


def test_notify_sub_second_remainder_is_not_forever():
    """0 секунд для старого агента — «бессрочно»; остаток меньше секунды округляется вверх."""
    db = notify_db([TK(until_at=N + timedelta(milliseconds=400))])
    store.notify_screens(db, set(), {1}, N)
    assert commands(db)[0][2]["duration"] == 1


def test_notify_sends_the_newest_live_ticker_of_the_screen():
    live = [TK(id=11, message="Старая", started_at=N - timedelta(minutes=9)),
            TK(id=12, message="Новая", started_at=N - timedelta(minutes=1)),
            TK(id=13, message="Просроченная", started_at=N, until_at=N)]
    db = notify_db(live)
    store.notify_screens(db, set(), {1}, N)
    assert [c[2]["message"] for c in commands(db)] == ["Новая"]


def test_notify_resends_newest_remaining_instead_of_hide():
    """Сняли одну из двух строк: экран из before − after получает оставшуюся, а не ticker_hide."""
    live = [TK(id=12, message="Осталась", target="screens", screen_ids=[1], started_at=N - timedelta(minutes=9)),
            TK(id=13, message="Новее, но чужая", target="screens", screen_ids=[2], started_at=N - timedelta(minutes=1))]
    db = notify_db(live)
    assert store.notify_screens(db, {1}, set(), N) == [1]
    assert commands(db) == [(1, "ticker_show", {"message": "Осталась", "color": "#ffd34d", "speed": "medium", "duration": 0})]


def test_notify_hide_when_nothing_left():
    db = notify_db([])
    assert store.notify_screens(db, {1, 3}, set(), N) == [1, 3]
    assert commands(db) == [(1, "ticker_hide", {}), (3, "ticker_hide", {})]


def test_notify_group_ticker_reaches_group_members_only():
    db = notify_db([TK(target="groups", group_ids=[7])])
    store.notify_screens(db, {1, 3}, set(), N)
    assert [(c[0], c[1]) for c in commands(db)] == [(1, "ticker_show"), (3, "ticker_hide")]


def test_notify_each_screen_once_in_order():
    db = notify_db([TK()])
    assert store.notify_screens(db, {2, 1}, {2, 3}, N) == [1, 2, 3]
    assert [c[0] for c in commands(db)] == [1, 2, 3]


def test_notify_skips_deleted_screens():
    db = notify_db([TK()])
    assert store.notify_screens(db, {9}, {1}, N) == [1]
    assert [c[0] for c in commands(db)] == [1]


def test_notify_nothing_affected_touches_nothing():
    db = notify_db([TK()])
    assert store.notify_screens(db, set(), set(), N) == []
    assert db.calls == []


# --- шаблоны ------------------------------------------------------------------

TPL = {"message": "Перерыв", "color": "#ff6b5e", "speed": "slow", "term_kind": "minutes", "term_value": 15}


def test_templates_list_shape():
    row = Row(id=4, message="Перерыв", color="#ff6b5e", speed="slow", term_kind="hour", term_value=18)
    db = RoutedDB([("FROM ticker_templates", [row])])
    assert store.list_templates(db) == [{"id": 4, "message": "Перерыв", "color": "#ff6b5e", "speed": "slow",
                                         "term_kind": "hour", "term_value": 18}]
    assert "ORDER BY" in db.sqls("FROM ticker_templates")[0]


def test_templates_crud_and_lookup_errors():
    db = RoutedDB([("INSERT INTO ticker_templates", [Row(id=4)]), ("UPDATE ticker_templates", [Row(id=4)]),
                   ("DELETE FROM ticker_templates", [Row(id=4)])])
    assert store.create_template(db, TPL, "admin") == 4
    assert db.params("INSERT INTO ticker_templates") == [{**{"msg": "Перерыв", "color": "#ff6b5e", "speed": "slow",
                                                             "kind": "minutes", "value": 15}, "who": "admin"}]
    assert store.update_template(db, 4, {**TPL, "term_kind": "cancel", "term_value": None}) is None
    (u,) = db.params("UPDATE ticker_templates")
    assert (u["id"], u["kind"], u["value"]) == (4, "cancel", None)
    assert store.delete_template(db, 4) is None
    assert db.params("DELETE FROM ticker_templates") == [{"id": 4}]
    assert db.commits == 0

    empty = RoutedDB([])                                                 # шаблона нет
    with pytest.raises(LookupError, match="Шаблон не найден"):
        store.update_template(empty, 9, TPL)
    with pytest.raises(LookupError, match="Шаблон не найден"):
        store.delete_template(empty, 9)


# --- миграция 034 (текст файла) -----------------------------------------------

def _migration():
    with open(os.path.join(MIGRATIONS, "034_ticker_redesign.sql"), encoding="utf-8") as f:
        return f.read()


def test_migration_034_is_idempotent_ddl():
    sql = _migration()
    for col in ("until_at TIMESTAMP", "target VARCHAR(8) NOT NULL DEFAULT 'all'", "started_at TIMESTAMP",
                "started_by VARCHAR(64)", "changed_at TIMESTAMP", "changed_by VARCHAR(64)"):
        assert f"ALTER TABLE tickers ADD COLUMN IF NOT EXISTS {col}" in sql, col
    for col in ("ticker_ids INTEGER[]", "ticker_stopped BOOLEAN", "ticker_screen_off BOOLEAN"):
        assert f"ALTER TABLE screens ADD COLUMN IF NOT EXISTS {col}" in sql, col
    assert "CREATE TABLE IF NOT EXISTS ticker_groups" in sql
    assert "CREATE TABLE IF NOT EXISTS ticker_templates" in sql
    assert re.search(r"CHECK \(term_kind IN \('cancel', ?'minutes', ?'hour'\)\)", sql)


def test_migration_034_converts_old_utc_times_to_msk_and_drops_dead_rows():
    sql = _migration()
    assert "(expires_at AT TIME ZONE 'UTC') AT TIME ZONE 'Europe/Moscow'" in sql
    assert "(created_at AT TIME ZONE 'UTC') AT TIME ZONE 'Europe/Moscow'" in sql
    assert re.search(r"DELETE FROM tickers\s+WHERE[^;]*is_active = FALSE[^;]*expires_at <= NOW\(\)", sql)
    assert "CASE WHEN is_all THEN 'all' ELSE 'screens' END" in sql and "started_by = created_by" in sql
    update = sql[sql.index("UPDATE tickers"):]
    assert "WHERE started_at IS NULL" in update                          # повторный запуск не трогает перенесённое


def test_migration_034_seeds_three_templates_only_into_an_empty_table():
    sql = _migration()
    i = sql.index("INSERT INTO ticker_templates")
    stmt = sql[i:sql.index(";", i)]
    assert "WHERE NOT EXISTS (SELECT 1 FROM ticker_templates)" in stmt
    for text_ in ("Магазин закрывается через 15 минут. Спасибо за покупки!",
                  "Технический перерыв. Приносим извинения за неудобства",
                  "Касса временно не работает. Пожалуйста, пройдите к соседней кассе"):
        assert text_ in stmt
    for color in ("#ffd34d", "#ff6b5e", "#ffffff"):
        assert color in stmt
