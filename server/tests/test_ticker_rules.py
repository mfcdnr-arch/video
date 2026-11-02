"""Бегущая строка: правила — срок, «где», строки экрана, подтверждение с экрана.

Чистая логика (ticker_rules) без БД и без fastapi; все моменты — наивное МСК.
SQL строк — test_ticker_store, API — test_ticker_api.
"""
from datetime import datetime, timedelta

import pytest

import broadcast_rules
import ticker_rules as rules
from routers.broadcast import STALE_WINDOW_TEXT
from ticker_rules import (
    CONFIRM_VERSION, MAX_LEN, MAX_MINUTES, PENDING_SECONDS, REASON_TEXT, REASONS, SPEEDS, TARGETS,
    parse_term, parse_ticker_body, resolve_screens, screen_reason, summarize, ticker_live,
    tickers_for_screen, version_tuple,
)

N = datetime(2026, 11, 2, 14, 30)


def body(**kw):
    """Минимально верное тело «всем экранам» + переопределения."""
    return {"message": "x", "target": "all", **kw}


# --- константы ------------------------------------------------------------------

def test_constants():
    assert (MAX_LEN, MAX_MINUTES, PENDING_SECONDS) == (200, 1440, 90)
    assert CONFIRM_VERSION == (1, 4, 0)
    assert tuple(SPEEDS) == ("slow", "medium", "fast")
    assert TARGETS == ("all", "groups", "screens")


def test_every_reason_has_text():
    assert set(REASON_TEXT) == set(REASONS)
    assert len(REASONS) == 7


def test_stale_text_lives_in_broadcast_rules():
    """Константа перенесена в правила эфира, а в роутере осталась под тем же именем."""
    assert broadcast_rules.STALE_WINDOW_TEXT == STALE_WINDOW_TEXT
    assert rules.STALE_WINDOW_TEXT == STALE_WINDOW_TEXT


# --- срок -----------------------------------------------------------------------

def test_term_cancel_is_none():
    assert parse_term({"kind": "cancel"}, N) is None
    assert parse_term(None, N) is None


def test_term_minutes_adds_to_now():
    assert parse_term({"kind": "minutes", "minutes": 30}, N) == datetime(2026, 11, 2, 15, 0)
    assert parse_term({"kind": "minutes", "minutes": MAX_MINUTES}, N) == N + timedelta(days=1)


def test_term_minutes_crosses_midnight():
    assert parse_term({"kind": "minutes", "minutes": 15}, datetime(2026, 11, 2, 23, 50)) == datetime(2026, 11, 3, 0, 5)


@pytest.mark.parametrize("bad", [0, 1441, -5, True, "15", 15.5, None])
def test_term_minutes_bounds(bad):
    with pytest.raises(ValueError, match="от 1 до 1440"):
        parse_term({"kind": "minutes", "minutes": bad}, N)


def test_term_hour_uses_broadcast_rules():
    assert parse_term({"kind": "hour", "hour": 16}, N) == datetime(2026, 11, 2, 16, 0)
    assert parse_term({"kind": "hour", "hour": 24}, N) == datetime(2026, 11, 3, 0, 0)
    with pytest.raises(ValueError, match="позже текущего"):
        parse_term({"kind": "hour", "hour": 14}, N)


@pytest.mark.parametrize("bad", [None, 0, 25, "16", True, 16.5])
def test_term_hour_junk_is_not_until_cancel(bad):
    """Час не выбран — это ошибка, а не молчаливое «до снятия»."""
    with pytest.raises(ValueError, match="от 1 до 24"):
        parse_term({"kind": "hour", "hour": bad}, N)


def test_term_hour_missing_is_error():
    with pytest.raises(ValueError, match="от 1 до 24"):
        parse_term({"kind": "hour"}, N)


def test_term_hour_stale_on_date():
    with pytest.raises(ValueError) as stale:
        parse_term({"kind": "hour", "hour": 16, "on_date": "2026-11-01"}, N)
    assert str(stale.value) == STALE_WINDOW_TEXT
    assert parse_term({"kind": "hour", "hour": 16, "on_date": "2026-11-02"}, N) == datetime(2026, 11, 2, 16, 0)
    with pytest.raises(ValueError, match="ГГГГ-ММ-ДД"):
        parse_term({"kind": "hour", "hour": 16, "on_date": "02.11.2026"}, N)


@pytest.mark.parametrize("bad", [{}, {"kind": "week"}, {"kind": None}, "minutes", 15, []])
def test_term_unknown_shape(bad):
    with pytest.raises(ValueError, match="Срок"):
        parse_term(bad, N)


# --- тело запроса ---------------------------------------------------------------

def test_body_new_shape():
    out = parse_ticker_body({
        "message": "  Касса  ", "color": "#ffffff", "speed": "fast", "target": "screens",
        "screen_ids": [3, 1], "term": {"kind": "minutes", "minutes": 30}}, N)
    assert out == {
        "message": "Касса", "color": "#ffffff", "speed": "fast", "target": "screens",
        "group_ids": [], "screen_ids": [1, 3], "until_at": N + timedelta(minutes=30), "term_given": True}


def test_body_defaults():
    out = parse_ticker_body({"message": "x", "target": "all"}, N)
    assert (out["color"], out["speed"]) == ("#ffd34d", "medium")
    assert out["until_at"] is None
    assert out["term_given"] is True


def test_body_legacy_shape():
    out = parse_ticker_body({"message": "x", "is_all": False, "screen_ids": [2], "duration": 60}, N)
    assert out["target"] == "screens"
    assert out["screen_ids"] == [2]
    assert out["until_at"] == N + timedelta(seconds=60)
    out = parse_ticker_body({"message": "x", "is_all": True}, N)
    assert out["target"] == "all"
    assert out["until_at"] is None


def test_body_legacy_all_ignores_screen_ids():
    """Старая панель шлёт is_all=true вместе с пустым screen_ids; лишний список в «все» не уходит."""
    out = parse_ticker_body({"message": "x", "is_all": True, "screen_ids": [5], "duration": 0}, N)
    assert (out["target"], out["screen_ids"], out["until_at"]) == ("all", [], None)


def test_body_legacy_without_is_all_means_all():
    assert parse_ticker_body({"message": "x"}, N)["target"] == "all"


def test_body_term_wins_over_duration():
    out = parse_ticker_body(body(term={"kind": "minutes", "minutes": 5}, duration=600), N)
    assert out["until_at"] == N + timedelta(minutes=5)


@pytest.mark.parametrize("bad", [-1, "60", 60.5, True, 86401])
def test_body_legacy_bad_duration(bad):
    with pytest.raises(ValueError, match="Длительность"):
        parse_ticker_body(body(duration=bad), N)


def test_body_groups_target():
    out = parse_ticker_body({"message": "x", "target": "groups", "group_ids": [4, 2, 4], "screen_ids": [9]}, N)
    assert (out["target"], out["group_ids"], out["screen_ids"]) == ("groups", [2, 4], [])


def test_body_term_error_is_valueerror():
    with pytest.raises(ValueError, match="от 1 до 1440"):
        parse_ticker_body(body(term={"kind": "minutes", "minutes": 0}), N)


def test_message_newlines_become_spaces():
    assert parse_ticker_body(body(message="Строка 1\nстрока 2"), N)["message"] == "Строка 1 строка 2"
    assert parse_ticker_body(body(message="a\r\nb\n\nc\n"), N)["message"] == "a b c"


def test_message_special_characters_kept():
    """Спецсимволы ASS экранирует агент, а панель — через esc(); сервер текст не портит."""
    text = '<b>{\\an8}"x"</b>'
    assert parse_ticker_body(body(message=text), N)["message"] == text


def test_message_length_limit_counts_after_strip():
    assert parse_ticker_body(body(message=" " + "я" * MAX_LEN + " "), N)["message"] == "я" * MAX_LEN


@pytest.mark.parametrize("raw, match", [
    (body(message=""), "Текст"),
    (body(message="   \n "), "Текст"),
    (body(message=None), "Текст"),
    (body(message=5), "Текст"),
    ({"target": "all"}, "Текст"),
    (body(message="я" * 201), "200"),
    (body(color="red"), "Цвет"),
    (body(color="#fff"), "Цвет"),
    (body(color=5), "Цвет"),
    (body(speed="turbo"), "Скорость"),
    (body(target="groups"), "групп"),
    (body(target="groups", group_ids=[]), "групп"),
    (body(target="screens"), "экран"),
    (body(target="everywhere"), "Где"),
    (body(target="screens", screen_ids=["a"]), "экран"),
    (body(target="screens", screen_ids=[True]), "экран"),
    (body(target="screens", screen_ids=[0]), "экран"),
    (body(target="screens", screen_ids=5), "экран"),
    (body(target="screens", screen_ids=[]), "экран"),
    (body(target="screens", screen_ids=[2 ** 31]), "экран"),
    (body(target="groups", group_ids=["x"]), "групп"),
    ({"message": "x", "is_all": False}, "экран"),
])
def test_body_rejects(raw, match):
    with pytest.raises(ValueError, match=match):
        parse_ticker_body(raw, N)


def test_body_not_a_dict():
    with pytest.raises(ValueError, match="JSON-объект"):
        parse_ticker_body([], N)


def test_keep_term_when_absent():
    out = parse_ticker_body({"message": "x", "target": "all"}, N, keep_term=True)
    assert out["term_given"] is False
    assert out["until_at"] is None


def test_keep_term_when_given():
    out = parse_ticker_body(body(term={"kind": "minutes", "minutes": 10}), N, keep_term=True)
    assert out["term_given"] is True
    assert out["until_at"] == N + timedelta(minutes=10)
    out = parse_ticker_body(body(term={"kind": "cancel"}), N, keep_term=True)
    assert (out["term_given"], out["until_at"]) == (True, None)


# --- живая ли строка ------------------------------------------------------------

def test_ticker_live_until_exclusive():
    assert ticker_live(N, N) is False
    assert ticker_live(N + timedelta(seconds=1), N) is True
    assert ticker_live(N - timedelta(seconds=1), N) is False
    assert ticker_live(None, N) is True


# --- «где» → экраны -------------------------------------------------------------

SCREENS = [(1, 10), (2, 10), (3, None)]


def test_resolve_screens():
    assert resolve_screens("all", [], [], SCREENS) == [1, 2, 3]
    assert resolve_screens("groups", [10], [], SCREENS) == [1, 2]
    assert resolve_screens("screens", [], [3, 1], SCREENS) == [1, 3]


def test_resolve_screens_ignores_missing_ids():
    assert resolve_screens("screens", [], [3, 99], SCREENS) == [3]
    assert resolve_screens("groups", [77], [], SCREENS) == []
    assert resolve_screens("screens", [], [98, 99], SCREENS) == []


def test_resolve_screens_ignores_other_list():
    """Для «групп» screen_ids не нужны, и наоборот."""
    assert resolve_screens("groups", [10], [3], SCREENS) == [1, 2]
    assert resolve_screens("screens", [10], [3], SCREENS) == [3]


def test_resolve_screens_unknown_target():
    with pytest.raises(ValueError, match="Где"):
        resolve_screens("nowhere", [], [], SCREENS)


# --- строки экрана --------------------------------------------------------------

def ticker(tid, started, *, target="all", group_ids=(), screen_ids=(), until_at=None):
    return {"id": tid, "message": f"m{tid}", "color": "#ffd34d", "speed": "medium", "target": target,
            "group_ids": list(group_ids), "screen_ids": list(screen_ids), "until_at": until_at,
            "started_at": started}


def test_tickers_for_screen_order_and_expiry():
    late = ticker(1, datetime(2026, 11, 2, 10, 0), target="screens", screen_ids=[1])
    early = ticker(2, datetime(2026, 11, 2, 9, 0), target="screens", screen_ids=[1])
    dead = ticker(3, datetime(2026, 11, 2, 8, 0), target="screens", screen_ids=[1], until_at=N)
    alien = ticker(4, datetime(2026, 11, 2, 7, 0), target="screens", screen_ids=[2])
    got = tickers_for_screen([late, dead, alien, early], 1, None, N)
    assert [t["id"] for t in got] == [2, 1]
    assert got[0] is early


def test_tickers_for_screen_group_and_all():
    by_group = ticker(1, datetime(2026, 11, 2, 9, 0), target="groups", group_ids=[10])
    other_group = ticker(2, datetime(2026, 11, 2, 9, 5), target="groups", group_ids=[11])
    everyone = ticker(3, datetime(2026, 11, 2, 9, 10))
    assert [t["id"] for t in tickers_for_screen([by_group, other_group, everyone], 1, 10, N)] == [1, 3]
    # экран без группы групповую строку не получает
    assert [t["id"] for t in tickers_for_screen([by_group, other_group, everyone], 1, None, N)] == [3]


def test_tickers_for_screen_same_start_ordered_by_id():
    t = datetime(2026, 11, 2, 9, 0)
    got = tickers_for_screen([ticker(7, t), ticker(5, t), ticker(6, t)], 1, None, N)
    assert [x["id"] for x in got] == [5, 6, 7]


def test_tickers_for_screen_empty():
    assert tickers_for_screen([], 1, None, N) == []


# --- версия и причина -----------------------------------------------------------

def test_version_tuple():
    assert version_tuple("1.4.0") == (1, 4, 0)
    assert version_tuple("1.10.0") > version_tuple("1.4.0")
    assert version_tuple("1.3.9") < CONFIRM_VERSION
    assert version_tuple(None) == ()
    assert version_tuple("abc") == ()
    assert version_tuple("") == ()
    assert version_tuple("1.x") == ()
    assert version_tuple(14) == ()


def screen(**kw):
    return {"online": True, "agent_version": "1.4.0", "ticker_ids": [], "ticker_stopped": False,
            "ticker_screen_off": False, **kw}


@pytest.mark.parametrize("state, want", [
    (screen(online=False, ticker_ids=[5]), "offline"),
    (screen(agent_version="1.3.0", ticker_ids=[5]), "old_agent"),
    (screen(agent_version=None), "old_agent"),
    (screen(agent_version="abc"), "old_agent"),
    (screen(ticker_stopped=True, ticker_ids=[5]), "stopped"),
    (screen(ticker_screen_off=True, ticker_ids=[5]), "screen_off"),
    (screen(ticker_stopped=True, ticker_screen_off=True), "stopped"),
    (screen(online=False, agent_version="1.3.0", ticker_stopped=True), "offline"),
    (screen(agent_version="1.3.0", ticker_stopped=True), "old_agent"),
])
def test_reason_order(state, want):
    assert screen_reason(5, state, N - timedelta(hours=1), N) == want


def test_reason_running_only_if_screen_reported_id():
    state = screen(ticker_ids=[5])
    assert screen_reason(5, state, N - timedelta(seconds=30), N) == "running"
    assert screen_reason(5, state, N - timedelta(hours=1), N) == "running"
    assert screen_reason(6, state, N - timedelta(seconds=30), N) == "pending"
    assert screen_reason(6, state, N - timedelta(seconds=120), N) == "not_confirmed"


def test_reason_pending_window_edges():
    state = screen()
    assert screen_reason(1, state, N - timedelta(seconds=89), N) == "pending"
    assert screen_reason(1, state, N - timedelta(seconds=90), N) == "not_confirmed"


def test_reason_screen_never_reported():
    """Агент 1.4.0 ещё не прислал ticker_state (None) — как пустой список."""
    state = screen(ticker_ids=None, ticker_stopped=None, ticker_screen_off=None)
    assert screen_reason(1, state, N - timedelta(seconds=10), N) == "pending"
    assert screen_reason(1, state, N - timedelta(minutes=10), N) == "not_confirmed"


def test_reason_texts_verbatim():
    assert REASON_TEXT["offline"] == "не в сети"
    assert REASON_TEXT["old_agent"] == "старый агент — без подтверждения"
    assert REASON_TEXT["stopped"] == "остановлен («Стоп»)"
    assert REASON_TEXT["screen_off"] == "экран выключен по расписанию питания"
    assert REASON_TEXT["pending"] == "ждём подтверждения"
    assert REASON_TEXT["not_confirmed"] == "не подтвердил — проверьте экран"


# --- сводка ---------------------------------------------------------------------

def test_summarize():
    out = summarize([("A", "running"), ("B", "offline"), ("C", "running")])
    assert out == {"total": 3, "running": 2,
                   "problems": [{"screen": "B", "reason": "offline", "text": "не в сети"}]}


def test_summarize_empty_and_order():
    assert summarize([]) == {"total": 0, "running": 0, "problems": []}
    out = summarize([("Z", "pending"), ("A", "stopped")])
    assert [p["screen"] for p in out["problems"]] == ["Z", "A"]
    assert out["running"] == 0
