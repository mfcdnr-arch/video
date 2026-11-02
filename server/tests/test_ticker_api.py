"""API бегущей строки (routers/ticker.py) и Celery-задача expire_tickers.

Эндпоинты зовутся напрямую на подменной сессии RoutedDB (из test_broadcast): настоящего Postgres
нет, SQL хранилища гоняет стенд (smoke_test.sh). Проверяется то, что легко сломать незаметно:
обзор считает «бежит на X из N» по отчёту экранов, а не по расчёту сервера; изменение, снятие
и срок доходят до экранов, которых строка касалась ДО правки, и до тех, кого она касается ПОСЛЕ;
commit один и раньше WS-будильника; старое тело запроса работает; ошибки — по-русски.
"""
import asyncio
import json
import re
from datetime import datetime, timedelta
from types import SimpleNamespace as Row

import pytest
import sqlalchemy
import sqlalchemy.orm
from fastapi import HTTPException

import placements_store
import routers.screens as screens_router
import routers.ticker as api
import tasks
import ticker_store
from test_broadcast import RoutedDB
from ticker_rules import REASON_TEXT

N = datetime(2026, 11, 2, 14, 30)
ADMIN = {"username": "admin", "role": "admin"}
CYRILLIC = re.compile("[А-Яа-яЁё]")


def scr(id, name, online=True, version="1.4.0", ids=None, group_id=None, stopped=False, off=False):
    """Экран как его отдаёт ticker_store.screens_brief."""
    return {"id": id, "name": name, "group_id": group_id, "group_name": None, "online": online,
            "offline_for_seconds": 5 if online else 3600, "agent_version": version,
            "ticker_ids": ids, "ticker_stopped": stopped, "ticker_screen_off": off}


def tk(id=11, message="Касса закрыта", target="all", group_ids=None, screen_ids=None, until_at=None,
       started_at=N - timedelta(minutes=5), changed_at=None, started_by="admin", changed_by=None):
    """Строка как её отдаёт ticker_store.list_live."""
    return {"id": id, "message": message, "color": "#ffd34d", "speed": "medium", "target": target,
            "group_ids": group_ids or [], "screen_ids": screen_ids or [], "until_at": until_at,
            "started_at": started_at, "started_by": started_by, "changed_at": changed_at, "changed_by": changed_by}


def TK(**kw):
    """То же, но «строка БД» — с доступом по атрибутам, как в test_ticker_store."""
    return Row(**tk(**kw))


def call(fn, *args, **kwargs):
    """Вызвать async-эндпоинт и дать задачам WS-будильника отработать до закрытия цикла."""
    async def main():
        out = await fn(*args, **kwargs)
        await asyncio.sleep(0)
        return out
    return asyncio.run(main())


class FakeWS:
    """ws_manager: кто подключён и кому ушёл будильник (и сколько commit было к этому моменту)."""

    def __init__(self, connected=(), db=None):
        self.connected = list(connected)
        self.db = db
        self.sent = []

    def connected_screens(self):
        return list(self.connected)

    async def send(self, screen_id, message):
        self.sent.append((screen_id, message, self.db.commits if self.db else None))
        return True


@pytest.fixture
def clock(monkeypatch):
    monkeypatch.setattr(api, "now_msk", lambda: N)


def commands(db):
    """Поставленные команды: [(экран, тип, payload)]. Тип — параметр :t или, как в /command/stop и /command/resume,
    литерал в самом SQL."""
    out = []
    for sql, p in db.calls:
        if "INSERT INTO commands" in sql:
            cmd_type = p["t"] if "t" in p else re.search(r"VALUES \(:sid, '(\w+)'", sql).group(1)
            out.append((p["sid"], cmd_type, json.loads(p["p"])))
    return out


def journal(db):
    return [(p["t"], p["d"], p["a"]) for p in db.params("INSERT INTO audit_log")]


def http_error(fn, *args, **kwargs):
    with pytest.raises(HTTPException) as e:
        call(fn, *args, **kwargs) if asyncio.iscoroutinefunction(fn) else fn(*args, **kwargs)
    return e.value


# --- обзор ----------------------------------------------------------------------

def test_overview_counts_and_reasons():
    screens = [scr(1, "Зал", ids=[11]), scr(2, "Касса", online=False), scr(3, "Склад", version="1.3.0")]
    out = api.overview_json([tk(id=11, target="all")], screens, [], [], N)
    (t,) = out["tickers"]
    assert t["screens_total"] == 3
    assert t["confirm"] == {"total": 3, "running": 1, "problems": [
        {"screen": "Касса", "reason": "offline", "text": REASON_TEXT["offline"]},
        {"screen": "Склад", "reason": "old_agent", "text": REASON_TEXT["old_agent"]}]}


def test_overview_other_ids_do_not_confirm():
    # экран в сети, но бежит другая строка: «бежит» только если экран сам назвал id этой
    out = api.overview_json([tk(id=11, started_at=N - timedelta(minutes=5))], [scr(1, "Зал", ids=[12])], [], [], N)
    assert out["tickers"][0]["confirm"]["problems"][0]["reason"] == "not_confirmed"


@pytest.mark.parametrize("started, changed, want", [
    (N - timedelta(seconds=30), None, "pending"),                      # правки не было: считаем от запуска
    (N - timedelta(minutes=10), None, "not_confirmed"),
    (N - timedelta(minutes=10), N - timedelta(seconds=30), "pending"),  # правка свежее запуска
    (N - timedelta(seconds=30), N - timedelta(minutes=10), "not_confirmed"),  # changed_at главнее
    (None, None, "not_confirmed"),                                      # перенесена миграцией без дат: не «ждём» вечно
])
def test_overview_edit_moment_falls_back(started, changed, want):
    row = tk(started_at=started, changed_at=changed)
    out = api.overview_json([row], [scr(1, "Зал", ids=[])], [], [], N)
    assert out["tickers"][0]["confirm"]["problems"][0]["reason"] == want


def test_overview_ticker_without_screens():
    # строка на удалённую группу: «на 0 экранах», а не ошибка
    out = api.overview_json([tk(target="groups", group_ids=[99])], [scr(1, "Зал", group_id=7)], [], [], N)
    (t,) = out["tickers"]
    assert t["screens_total"] == 0
    assert t["confirm"] == {"total": 0, "running": 0, "problems": []}
    assert out["screens"][0]["live_count"] == 0


def test_overview_live_count_per_screen():
    screens = [scr(1, "Зал", group_id=7), scr(2, "Касса", group_id=8), scr(3, "Склад")]
    live = [tk(id=1, target="screens", screen_ids=[1]), tk(id=2, target="screens", screen_ids=[1]),
            tk(id=3, target="groups", group_ids=[7]), tk(id=4, target="all"),
            tk(id=5, target="screens", screen_ids=[2])]
    out = api.overview_json(live, screens, [], [], N)
    assert {s["id"]: s["live_count"] for s in out["screens"]} == {1: 4, 2: 2, 3: 1}


def test_overview_skips_expired_rows():
    live = [tk(id=1, until_at=N), tk(id=2, until_at=N - timedelta(seconds=1)), tk(id=3, until_at=N + timedelta(seconds=1))]
    out = api.overview_json(live, [scr(1, "Зал")], [], [], N)
    assert [t["id"] for t in out["tickers"]] == [3]
    assert out["screens"][0]["live_count"] == 1


def test_overview_keeps_launch_order():
    live = [tk(id=7, started_at=N - timedelta(minutes=9)), tk(id=5, started_at=N - timedelta(minutes=1))]
    assert [t["id"] for t in api.overview_json(live, [], [], [], N)["tickers"]] == [7, 5]


def test_overview_screen_fields_pass_through():
    screens = [scr(1, "Зал", ids=[11], group_id=7)]
    out = api.overview_json([], screens, [{"id": 7, "name": "Зал 1", "screen_count": 1}], [], N)
    assert out["screens"] == [{**screens[0], "live_count": 0}]
    assert out["groups"] == [{"id": 7, "name": "Зал 1", "screen_count": 1}]


def test_overview_generated_at_and_templates():
    templates = [{"id": 3, "message": "Перерыв", "color": "#ff6b5e", "speed": "fast", "term_kind": "hour", "term_value": 18},
                 {"id": 4, "message": "Скоро закроемся", "color": "#ffd34d", "speed": "medium", "term_kind": "cancel", "term_value": None}]
    out = api.overview_json([], [], [], templates, N)
    assert out["generated_at"] == "2026-11-02T14:30:00"
    assert out["templates"] == [
        {"id": 3, "message": "Перерыв", "color": "#ff6b5e", "speed": "fast", "term": {"kind": "hour", "hour": 18}},
        {"id": 4, "message": "Скоро закроемся", "color": "#ffd34d", "speed": "medium", "term": {"kind": "cancel"}}]


def test_overview_empty():
    assert api.overview_json([], [], [], [], N) == {"generated_at": "2026-11-02T14:30:00", "tickers": [],
                                                    "screens": [], "groups": [], "templates": []}


# --- строка в JSON --------------------------------------------------------------

def test_remaining_seconds():
    assert api.ticker_json(tk(until_at=N + timedelta(minutes=15)), N)["remaining_seconds"] == 900
    assert api.ticker_json(tk(until_at=None), N)["remaining_seconds"] is None            # «до снятия»
    assert api.ticker_json(tk(until_at=N + timedelta(milliseconds=200)), N)["remaining_seconds"] == 1   # вверх, не в ноль
    assert api.ticker_json(tk(until_at=N - timedelta(seconds=5)), N)["remaining_seconds"] == 0


def test_ticker_json_shape_is_json_safe():
    row = tk(until_at=N + timedelta(minutes=15), changed_at=N - timedelta(minutes=1), changed_by="oper",
             target="groups", group_ids=[7])
    out = api.ticker_json(row, N)
    assert out == {"id": 11, "message": "Касса закрыта", "color": "#ffd34d", "speed": "medium", "target": "groups",
                   "is_all": False, "group_ids": [7], "screen_ids": [], "until_at": "2026-11-02T14:45:00",
                   "started_at": "2026-11-02T14:25:00", "started_by": "admin", "changed_at": "2026-11-02T14:29:00",
                   "changed_by": "oper", "remaining_seconds": 900}
    json.dumps(out)
    assert api.ticker_json(tk(started_at=None, changed_at=None), N)["started_at"] is None


def test_overview_ticker_row_has_everything_the_panel_needs():
    out = api.overview_json([tk(until_at=N + timedelta(minutes=15))], [scr(1, "Зал", ids=[11])], [], [], N)
    (t,) = out["tickers"]
    for key in ("id", "message", "color", "speed", "target", "group_ids", "screen_ids", "until_at", "started_at",
                "started_by", "changed_at", "changed_by", "screens_total", "confirm", "remaining_seconds"):
        assert key in t
    json.dumps(out)


# --- GET -----------------------------------------------------------------------

def test_get_tickers_stays_a_list():
    rows = [TK(id=1, started_at=N - timedelta(minutes=9)),
            TK(id=2, target="screens", screen_ids=[4], until_at=N + timedelta(minutes=5))]
    db = RoutedDB([("FROM tickers t", rows)])
    out = _get_tickers(db)
    assert isinstance(out, list)
    for t in out:
        assert {"id", "message", "color", "speed", "is_all"} <= set(t)
    assert [t["id"] for t in out] == [2, 1]                       # новая сверху, как было
    assert (out[0]["is_all"], out[1]["is_all"]) == (False, True)
    assert out[0]["target"] == "screens" and out[0]["until_at"] == "2026-11-02T14:35:00"
    assert out[1]["until_at"] is None
    assert {"started_at", "started_by"} <= set(out[0])
    json.dumps(out)


def _get_tickers(db):
    return api.get_tickers(db=db, current_admin=ADMIN)


def test_get_tickers_asks_for_live_rows_by_python_now(clock):
    db = RoutedDB([("FROM tickers t", [])])
    _get_tickers(db)
    (p,) = db.params("FROM tickers t")
    assert p == {"now": N}


def test_get_overview_assembles_store_reads(clock):
    db = RoutedDB([
        ("ORDER BY t.started_at", [TK(id=11)]),
        ("FROM sync_groups g", [Row(id=7, name="Зал 1", screen_count=1)]),
        ("FROM screens s LEFT JOIN", [Row(id=1, name="Зал", group_id=None, group_name=None, online=True, offline_for_seconds=3,
                                          agent_version="1.4.0", ticker_ids=[11], ticker_stopped=False, ticker_screen_off=False)]),
        ("FROM ticker_templates", [Row(id=3, message="Перерыв", color="#ff6b5e", speed="fast", term_kind="cancel", term_value=None)]),
    ])
    out = api.get_overview(db=db, current_admin=ADMIN)
    assert out["generated_at"] == "2026-11-02T14:30:00"
    assert out["tickers"][0]["confirm"]["running"] == 1
    assert out["screens"][0]["live_count"] == 1
    assert out["groups"] == [{"id": 7, "name": "Зал 1", "screen_count": 1}]
    assert out["templates"][0]["term"] == {"kind": "cancel"}
    assert db.commits == 0


# --- POST -----------------------------------------------------------------------

def post_db(live=None, screens=((1, None), (2, None), (3, None))):
    return RoutedDB([
        ("INSERT INTO tickers", [Row(id=11)]),
        ("FROM screens WHERE id = ANY", lambda p: [Row(id=i) for i in p["ids"] if i in (1, 2, 3)]),
        ("FROM sync_groups WHERE id = ANY", lambda p: [Row(id=i) for i in p["ids"] if i == 7]),
        ("WHERE t.id = :id", live or [TK(id=11, target="screens", screen_ids=[1, 2], until_at=N + timedelta(minutes=15),
                                          started_at=N)]),
        ("ORDER BY t.started_at", live or [TK(id=11, target="screens", screen_ids=[1, 2], until_at=N + timedelta(minutes=15),
                                               started_at=N)]),
        ("SELECT id, group_id FROM screens", [Row(id=i, group_id=g) for i, g in screens]),
    ])


def test_post_creates_notifies_commits_then_wakes_connected_screens(clock, monkeypatch):
    db = post_db()
    ws = FakeWS(connected=[2, 9], db=db)
    monkeypatch.setattr(api, "ws_manager", ws)
    body = {"message": "Касса закрыта", "target": "screens", "screen_ids": [1, 2], "term": {"kind": "minutes", "minutes": 15}}
    out = call(api.create_ticker, body=body, db=db, current_admin=ADMIN)
    assert out == {"id": 11, "status": "ok"}
    (p,) = db.params("INSERT INTO tickers")
    assert (p["now"], p["who"], p["until"]) == (N, "admin", N + timedelta(minutes=15))
    assert commands(db) == [(1, "ticker_show", {"message": "Касса закрыта", "color": "#ffd34d", "speed": "medium", "duration": 900}),
                            (2, "ticker_show", {"message": "Касса закрыта", "color": "#ffd34d", "speed": "medium", "duration": 900})]
    assert journal(db) == [("Бегущая строка запущена", "Касса закрыта", "admin")]
    assert db.commits == 1
    assert ws.sent == [(2, {"type": "new_command", "count": 1}, 1)]       # только подключённому и после commit


def test_post_old_body_is_still_understood(clock, monkeypatch):
    db = post_db()
    monkeypatch.setattr(api, "ws_manager", FakeWS())
    body = {"message": "Старое тело", "is_all": False, "screen_ids": [1], "duration": 60}
    assert call(api.create_ticker, body=body, db=db, current_admin=ADMIN) == {"id": 11, "status": "ok"}
    (p,) = db.params("INSERT INTO tickers")
    assert p["until"] == N + timedelta(seconds=60) and p["target"] == "screens" and p["is_all"] is False
    assert db.params("INSERT INTO ticker_screens") == [{"tid": 11, "sid": 1}]


def test_post_without_term_is_until_cancel(clock, monkeypatch):
    db = post_db()
    monkeypatch.setattr(api, "ws_manager", FakeWS())
    call(api.create_ticker, body={"message": "x"}, db=db, current_admin=ADMIN)
    (p,) = db.params("INSERT INTO tickers")
    assert p["until"] is None and p["target"] == "all"


@pytest.mark.parametrize("body", [
    {"message": ""},
    {"message": "x" * 201},
    {"message": "x", "term": {"kind": "minutes", "minutes": 0}},
    {"message": "x", "term": {"kind": "hour", "hour": 10}},            # в 14:30 час 10 уже в прошлом
    {"message": "x", "target": "groups", "group_ids": []},
    ["не", "объект"],
])
def test_post_bad_body_400_russian(clock, body):
    db = post_db()
    err = http_error(api.create_ticker, body=body, db=db, current_admin=ADMIN)
    assert err.status_code == 400 and CYRILLIC.search(err.detail)
    assert not db.calls and db.commits == 0                             # ничего не записано


def test_post_vanished_screen_is_400_not_500(clock, monkeypatch):
    db = RoutedDB([("FROM screens WHERE id = ANY", [Row(id=1)])])
    monkeypatch.setattr(api, "ws_manager", FakeWS())
    body = {"message": "x", "target": "screens", "screen_ids": [1, 99]}
    err = http_error(api.create_ticker, body=body, db=db, current_admin=ADMIN)
    assert err.status_code == 400 and "обновите страницу" in err.detail
    assert not db.params("INSERT INTO tickers") and db.commits == 0


# --- PUT ------------------------------------------------------------------------

def put_db(before, after, exist=(2, 3)):
    seq = iter([[before], [after]])
    return RoutedDB([
        ("FOR UPDATE", [Row(id=11, until_at=None)]),
        ("FROM screens WHERE id = ANY", [Row(id=i) for i in exist]),
        ("WHERE t.id = :id", lambda p: next(seq)),
        ("ORDER BY t.started_at", [after]),
        ("SELECT id, group_id FROM screens", [Row(id=i, group_id=None) for i in (1, 2, 3)]),
    ])


def test_put_tells_screens_before_and_after_the_change(clock, monkeypatch):
    before = TK(target="screens", screen_ids=[1, 2])
    after = TK(target="screens", screen_ids=[2, 3], message="Новый текст")
    db = put_db(before, after)
    ws = FakeWS(connected=[1, 3], db=db)
    monkeypatch.setattr(api, "ws_manager", ws)
    body = {"message": "Новый текст", "target": "screens", "screen_ids": [2, 3]}
    assert call(api.update_ticker, 11, body=body, db=db, current_admin=ADMIN) == {"status": "ok"}
    got = commands(db)
    assert [(sid, kind) for sid, kind, _ in got] == [(1, "ticker_hide"), (2, "ticker_show"), (3, "ticker_show")]
    assert got[1][2]["message"] == "Новый текст"
    assert journal(db) == [("Бегущая строка изменена", "Новый текст", "admin")]
    assert db.commits == 1
    assert [(sid, c) for sid, _, c in ws.sent] == [(1, 1), (3, 1)]


def test_put_without_term_keeps_the_old_one(clock, monkeypatch):
    db = put_db(TK(), TK())
    monkeypatch.setattr(api, "ws_manager", FakeWS())
    call(api.update_ticker, 11, body={"message": "Правка", "term": None}, db=db, current_admin=ADMIN)
    (p,) = db.params("UPDATE tickers")
    assert p["until"] is None and "until_at = :until" in db.sqls("UPDATE tickers")[0]
    # прежний срок строки в БД (row.until_at) — а не «до снятия» из тела
    db = RoutedDB([("FOR UPDATE", [Row(id=11, until_at=N + timedelta(minutes=7))]),
                   ("WHERE t.id = :id", [TK()]), ("ORDER BY t.started_at", [TK()]),
                   ("SELECT id, group_id FROM screens", [Row(id=1, group_id=None)])])
    call(api.update_ticker, 11, body={"message": "Правка"}, db=db, current_admin=ADMIN)
    assert db.params("UPDATE tickers")[0]["until"] == N + timedelta(minutes=7)


def test_update_missing_ticker_404(clock):
    db = RoutedDB([])
    err = http_error(api.update_ticker, 99, body={"message": "x"}, db=db, current_admin=ADMIN)
    assert err.status_code == 404
    assert err.detail == "Бегущая строка не найдена"
    assert db.commits == 0 and not db.params("INSERT INTO commands")


def test_update_expired_ticker_404(clock):
    db = RoutedDB([("FOR UPDATE", [Row(id=11, until_at=N)])])           # срок вышел в этот самый момент
    err = http_error(api.update_ticker, 11, body={"message": "x"}, db=db, current_admin=ADMIN)
    assert err.status_code == 404 and err.detail == "Бегущая строка не найдена"


def test_put_bad_body_and_vanished_group_are_400(clock):
    db = put_db(TK(), TK())
    err = http_error(api.update_ticker, 11, body={"message": ""}, db=db, current_admin=ADMIN)
    assert err.status_code == 400 and CYRILLIC.search(err.detail)
    db = RoutedDB([("FOR UPDATE", [Row(id=11, until_at=None)]), ("FROM sync_groups WHERE id = ANY", [])])
    err = http_error(api.update_ticker, 11, body={"message": "x", "target": "groups", "group_ids": [99]},
                     db=db, current_admin=ADMIN)
    assert err.status_code == 400 and "обновите страницу" in err.detail and db.commits == 0


# --- DELETE ---------------------------------------------------------------------

def test_delete_hides_the_ticker_on_its_screens(clock, monkeypatch):
    db = RoutedDB([("WHERE t.id = :id", [TK(target="screens", screen_ids=[1, 2])]),
                   ("DELETE FROM tickers", [Row(message="Касса закрыта")]),
                   ("SELECT id, group_id FROM screens", [Row(id=1, group_id=None), Row(id=2, group_id=None)])])
    ws = FakeWS(connected=[2], db=db)
    monkeypatch.setattr(api, "ws_manager", ws)
    assert call(api.delete_ticker, 11, db=db, current_admin=ADMIN) == {"status": "ok"}
    assert commands(db) == [(1, "ticker_hide", {}), (2, "ticker_hide", {})]
    assert journal(db) == [("Бегущая строка снята", "Касса закрыта", "admin")]
    assert db.commits == 1
    assert ws.sent == [(2, {"type": "new_command", "count": 1}, 1)]


def test_delete_keeps_other_tickers_running_on_old_agents(clock, monkeypatch):
    other = TK(id=12, message="Другая", started_at=N - timedelta(minutes=2))
    db = RoutedDB([("WHERE t.id = :id", [TK(target="screens", screen_ids=[1])]),
                   ("DELETE FROM tickers", [Row(message="Касса закрыта")]),
                   ("ORDER BY t.started_at", [other]),
                   ("SELECT id, group_id FROM screens", [Row(id=1, group_id=None)])])
    monkeypatch.setattr(api, "ws_manager", FakeWS())
    call(api.delete_ticker, 11, db=db, current_admin=ADMIN)
    assert [(sid, kind) for sid, kind, _ in commands(db)] == [(1, "ticker_show")]
    assert commands(db)[0][2]["message"] == "Другая"


def test_delete_missing_ticker_404(clock):
    db = RoutedDB([])
    err = http_error(api.delete_ticker, 99, db=db, current_admin=ADMIN)
    assert err.status_code == 404
    assert err.detail == "Бегущая строка не найдена"
    assert db.commits == 0 and not journal(db)


def test_ws_wake_up_comes_only_after_commit(clock, monkeypatch):
    # если commit упадёт, экраны не должны получить будильник про команду, которой нет
    seen = []
    monkeypatch.setattr(api, "_wake_agents", lambda ids: seen.append((sorted(ids), db.commits)))
    db = post_db()
    call(api.create_ticker, body={"message": "x", "target": "screens", "screen_ids": [1, 2]}, db=db, current_admin=ADMIN)
    db = put_db(TK(target="screens", screen_ids=[1, 2]), TK(target="screens", screen_ids=[2, 3]))
    call(api.update_ticker, 11, body={"message": "x", "target": "screens", "screen_ids": [2, 3]}, db=db, current_admin=ADMIN)
    db = RoutedDB([("WHERE t.id = :id", [TK(target="screens", screen_ids=[1])]), ("DELETE FROM tickers", [Row(message="x")]),
                   ("SELECT id, group_id FROM screens", [Row(id=1, group_id=None)])])
    call(api.delete_ticker, 11, db=db, current_admin=ADMIN)
    assert seen == [([1, 2], 1), ([1, 2, 3], 1), ([1], 1)]


def test_failed_commit_sends_no_ws_wake_up(clock, monkeypatch):
    ws = FakeWS(connected=[1, 2])
    monkeypatch.setattr(api, "ws_manager", ws)
    db = post_db()

    def broken_commit():
        raise RuntimeError("commit failed")
    db.commit = broken_commit
    with pytest.raises(RuntimeError):
        call(api.create_ticker, body={"message": "x", "target": "screens", "screen_ids": [1, 2]}, db=db, current_admin=ADMIN)
    assert ws.sent == []


# --- шаблоны --------------------------------------------------------------------

def test_get_templates_term_is_nested():
    rows = [Row(id=1, message="А", color="#ffd34d", speed="medium", term_kind="minutes", term_value=15),
            Row(id=2, message="Б", color="#ff6b5e", speed="slow", term_kind="cancel", term_value=None),
            Row(id=3, message="В", color="#ffffff", speed="fast", term_kind="hour", term_value=18)]
    out = api.get_templates(db=RoutedDB([("FROM ticker_templates", rows)]), current_admin=ADMIN)
    assert [t["term"] for t in out] == [{"kind": "minutes", "minutes": 15}, {"kind": "cancel"}, {"kind": "hour", "hour": 18}]
    assert set(out[0]) == {"id", "message", "color", "speed", "term"}


def test_post_template_stores_kind_and_value():
    db = RoutedDB([("INSERT INTO ticker_templates", [Row(id=5)])])
    body = {"message": " Перерыв ", "color": "#FF6B5E", "speed": "fast", "term": {"kind": "minutes", "minutes": 30}}
    assert api.create_template(body=body, db=db, current_admin=ADMIN) == {"id": 5, "status": "ok"}
    (p,) = db.params("INSERT INTO ticker_templates")
    assert (p["msg"], p["color"], p["speed"], p["kind"], p["value"], p["who"]) == ("Перерыв", "#ff6b5e", "fast", "minutes", 30, "admin")
    assert db.commits == 1


def test_post_template_hour_is_not_checked_against_the_clock(clock):
    db = RoutedDB([("INSERT INTO ticker_templates", [Row(id=5)])])
    api.create_template(body={"message": "До шести", "term": {"kind": "hour", "hour": 1}}, db=db, current_admin=ADMIN)
    assert (db.params("INSERT INTO ticker_templates")[0]["kind"], db.params("INSERT INTO ticker_templates")[0]["value"]) == ("hour", 1)


@pytest.mark.parametrize("body", [
    {"message": ""},
    {"message": "x", "color": "red"},
    {"message": "x", "term": {"kind": "minutes", "minutes": 0}},
    {"message": "x", "term": {"kind": "hour"}},
    [],
])
def test_template_bad_body_400_russian(body):
    db = RoutedDB([])
    for fn, args in ((api.create_template, ()), (api.update_template, (5,))):
        err = http_error(fn, *args, body=body, db=db, current_admin=ADMIN)
        assert err.status_code == 400 and CYRILLIC.search(err.detail)
    assert not db.calls and db.commits == 0


def test_put_template_rewrites_and_commits():
    db = RoutedDB([("UPDATE ticker_templates", [Row(id=5)])])
    body = {"message": "Новый", "term": {"kind": "cancel"}}
    assert api.update_template(5, body=body, db=db, current_admin=ADMIN) == {"status": "ok"}
    (p,) = db.params("UPDATE ticker_templates")
    assert (p["id"], p["msg"], p["kind"], p["value"]) == (5, "Новый", "cancel", None)
    assert db.commits == 1


def test_missing_template_404():
    db = RoutedDB([])
    for fn, args in ((api.update_template, (5,)), (api.delete_template, (5,))):
        kwargs = {"body": {"message": "x"}} if fn is api.update_template else {}
        err = http_error(fn, *args, db=db, current_admin=ADMIN, **kwargs)
        assert err.status_code == 404 and err.detail == "Шаблон не найден"
    assert db.commits == 0


def test_delete_template_commits():
    db = RoutedDB([("DELETE FROM ticker_templates", [Row(id=5)])])
    assert api.delete_template(5, db=db, current_admin=ADMIN) == {"status": "ok"}
    assert db.commits == 1


# --- лента экрана, heartbeat, «Стоп» и «Показ» (routers/screens.py) ---------------
# Бегущую строку больше не снимает ни «Стоп», ни смена плейлиста: она живёт до срока, а экран узнаёт
# о ней из ленты (агент 1.4.0) или командами (старый агент). Сбой строк не должен ронять ни ленту,
# ни heartbeat — на установке новый API стартует раньше, чем migrate.sh накатит миграцию 034.

class TxDB(RoutedDB):
    """RoutedDB с поведением Postgres: после ошибки запроса падает всё, пока не сделан rollback
    (в том числе commit). commit и rollback пишутся в calls маркерами — видно, что раньше чего."""

    def __init__(self, routes, fail_on=None):
        super().__init__(routes, fail_on)
        self.aborted = False

    def execute(self, query, params=None):
        if self.aborted:
            raise RuntimeError("current transaction is aborted, commands ignored until end of transaction block")
        try:
            return super().execute(query, params)
        except RuntimeError:
            self.aborted = True
            raise

    def commit(self):
        if self.aborted:
            raise RuntimeError("Can't reconnect until invalid transaction is rolled back")
        self.calls.append(("-- COMMIT", {}))
        super().commit()

    def rollback(self):
        self.aborted = False
        self.calls.append(("-- ROLLBACK", {}))
        super().rollback()


def at(db, needle):
    """Номер первого вызова, в SQL которого есть needle (порядок запросов)."""
    return next(i for i, (sql, _) in enumerate(db.calls) if needle in sql)


@pytest.fixture
def screens_clock(monkeypatch):
    monkeypatch.setattr(screens_router, "now_msk", lambda: N)


@pytest.fixture
def spy_auto_clear(monkeypatch):
    """Прежний автоснятчик: если его кто-то ещё зовёт, попадёт в список."""
    called = []
    monkeypatch.setattr(api, "auto_clear_tickers", lambda *a, **kw: called.append(a), raising=False)
    return called


def run_feed(db):
    return screens_router.schedule_for_minipc(1, db=db, _screen=None)


def test_auto_clear_tickers_is_gone():
    assert not hasattr(api, "auto_clear_tickers")


def test_feed_has_tickers(screens_clock):
    live = [TK(id=11, message="Касса закрыта", until_at=N + timedelta(minutes=15)),
            TK(id=12, message="Технический перерыв", target="all", started_at=N - timedelta(minutes=1))]
    feed = run_feed(RoutedDB([("ORDER BY t.started_at", live)]))
    # по порядку запуска; срок — ISO без пояса (наивное МСК), «до снятия» — null
    assert feed["tickers"] == [
        {"id": 11, "message": "Касса закрыта", "color": "#ffd34d", "speed": "medium", "until_at": "2026-11-02T14:45:00"},
        {"id": 12, "message": "Технический перерыв", "color": "#ffd34d", "speed": "medium", "until_at": None}]
    json.dumps(feed["tickers"])


def test_feed_without_live_tickers_still_has_the_key(screens_clock):
    # ключ есть всегда: по нему агент 1.4.0 узнаёт, что строки берутся из ленты, и убирает снятые
    assert run_feed(RoutedDB([]))["tickers"] == []


def test_feed_tickers_follow_screen_scope(screens_clock):
    # лента экрана 1: его собственные строки и «на все», чужие — нет
    live = [TK(id=11, target="screens", screen_ids=[1]), TK(id=12, target="screens", screen_ids=[2]), TK(id=13, target="all")]
    feed = run_feed(RoutedDB([("ORDER BY t.started_at", live)]))
    assert [t["id"] for t in feed["tickers"]] == [11, 13]


def test_feed_tickers_failure_omits_the_key_not_500(screens_clock):
    # нет ключа — «ничего не менять» для агента 1.4.0; пустой список снял бы живые объявления из-за сбоя БД
    db = TxDB([("SELECT last_playlist_id", [5])], fail_on="FROM tickers")
    feed = run_feed(db)
    assert "tickers" not in feed
    assert db.rollbacks == 1 and not db.aborted
    # остальная лента на месте
    assert feed["active_source"] == "template" and feed["schedule"] == [] and feed["files"] == []
    assert feed["fallback"] == [] and feed["power"] == {"on": None, "off": None}


def test_feed_tickers_failure_keeps_the_playlist_memory(screens_clock):
    # сменившийся плейлист запомнен и закоммичен ДО чтения строк: откат при сбое строк его не отменяет
    db = TxDB([("SELECT last_playlist_id", [5])], fail_on="FROM tickers")
    run_feed(db)
    assert db.params("UPDATE screens SET last_playlist_id") == [{"p": None, "sid": 1}]
    assert at(db, "UPDATE screens SET last_playlist_id") < at(db, "-- COMMIT") < at(db, "FROM tickers t") < at(db, "-- ROLLBACK")


def test_playlist_change_does_not_touch_tickers(screens_clock, spy_auto_clear):
    # слот кончился (плейлист 5 → ничего): раньше это снимало все строки сети
    db = RoutedDB([("SELECT last_playlist_id", [5]),
                   ("ORDER BY t.started_at", [TK(id=11, target="all")])])
    feed = run_feed(db)
    assert db.params("UPDATE screens SET last_playlist_id") == [{"p": None, "sid": 1}]   # смена запомнена
    assert spy_auto_clear == []
    assert not db.sqls("DELETE FROM tickers") and not db.sqls("UPDATE tickers")
    assert not db.sqls("INSERT INTO commands") and not db.sqls("INSERT INTO audit_log")  # ни команд, ни записи в журнале
    assert [t["id"] for t in feed["tickers"]] == [11]                                    # строка в ленте как была


def stop_db(live=(), fail_on=None):
    return TxDB([("ORDER BY t.started_at", list(live))], fail_on=fail_on)


def test_stop_keeps_tickers_and_hides_on_that_screen(monkeypatch, spy_auto_clear):
    live = [TK(id=11, target="all"), TK(id=12, target="screens", screen_ids=[6])]       # на экране 5 живёт одна
    db = stop_db(live)
    ws = FakeWS(connected=[5, 9], db=db)
    monkeypatch.setattr(screens_router, "ws_manager", ws)
    monkeypatch.setattr(screens_router, "now_msk", lambda: N)
    assert call(screens_router.command_stop_screen, 5, admin=ADMIN, db=db) == {"status": "ok", "ws_sent": True}
    assert commands(db) == [(5, "stop", {}), (5, "ticker_hide", {})]      # старому агенту: гаси строку на этом экране
    assert spy_auto_clear == []
    assert not db.sqls("DELETE FROM tickers") and not db.sqls("UPDATE tickers")      # на сервере строки живы
    assert not journal(db)                                                            # в журнал ничего не пишется
    assert db.commits == 2                                                            # stop и ticker_hide
    assert ws.sent == [(5, {"type": "new_command", "count": 1}, 2)]       # будильник только этому экрану, после commit


def test_stop_without_live_tickers_queues_only_stop(monkeypatch):
    # живых строк у экрана нет: лишний ticker_hide остался бы в очереди старого агента (по WS — одна команда
    # за пробуждение) и задержал бы следующий «Показ» или ticker_show
    db = stop_db([TK(id=11, target="screens", screen_ids=[6]), TK(id=12, until_at=N - timedelta(seconds=1))])
    monkeypatch.setattr(screens_router, "ws_manager", FakeWS())
    monkeypatch.setattr(screens_router, "now_msk", lambda: N)
    call(screens_router.command_stop_screen, 5, admin=ADMIN, db=db)
    assert commands(db) == [(5, "stop", {})]
    assert db.commits == 1


def test_stop_survives_a_failing_ticker_part(monkeypatch):
    # на установке новый API стартует раньше миграции 034: чтение строк падает. «Стоп» уже закоммичен и уходит
    db = stop_db(fail_on="FROM tickers")
    ws = FakeWS(connected=[5], db=db)
    monkeypatch.setattr(screens_router, "ws_manager", ws)
    monkeypatch.setattr(screens_router, "now_msk", lambda: N)
    assert call(screens_router.command_stop_screen, 5, admin=ADMIN, db=db) == {"status": "ok", "ws_sent": True}
    assert commands(db) == [(5, "stop", {})]
    assert at(db, "INSERT INTO commands") < at(db, "-- COMMIT") < at(db, "FROM tickers t") < at(db, "-- ROLLBACK")
    assert db.commits == 1 and db.rollbacks == 1 and not db.aborted
    assert ws.sent == [(5, {"type": "new_command", "count": 1}, 1)]       # будильник всё равно, после commit


def test_resume_shows_the_newest_live_ticker_again(screens_clock, monkeypatch):
    live = [TK(id=11, message="Старая", target="all", started_at=N - timedelta(minutes=9)),
            TK(id=12, message="Новая", target="screens", screen_ids=[5], until_at=N + timedelta(minutes=10),
               started_at=N - timedelta(minutes=2)),
            TK(id=13, message="Чужая", target="screens", screen_ids=[6], started_at=N - timedelta(minutes=1))]
    db = RoutedDB([("ORDER BY t.started_at", live),
                   ("SELECT id, group_id FROM screens", [Row(id=5, group_id=None), Row(id=6, group_id=None)])])
    ws = FakeWS(connected=[5], db=db)
    monkeypatch.setattr(screens_router, "ws_manager", ws)
    assert call(screens_router.command_resume_screen, 5, admin=ADMIN, db=db) == {"status": "ok", "ws_sent": True}
    assert commands(db) == [(5, "resume", {}),
                            (5, "ticker_show", {"message": "Новая", "color": "#ffd34d", "speed": "medium", "duration": 600})]
    assert db.commits == 2                                                 # «Показ», потом ticker_show
    assert ws.sent == [(5, {"type": "new_command", "count": 1}, 2)]


def test_resume_without_live_tickers_queues_only_resume(screens_clock, monkeypatch):
    # живых строк нет (срок вышел или не было): лишняя команда в очереди старого агента, которому по WS
    # дают одну команду за пробуждение, только задержала бы следующий ticker_show
    expired = [TK(id=11, target="all", until_at=N - timedelta(seconds=1))]
    db = RoutedDB([("ORDER BY t.started_at", expired), ("SELECT id, group_id FROM screens", [Row(id=5, group_id=None)])])
    monkeypatch.setattr(screens_router, "ws_manager", FakeWS())
    call(screens_router.command_resume_screen, 5, admin=ADMIN, db=db)
    assert commands(db) == [(5, "resume", {})]
    assert db.commits == 1


def test_resume_survives_a_failing_ticker_part(screens_clock, monkeypatch):
    # на установке новый API стартует раньше миграции 034: чтение строк падает. Команда «Показ» уже
    # закоммичена и не теряется, а ответ — ok
    db = TxDB([], fail_on="FROM tickers")
    ws = FakeWS(connected=[5], db=db)
    monkeypatch.setattr(screens_router, "ws_manager", ws)
    assert call(screens_router.command_resume_screen, 5, admin=ADMIN, db=db) == {"status": "ok", "ws_sent": True}
    assert commands(db) == [(5, "resume", {})]
    assert at(db, "INSERT INTO commands") < at(db, "-- COMMIT") < at(db, "FROM tickers t") < at(db, "-- ROLLBACK")
    assert db.commits == 1 and db.rollbacks == 1 and not db.aborted
    assert ws.sent == [(5, {"type": "new_command", "count": 1}, 1)]


def test_poll_takes_commands_in_a_stable_order():
    # команды одной транзакции (stop + ticker_hide, resume + ticker_show) имеют один created_at:
    # без id порядок при равенстве не определён
    db = RoutedDB([("FROM commands", [Row(id=7, type="stop", payload={})])])
    out = screens_router.poll_commands(5, db=db, _screen=None)
    assert out == {"command": {"id": 7, "type": "stop", "payload": {}}}
    (sql,) = db.sqls("FROM commands")
    assert "ORDER BY created_at, id LIMIT 1" in sql


def heartbeat(db, body, screen_id=3):
    return call(screens_router.heartbeat, screen_id, body, db=db, _screen=None)


def report(db):
    return db.params("SET ticker_ids")


def test_heartbeat_records_ticker_state():
    db = RoutedDB([])
    out = heartbeat(db, {"status": "online", "agent_version": "1.4.0",
                         "ticker_state": {"ids": [11, 12], "stopped": True, "screen_off": False}})
    assert out["status"] == "ok"
    assert report(db) == [{"sid": 3, "ids": [11, 12], "stopped": True, "off": False}]
    assert len(db.params("SET last_seen")) == 1 and db.commits == 2        # сигнал и отчёт — каждый своим commit


def test_heartbeat_without_ticker_state_leaves_columns():
    db = RoutedDB([])
    heartbeat(db, {"status": "online", "agent_version": "1.3.0", "playing_file": "a.mp4"})   # старый агент
    assert report(db) == [] and not db.sqls("ticker_")
    assert len(db.params("SET last_seen")) == 1 and db.commits == 1


@pytest.mark.parametrize("raw", ["oops", ["ids"], 5, True, {}, {"stopped": True}, {"ids": "11,12"}, {"ids": None}, {"ids": 11}])
def test_heartbeat_ignores_malformed_ticker_state(raw):
    db = RoutedDB([])
    assert heartbeat(db, {"status": "online", "ticker_state": raw})["status"] == "ok"
    assert report(db) == [] and db.commits == 1                             # прежний отчёт не затёрт


def test_heartbeat_keeps_only_real_ids():
    db = RoutedDB([])
    heartbeat(db, {"ticker_state": {"ids": [11, "12", None, True, 1.5, 0, -3, 2 ** 40, 13], "screen_off": 1}})
    assert report(db) == [{"sid": 3, "ids": [11, 13], "stopped": False, "off": True}]


def test_heartbeat_empty_ids_is_a_report_not_a_missing_one():
    # экран остановлен или строк нет — пустой список: это отчёт «ничего не бежит», а не отсутствие отчёта
    db = RoutedDB([])
    heartbeat(db, {"ticker_state": {"ids": [], "stopped": True}})
    assert report(db) == [{"sid": 3, "ids": [], "stopped": True, "off": False}]


def test_heartbeat_survives_a_failing_ticker_report():
    # на установке новый API стартует раньше миграции 034: колонок отчёта ещё нет. Сигнал экрана и история
    # (last_seen, minipc_status) закоммичены ДО отчёта, а сбой отчёта heartbeat не роняет и транзакцию откатывает
    db = TxDB([], fail_on="SET ticker_ids")
    out = heartbeat(db, {"status": "online", "agent_version": "1.4.0", "ticker_state": {"ids": [11]}})
    assert out["status"] == "ok" and "server_time" in out
    assert at(db, "SET last_seen") < at(db, "INSERT INTO minipc_status") < at(db, "-- COMMIT") \
        < at(db, "SET ticker_ids") < at(db, "-- ROLLBACK")
    assert db.commits == 1 and db.rollbacks == 1 and not db.aborted


# --- Celery: снятие по сроку ----------------------------------------------------

def _run_task(monkeypatch, db, now=N):
    """Запустить tasks.expire_tickers на подменной сессии; вернуть (результат, engine.disposed)."""
    disposed = []
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@db/x")
    monkeypatch.setattr(sqlalchemy, "create_engine", lambda url, **kw: Row(dispose=lambda: disposed.append(url)))

    class _Session:
        def __init__(self, engine):
            pass

        def __enter__(self):
            return db

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(sqlalchemy.orm, "Session", _Session)
    monkeypatch.setattr(placements_store, "now_msk", lambda: now)
    return tasks.expire_tickers(), disposed


def test_expire_tickers_is_scheduled_every_minute():
    assert tasks.celery_app.conf.beat_schedule["expire-tickers"] == {"task": "expire_tickers", "schedule": 60.0}
    assert "expire_tickers" in tasks.celery_app.tasks


def test_expire_tickers_task_commits_once(monkeypatch):
    db = RoutedDB([])
    seen = []
    due = [{"id": 1, "message": "A", "targets": [1, 2]}, {"id": 2, "message": "B", "targets": [2, 3]}]
    monkeypatch.setattr(ticker_store, "expire_due", lambda db_, now: seen.append(("expire_due", db_, now)) or due)
    monkeypatch.setattr(ticker_store, "notify_screens",
                        lambda db_, before, after, now: seen.append(("notify", before, after, now)) or [])
    out, disposed = _run_task(monkeypatch, db)
    assert out == {"expired": 2}
    assert seen == [("expire_due", db, N), ("notify", {1, 2, 3}, set(), N)]
    assert db.commits == 1 and db.rollbacks == 0
    assert disposed == ["postgresql://u:p@db/x"]


def test_expire_tickers_resends_the_remaining_ticker_to_old_agents(monkeypatch):
    # у старого агента строка одна: срок «новой» A вышел, «до снятия» B ещё живёт — ей нужен ticker_show
    gone = TK(id=11, message="A", target="screens", screen_ids=[1], until_at=N - timedelta(seconds=1),
              started_at=N - timedelta(minutes=2))
    left = TK(id=12, message="B", target="all", started_at=N - timedelta(minutes=9))
    db = RoutedDB([("FOR UPDATE", [gone]),
                   ("ORDER BY t.started_at", [left]),
                   ("SELECT id, group_id FROM screens", [Row(id=1, group_id=None), Row(id=2, group_id=None)])])
    out, _ = _run_task(monkeypatch, db)
    assert out == {"expired": 1}
    assert commands(db) == [(1, "ticker_show", {"message": "B", "color": "#ffd34d", "speed": "medium", "duration": 0})]
    assert journal(db) == [("Бегущая строка снята по сроку", "A", "система")]       # в журнале ровно один раз
    assert db.commits == 1


def test_expire_tickers_hides_when_nothing_is_left(monkeypatch):
    gone = TK(id=11, target="screens", screen_ids=[1, 2], until_at=N - timedelta(seconds=1))
    db = RoutedDB([("FOR UPDATE", [gone]),
                   ("SELECT id, group_id FROM screens", [Row(id=1, group_id=None), Row(id=2, group_id=None)])])
    _run_task(monkeypatch, db)
    assert commands(db) == [(1, "ticker_hide", {}), (2, "ticker_hide", {})]


def test_expire_tickers_nothing_due_sends_nothing(monkeypatch):
    db = RoutedDB([])
    out, _ = _run_task(monkeypatch, db)
    assert out == {"expired": 0}
    assert not db.params("INSERT INTO commands") and not journal(db)
    assert db.commits == 1


def test_expire_tickers_error_is_reported_not_raised(monkeypatch):
    db = RoutedDB([], fail_on="FOR UPDATE")
    out, disposed = _run_task(monkeypatch, db)
    assert "error" in out and db.commits == 0
    assert disposed                                                         # соединение закрыто и при ошибке


def test_expire_tickers_without_database_url(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert tasks.expire_tickers() == "error: DATABASE_URL is not set"
