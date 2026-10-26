"""API показов (server/routers/placements.py): разбор и проверка тела запроса.

Чистые функции роутера, БД не нужна. SQL-часть (placements_store) проверяется
smoke_test.sh на живом стенде.
"""
from datetime import date
import pytest
from fastapi import HTTPException
from routers.placements import parse_body, parse_decisions, unresolved, summary_text, overflow_notes

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
