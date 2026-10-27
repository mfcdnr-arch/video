"""placements_store.resolve_placement_hour: какие плейлисты показа играют на
экране в текущий час МСК.

Без SQL: загрузчики (load_placements / load_overrides / load_decisions)
подменяются через monkeypatch, поэтому функция обязана брать их по именам
модуля. Сама логика выбора — placements_logic (applies_at, choose_playlists).
"""
from datetime import date, datetime

import placements_store as st


def test_resolver_uses_msk_now(monkeypatch):
    p = {"id": 1, "playlist_id": 10, "playlist_name": "A", "date_from": date(2026, 11, 1), "date_to": date(2026, 11, 1),
         "hour_from": 0, "hour_to": 1, "stopped_at": None, "screens": {5}}
    monkeypatch.setattr(st, "load_placements", lambda db, a, b, playlist_id=None: [p])
    monkeypatch.setattr(st, "load_overrides", lambda db, a, b: [])
    monkeypatch.setattr(st, "load_decisions", lambda db, ids: {})
    assert st.resolve_placement_hour(None, 5, datetime(2026, 11, 1, 0, 30)) == [10]
    assert st.resolve_placement_hour(None, 5, datetime(2026, 10, 31, 23, 30)) == []


D = date(2026, 11, 1)


def P(id, pl, screens, h1=9, h2=13, saved=None, stopped=None):
    return {"id": id, "playlist_id": pl, "playlist_name": f"PL{pl}", "date_from": D, "date_to": D,
            "hour_from": h1, "hour_to": h2, "stopped_at": stopped, "saved_at": saved, "screens": set(screens)}


def OV(id, screen_id, pl=None, off=False, on_date=D):
    return {"id": id, "screen_id": screen_id, "on_date": on_date, "playlist_id": pl,
            "playlist_name": None if pl is None else f"PL{pl}", "is_off": off}


def patch(monkeypatch, placements, overrides=(), decisions=None):
    """Подменить загрузчики; вернуть dict, куда они пишут свои аргументы."""
    calls = {}

    def lp(db, a, b, playlist_id=None):
        calls["placements"] = (a, b)
        return list(placements)

    def lo(db, a, b):
        calls["overrides"] = (a, b)
        return list(overrides)

    def ld(db, ids):
        calls["decisions"] = sorted(ids)
        return dict(decisions or {})

    monkeypatch.setattr(st, "load_placements", lp)
    monkeypatch.setattr(st, "load_overrides", lo)
    monkeypatch.setattr(st, "load_decisions", ld)
    return calls


def test_resolver_reads_only_that_day_and_decisions_of_applicable(monkeypatch):
    calls = patch(monkeypatch, [P(1, 10, [5]), P(2, 20, [6]), P(3, 30, [5], h1=14, h2=16)])
    assert st.resolve_placement_hour(None, 5, datetime(2026, 11, 1, 10, 5)) == [10]
    assert calls["placements"] == (D, D) and calls["overrides"] == (D, D)
    assert calls["decisions"] == [1]          # показ 2 — чужой экран, 3 — другие часы


def test_resolver_other_screen_or_no_placements_gives_empty(monkeypatch):
    patch(monkeypatch, [P(1, 10, [6])])
    assert st.resolve_placement_hour(None, 5, datetime(2026, 11, 1, 10, 0)) == []
    patch(monkeypatch, [])
    assert st.resolve_placement_hour(None, 5, datetime(2026, 11, 1, 10, 0)) == []


def test_resolver_newest_first_and_alternate_by_decision(monkeypatch):
    old = P(1, 10, [5], saved=datetime(2026, 10, 20))
    new = P(2, 20, [5], saved=datetime(2026, 10, 25))
    patch(monkeypatch, [old, new], decisions={(2, 5, "placement", 1): "alternate"})
    assert st.resolve_placement_hour(None, 5, datetime(2026, 11, 1, 9, 0)) == [20, 10]
    patch(monkeypatch, [old, new], decisions={(2, 5, "placement", 1): "replace"})
    assert st.resolve_placement_hour(None, 5, datetime(2026, 11, 1, 9, 0)) == [20]


def test_resolver_passes_todays_override_of_this_screen(monkeypatch):
    p = P(1, 10, [5])
    dec = {(1, 5, "override", 7): "alternate"}
    patch(monkeypatch, [p], [OV(7, 5, pl=30), OV(8, 6, pl=40)], dec)
    assert st.resolve_placement_hour(None, 5, datetime(2026, 11, 1, 9, 0)) == [10, 30]
    # переопределение другого экрана не подхватывается
    patch(monkeypatch, [p], [OV(7, 6, pl=30)], dec)
    assert st.resolve_placement_hour(None, 5, datetime(2026, 11, 1, 9, 0)) == [10]


def test_resolver_ignores_is_off_override_and_stopped_hours(monkeypatch):
    # «выключен» проверяют вызывающие раньше; в выбор плейлистов он не идёт
    patch(monkeypatch, [P(1, 10, [5])], [OV(7, 5, off=True)], {(1, 5, "override", 7): "alternate"})
    assert st.resolve_placement_hour(None, 5, datetime(2026, 11, 1, 9, 0)) == [10]
    # остановлен в 10:37: час 9–10 уже отыграл, 10–11 погашен
    patch(monkeypatch, [P(1, 10, [5], stopped=datetime(2026, 11, 1, 10, 37))])
    assert st.resolve_placement_hour(None, 5, datetime(2026, 11, 1, 9, 30)) == [10]
    assert st.resolve_placement_hour(None, 5, datetime(2026, 11, 1, 10, 50)) == []
