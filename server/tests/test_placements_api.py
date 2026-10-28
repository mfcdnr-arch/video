"""API показов (server/routers/placements.py): разбор и проверка тела запроса.

Чистые функции роутера, БД не нужна. Разбор строк БД (placements_store) —
в test_placements_store.py; smoke_test.sh на стенде проходит только путь без
пересечений (предпросмотр и создание).
"""
from datetime import date, datetime
import pytest
from fastapi import HTTPException
from routers.placements import (parse_body, parse_decisions, unresolved, summary_text, overflow_notes,
                                week_start, sort_listing, listing_row, check_stoppable, check_deletable)

T = date(2026, 10, 26)
B = {"playlist_id": 1, "all_screens": False, "screen_ids": [3], "date_from": "2026-11-01",
     "date_to": "2026-11-30", "hour_from": 9, "hour_to": 13}

def test_parse_body_ok():
    d = parse_body(B, T)
    assert (d["date_from"], d["hour_to"], d["screens"]) == (date(2026, 11, 1), 13, {3})

@pytest.mark.parametrize("patch", [{"hour_to": 9}, {"hour_from": 24}, {"hour_to": 25},
                                   {"date_to": "2026-10-31"}, {"screen_ids": []}, {"date_from": "2026-10-25"}])
def test_parse_body_rejects(patch):
    with pytest.raises(HTTPException) as e:
        parse_body({**B, **patch}, T)
    assert e.value.status_code == 400

def test_parse_body_allows_past_date_from_when_unchanged():
    d = parse_body({**B, "date_from": "2026-10-20"}, T, existing_date_from=date(2026, 10, 20))
    assert d["date_from"] == date(2026, 10, 20)

def test_all_screens_needs_no_list():
    assert parse_body({**B, "all_screens": True, "screen_ids": []}, T)["all_screens"] is True

@pytest.mark.parametrize("patch", [{"hour_from": "²"}, {"playlist_id": True}, {"hour_to": 12.5},
                                   {"screen_ids": ["x"]}, {"date_from": None}])
def test_parse_body_rejects_junk_values_with_400(patch):
    with pytest.raises(HTTPException) as e:
        parse_body({**B, **patch}, T)
    assert e.value.status_code == 400


# --- решения по пересечениям, итоговая фраза, переполнение часа ---------------

C = {"screen_id": 3, "other_kind": "placement", "other_id": 5, "other_playlist_id": 2,
     "hour_from": 10, "hour_to": 12}

def test_parse_decisions_ok():
    raw = [{"screen_id": 3, "other_kind": "placement", "other_id": 5, "mode": "alternate"}]
    assert parse_decisions(raw) == {(3, "placement", 5): "alternate"}

@pytest.mark.parametrize("raw", ["x", [{"screen_id": 3, "other_kind": "placement", "other_id": 5, "mode": "both"}],
                                 [{"screen_id": 3, "other_kind": "slot", "other_id": 5, "mode": "replace"}],
                                 [{"other_kind": "placement", "other_id": 5, "mode": "replace"}]])
def test_parse_decisions_rejects(raw):
    with pytest.raises(HTTPException) as e:
        parse_decisions(raw)
    assert e.value.status_code == 400

def test_parse_decisions_accepts_all_three_modes():
    raw = [{"screen_id": 3, "other_kind": "placement", "other_id": 5, "mode": "yield"},
           {"screen_id": 3, "other_kind": "override", "other_id": 9, "mode": "yield"},
           {"screen_id": 4, "other_kind": "placement", "other_id": 5, "mode": "replace"},
           {"screen_id": 5, "other_kind": "placement", "other_id": 5, "mode": "alternate"}]
    assert parse_decisions(raw) == {(3, "placement", 5): "yield", (3, "override", 9): "yield",
                                    (4, "placement", 5): "replace", (5, "placement", 5): "alternate"}

def test_unresolved_counts_conflicts_without_decision():
    other = {**C, "screen_id": 4}
    assert unresolved([C, other], {(3, "placement", 5): "replace"}) == 1
    assert unresolved([C], {}) == 1
    assert unresolved([], {}) == 0

def test_summary_text():
    assert summary_text(parse_body(B, T), 2) == (
        "Плейлист пойдёт на 2 экран(ах) с 01.11 по 30.11.2026, ежедневно 09:00–13:00. "
        "В остальное время экраны показывают своё обычное расписание.")

def test_summary_text_all_screens():
    d = parse_body({**B, "all_screens": True, "screen_ids": [], "hour_to": 24}, T)
    assert summary_text(d, 7) == (
        "Плейлист пойдёт на всех экранах сети (7) с 01.11 по 30.11.2026, ежедневно 09:00–24:00. "
        "В остальное время экраны показывают своё обычное расписание.")

def test_overflow_notes():
    secs = {1: 2400, 2: 1500, 9: 600}
    total = lambda playlist_id, screen_id: secs[playlist_id]
    other_screen = {**C, "screen_id": 4}                     # то же пересечение на другом экране
    fits = {**C, "other_playlist_id": 9}                     # 2400 + 600 = ровно час — помещается
    same = {**C, "other_playlist_id": 1}                     # тот же плейлист — не «два плейлиста»
    assert overflow_notes([C, other_screen, fits, same], 1, total) == [
        "в 10:00 ролики двух плейлистов занимают 65 мин — в час не помещаются, часть выйдет реже"]


# --- неделя, порядок списка, 409 на остановку и отмену -------------------------

def test_week_start_must_be_monday():
    assert week_start("2026-11-02") == date(2026, 11, 2)
    with pytest.raises(HTTPException):
        week_start("2026-11-03")


@pytest.mark.parametrize("raw", ["2026-11-03", "2026-11-01", "", "завтра", "2026-13-01", None])
def test_week_start_rejects_non_monday_and_junk_with_400(raw):
    with pytest.raises(HTTPException) as e:
        week_start(raw)
    assert e.value.status_code == 400


def test_sort_listing_active_then_planned_then_finished_each_newest_start_first():
    def row(id, status, d):
        return {"id": id, "status": status, "date_from": date(2026, 11, d)}
    rows = [row(1, "done", 20), row(2, "planned", 3), row(3, "active", 1), row(4, "stopped", 25),
            row(5, "planned", 9), row(6, "active", 5), row(7, "done", 2)]
    assert [r["id"] for r in sort_listing(rows)] == [6, 3, 5, 2, 4, 1, 7]
    assert sort_listing([]) == []


def test_listing_row_shape_status_screens_and_alternates():
    p = {"id": 7, "playlist_id": 10, "playlist_name": "Утро", "all_screens": False, "screens": {3, 1, 9},
         "date_from": date(2026, 11, 1), "date_to": date(2026, 11, 30), "hour_from": 9, "hour_to": 13,
         "stopped_at": None, "created_by": "admin", "created_at": datetime(2026, 10, 26, 12, 0)}
    alt = {"screen_id": 3, "other_kind": "placement", "other_id": 8, "other_playlist_id": 20,
           "other_playlist_name": "Акция", "date_from": date(2026, 11, 5), "date_to": date(2026, 11, 7),
           "hour_from": 10, "hour_to": 12, "mode": "alternate"}
    gives_way = {"screen_id": 1, "other_kind": "override", "other_id": 4, "other_playlist_id": 30,
                 "other_playlist_name": "Вечер", "date_from": date(2026, 11, 8), "date_to": date(2026, 11, 8),
                 "hour_from": 9, "hour_to": 13, "mode": "yield"}
    row = listing_row(p, datetime(2026, 11, 3, 10), {1: "Бета", 3: "Альфа"}, [alt, gives_way])
    assert row == {
        "id": 7, "playlist_id": 10, "all_screens": False, "screen_count": 3,
        "screens": [{"id": 3, "name": "Альфа"}, {"id": 1, "name": "Бета"}, {"id": 9, "name": "Экран #9"}],
        "date_from": date(2026, 11, 1), "date_to": date(2026, 11, 30), "hour_from": 9, "hour_to": 13,
        "status": "active",
        "alternates": [{"screen_id": 3, "screen_name": "Альфа", "mode": "alternate", "other_playlist_name": "Акция",
                        "date_from": date(2026, 11, 5), "date_to": date(2026, 11, 7), "hour_from": 10, "hour_to": 12},
                       {"screen_id": 1, "screen_name": "Бета", "mode": "yield", "other_playlist_name": "Вечер",
                        "date_from": date(2026, 11, 8), "date_to": date(2026, 11, 8), "hour_from": 9, "hour_to": 13}],
        "created_by": "admin", "created_at": datetime(2026, 10, 26, 12, 0)}
    assert listing_row(p, datetime(2026, 10, 30, 10), {}, [])["status"] == "planned"
    assert listing_row({**p, "stopped_at": datetime(2026, 11, 2)}, datetime(2026, 11, 3), {}, [])["status"] == "stopped"


def test_check_stoppable_only_active():
    check_stoppable("active")
    for status in ("planned", "done", "stopped"):
        with pytest.raises(HTTPException) as e:
            check_stoppable(status)
        assert e.value.status_code == 409 and e.value.detail == "Остановить можно только идущий показ"


def test_check_deletable_only_planned():
    check_deletable("planned")
    for status in ("active", "done", "stopped"):
        with pytest.raises(HTTPException) as e:
            check_deletable(status)
        assert e.value.status_code == 409
        assert e.value.detail == "Показ уже идёт или закончился — используйте «Остановить»"
