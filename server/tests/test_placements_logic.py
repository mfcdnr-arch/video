"""Логика показов (server/placements_logic.py): даты, часы, статусы,
пересечения, выбор плейлистов. БД не нужна — на вход простые dict.

Все даты и часы — наивное МСК. Показ: [date_from..date_to] включительно,
часы [hour_from, hour_to) — полуинтервал.
"""
from datetime import date, datetime, timedelta

from placements_logic import (dates_overlap, hours_overlap, applies_at, placement_status,
                              find_conflicts, choose_playlists, template_playlist,
                              week_cells, alternate_overlaps)


def P(id, d1, d2, h1, h2, screens, pl=10, stopped=None, saved=None):
    p = {"id": id, "playlist_id": pl, "playlist_name": f"PL{pl}", "date_from": d1, "date_to": d2,
         "hour_from": h1, "hour_to": h2, "stopped_at": stopped, "screens": set(screens)}
    if saved is not None:
        p["saved_at"] = saved       # когда показ создан/правился последним; без него — порядок по id
    return p


def OV(id, screen_id, on_date, pl=30, off=False):
    return {"id": id, "screen_id": screen_id, "on_date": on_date,
            "playlist_id": None if off else pl, "playlist_name": None if off else f"PL{pl}", "is_off": off}


N1, N30 = date(2026, 11, 1), date(2026, 11, 30)


# --- даты и часы -------------------------------------------------------------

def test_dates_overlap_inclusive():
    assert dates_overlap(N1, date(2026, 11, 10), date(2026, 11, 10), N30) == (date(2026, 11, 10), date(2026, 11, 10))
    assert dates_overlap(N1, date(2026, 11, 9), date(2026, 11, 10), N30) is None
    assert dates_overlap(N1, N30, date(2026, 11, 5), date(2026, 11, 7)) == (date(2026, 11, 5), date(2026, 11, 7))


def test_hours_overlap_half_open():
    assert hours_overlap(9, 13, 13, 15) is None and hours_overlap(9, 13, 10, 12) == (10, 12)


def test_hours_overlap_partial_and_disjoint():
    assert hours_overlap(9, 13, 12, 18) == (12, 13)
    assert hours_overlap(0, 24, 23, 24) == (23, 24)
    assert hours_overlap(9, 10, 11, 12) is None


# --- applies_at --------------------------------------------------------------

def test_applies_at_uses_given_msk_hour():
    p = P(1, N1, N30, 0, 24, [1])
    assert applies_at(p, date(2026, 11, 1), 0) and not applies_at(p, date(2026, 10, 31), 23)


def test_applies_at_hour_and_date_bounds():
    p = P(1, N1, N30, 9, 13, [1])
    assert applies_at(p, N30, 12) and not applies_at(p, N30, 13)
    assert applies_at(p, N1, 9) and not applies_at(p, N1, 8)
    assert not applies_at(p, date(2026, 12, 1), 10)


def test_stop_mid_hour_stops_current_hour():
    p = P(1, N1, N30, 9, 13, [1], stopped=datetime(2026, 11, 5, 10, 37))
    assert applies_at(p, date(2026, 11, 5), 9) and not applies_at(p, date(2026, 11, 5), 10)


def test_stop_on_the_hour_keeps_finished_hour():
    p = P(1, N1, N30, 9, 13, [1], stopped=datetime(2026, 11, 5, 10, 0))
    assert applies_at(p, date(2026, 11, 5), 9) and not applies_at(p, date(2026, 11, 5), 10)


def test_stop_affects_later_days_only_not_earlier_ones():
    p = P(1, N1, N30, 9, 13, [1], stopped=datetime(2026, 11, 5, 10, 37))
    assert applies_at(p, date(2026, 11, 4), 12)        # день целиком до остановки
    assert not applies_at(p, date(2026, 11, 6), 9)     # день после остановки


def test_stop_at_midnight_edge_does_not_crash_on_hour_23():
    p = P(1, N1, N30, 0, 24, [1], stopped=datetime(2026, 11, 6, 0, 0))
    assert applies_at(p, date(2026, 11, 5), 23)        # 23:00–24:00 закончился ровно в stopped_at
    assert not applies_at(p, date(2026, 11, 6), 0)


# --- статус ------------------------------------------------------------------

def test_status():
    p = P(1, N1, N30, 9, 13, [1])
    assert placement_status(p, datetime(2026, 10, 31, 12)) == "planned"
    assert placement_status(p, datetime(2026, 11, 30, 12)) == "active"
    assert placement_status(p, datetime(2026, 11, 30, 13)) == "done"


def test_status_stopped_beats_everything_and_done_next_day():
    p = P(1, N1, N30, 9, 13, [1], stopped=datetime(2026, 11, 5, 10, 37))
    assert placement_status(p, datetime(2026, 10, 1)) == "stopped"
    assert placement_status(p, datetime(2026, 12, 15)) == "stopped"
    q = P(2, N1, N30, 9, 13, [1])
    assert placement_status(q, datetime(2026, 12, 1, 0)) == "done"
    assert placement_status(q, datetime(2026, 11, 1, 0)) == "active"   # день начался, час ещё нет — не «planned»


# --- пересечения -------------------------------------------------------------

def test_conflict_with_other_placement_and_override():
    new = P(None, N1, N30, 9, 13, [1, 2])
    other = P(7, date(2026, 11, 5), date(2026, 11, 10), 10, 12, [1], pl=20)
    ov = {"id": 3, "screen_id": 2, "on_date": date(2026, 11, 7), "playlist_id": 30, "playlist_name": "PL30", "is_off": False}
    off = {"id": 4, "screen_id": 1, "on_date": date(2026, 11, 8), "playlist_id": None, "playlist_name": None, "is_off": True}
    conflicts, off_days = find_conflicts(new, [other], [ov, off])
    assert [(c["screen_id"], c["other_kind"], c["date_from"], c["hour_from"], c["hour_to"]) for c in conflicts] == \
        [(1, "placement", date(2026, 11, 5), 10, 12), (2, "override", date(2026, 11, 7), 9, 13)]
    assert off_days == [{"screen_id": 1, "on_date": date(2026, 11, 8)}]


def test_conflict_dict_shape_for_placement_and_override():
    new = P(None, N1, N30, 9, 13, [1])
    other = P(7, date(2026, 11, 5), date(2026, 12, 10), 12, 18, [1], pl=20)
    ov = OV(3, 1, date(2026, 11, 7), pl=30)
    conflicts, _ = find_conflicts(new, [other], [ov])
    assert conflicts == [
        {"screen_id": 1, "other_kind": "placement", "other_id": 7, "other_playlist_id": 20,
         "other_playlist_name": "PL20", "date_from": date(2026, 11, 5), "date_to": N30,
         "hour_from": 12, "hour_to": 13},
        {"screen_id": 1, "other_kind": "override", "other_id": 3, "other_playlist_id": 30,
         "other_playlist_name": "PL30", "date_from": date(2026, 11, 7), "date_to": date(2026, 11, 7),
         "hour_from": 9, "hour_to": 13},
    ]


def test_no_conflict_when_hours_touch_dates_miss_or_screens_differ():
    new = P(None, N1, N30, 9, 13, [1])
    assert find_conflicts(new, [P(7, N1, N30, 13, 15, [1])], []) == ([], [])                       # часы встык
    assert find_conflicts(new, [P(7, date(2026, 12, 1), date(2026, 12, 5), 9, 13, [1])], []) == ([], [])  # даты мимо
    assert find_conflicts(new, [P(7, N1, N30, 9, 13, [2, 3])], []) == ([], [])                     # другие экраны


def test_conflict_only_on_common_screens():
    new = P(None, N1, N30, 9, 13, [1, 2])
    conflicts, _ = find_conflicts(new, [P(7, N1, N30, 9, 13, [2, 3])], [])
    assert [c["screen_id"] for c in conflicts] == [2]


def test_conflicts_skip_self_and_stopped():
    new = P(5, N1, N30, 9, 13, [1])
    assert find_conflicts(new, [P(5, N1, N30, 9, 13, [1]), P(6, N1, N30, 9, 13, [1], stopped=datetime(2026, 11, 2))], []) == ([], [])


def test_new_without_id_does_not_skip_others_without_id():
    new = P(None, N1, N30, 9, 13, [1])
    conflicts, _ = find_conflicts(new, [P(None, N1, N30, 9, 13, [1])], [])
    assert len(conflicts) == 1


def test_override_outside_new_dates_or_screens_ignored():
    new = P(None, N1, date(2026, 11, 10), 9, 13, [1])
    ovs = [OV(1, 1, date(2026, 11, 11)), OV(2, 2, date(2026, 11, 5)), OV(3, 1, date(2026, 11, 12), off=True)]
    assert find_conflicts(new, [], ovs) == ([], [])


def test_conflicts_sorted_by_screen_then_date():
    new = P(None, N1, N30, 9, 13, [1, 2])
    a = P(1, date(2026, 11, 20), date(2026, 11, 22), 9, 13, [1, 2], pl=11)
    b = P(2, date(2026, 11, 3), date(2026, 11, 4), 9, 13, [1, 2], pl=12)
    ov = OV(5, 1, date(2026, 11, 10))
    conflicts, _ = find_conflicts(new, [a, b], [ov])
    assert [(c["screen_id"], c["date_from"].day) for c in conflicts] == [(1, 3), (1, 10), (1, 20), (2, 3), (2, 20)]


def test_off_days_sorted_and_per_screen():
    new = P(None, N1, N30, 9, 13, [1, 2])
    offs = [OV(1, 2, date(2026, 11, 3), off=True), OV(2, 1, date(2026, 11, 9), off=True), OV(3, 1, date(2026, 11, 2), off=True)]
    _, off_days = find_conflicts(new, [], offs)
    assert off_days == [{"screen_id": 1, "on_date": date(2026, 11, 2)},
                        {"screen_id": 1, "on_date": date(2026, 11, 9)},
                        {"screen_id": 2, "on_date": date(2026, 11, 3)}]


# --- выбор плейлистов --------------------------------------------------------

def test_choose_newest_wins_without_decision():
    assert choose_playlists([P(1, N1, N30, 9, 13, [1], pl=10), P(2, N1, N30, 9, 13, [1], pl=20)], {}, None, 1) == [20]


def test_choose_alternate_includes_older_and_override():
    d = {(2, 1, "placement", 1): "alternate", (2, 1, "override", 3): "alternate"}
    ov = {"id": 3, "playlist_id": 30, "is_off": False}
    assert choose_playlists([P(1, N1, N30, 9, 13, [1], pl=10), P(2, N1, N30, 9, 13, [1], pl=20)], d, ov, 1) == [20, 10, 30]


def test_choose_newest_by_id_not_by_list_order():
    pls = [P(5, N1, N30, 9, 13, [1], pl=50), P(2, N1, N30, 9, 13, [1], pl=20)]
    assert choose_playlists(pls, {}, None, 1) == [50]


def test_choose_replace_and_missing_decision_exclude():
    pls = [P(1, N1, N30, 9, 13, [1], pl=10), P(2, N1, N30, 9, 13, [1], pl=20)]
    ov = {"id": 3, "playlist_id": 30, "is_off": False}
    assert choose_playlists(pls, {(2, 1, "placement", 1): "replace", (2, 1, "override", 3): "replace"}, ov, 1) == [20]
    assert choose_playlists(pls, {}, ov, 1) == [20]


def test_choose_decisions_are_per_screen():
    pls = [P(1, N1, N30, 9, 13, [1, 2], pl=10), P(2, N1, N30, 9, 13, [1, 2], pl=20)]
    d = {(2, 1, "placement", 1): "alternate"}
    assert choose_playlists(pls, d, None, 1) == [20, 10]
    assert choose_playlists(pls, d, None, 2) == [20]


def test_choose_three_placements_only_chosen_older_join_in_newest_first_order():
    pls = [P(1, N1, N30, 9, 13, [1], pl=10), P(2, N1, N30, 9, 13, [1], pl=20), P(3, N1, N30, 9, 13, [1], pl=30)]
    d = {(3, 1, "placement", 1): "alternate"}                       # id=2 не выбран
    assert choose_playlists(pls, d, None, 1) == [30, 10]
    d = {(3, 1, "placement", 1): "alternate", (3, 1, "placement", 2): "alternate"}
    assert choose_playlists(pls, d, None, 1) == [30, 20, 10]


def test_choose_decisions_of_older_placements_are_ignored():
    pls = [P(1, N1, N30, 9, 13, [1], pl=10), P(2, N1, N30, 9, 13, [1], pl=20), P(3, N1, N30, 9, 13, [1], pl=30)]
    d = {(2, 1, "placement", 1): "alternate"}                       # решение не у новейшего (3)
    assert choose_playlists(pls, d, None, 1) == [30]


def test_choose_edited_older_placement_wins_by_saved_at():
    # A (id=1) правили позже, чем B (id=2) создали: приоритет у последнего сохранённого,
    # а решения лежат под id правленого показа — (1, экран, "placement", 2).
    a = P(1, N1, N30, 9, 13, [1], pl=10, saved=datetime(2026, 11, 3, 12))
    b = P(2, N1, N30, 9, 13, [1], pl=20, saved=datetime(2026, 11, 2, 12))
    assert choose_playlists([a, b], {(1, 1, "placement", 2): "alternate"}, None, 1) == [10, 20]
    assert choose_playlists([a, b], {(1, 1, "placement", 2): "replace"}, None, 1) == [10]
    assert choose_playlists([a, b], {}, None, 1) == [10]
    # решения «со стороны» вытесненного (id=2) для него не работают
    assert choose_playlists([a, b], {(2, 1, "placement", 1): "alternate"}, None, 1) == [10]


def test_choose_edited_older_placement_with_override_and_three_placements():
    a = P(1, N1, N30, 9, 13, [1], pl=10, saved=datetime(2026, 11, 9))
    b = P(2, N1, N30, 9, 13, [1], pl=20, saved=datetime(2026, 11, 2))
    c = P(3, N1, N30, 9, 13, [1], pl=30, saved=datetime(2026, 11, 5))
    ov = {"id": 7, "playlist_id": 70, "is_off": False}
    d = {(1, 1, "placement", 3): "alternate", (1, 1, "placement", 2): "alternate", (1, 1, "override", 7): "alternate"}
    # порядок: сначала самый свежий по saved_at (A), дальше по убыванию saved_at (C, B), в конце переопределение
    assert choose_playlists([b, c, a], d, ov, 1) == [10, 30, 20, 70]


def test_choose_saved_at_ties_and_missing_fall_back_to_id():
    t = datetime(2026, 11, 2, 12)
    same = [P(1, N1, N30, 9, 13, [1], pl=10, saved=t), P(2, N1, N30, 9, 13, [1], pl=20, saved=t)]
    assert choose_playlists(same, {}, None, 1) == [20]                       # saved_at равны — новейший по id
    mixed = [P(5, N1, N30, 9, 13, [1], pl=50), P(2, N1, N30, 9, 13, [1], pl=20, saved=t)]
    assert choose_playlists(mixed, {}, None, 1) == [20]                      # без saved_at — самый давний


def test_choose_off_override_never_added():
    off = {"id": 3, "playlist_id": None, "is_off": True}
    d = {(2, 1, "override", 3): "alternate"}
    assert choose_playlists([P(2, N1, N30, 9, 13, [1], pl=20)], d, off, 1) == [20]


def test_choose_no_repeats_of_playlist_id():
    pls = [P(1, N1, N30, 9, 13, [1], pl=20), P(2, N1, N30, 9, 13, [1], pl=20)]
    ov = {"id": 3, "playlist_id": 20, "is_off": False}
    d = {(2, 1, "placement", 1): "alternate", (2, 1, "override", 3): "alternate"}
    assert choose_playlists(pls, d, ov, 1) == [20]


def test_choose_empty_applicable():
    ov = {"id": 3, "playlist_id": 30, "is_off": False}
    assert choose_playlists([], {(2, 1, "override", 3): "alternate"}, ov, 1) == []


# --- шаблон недели -----------------------------------------------------------

def test_template_hour_beats_all_day():
    slots = [{"day_of_week": 0, "hour": None, "playlist_id": 1, "prio": 3},
             {"day_of_week": 0, "hour": 9, "playlist_id": 2, "prio": 1}]
    assert template_playlist(slots, 0, 9) == 2 and template_playlist(slots, 0, 10) == 1


def test_template_prio_within_group_and_other_days_ignored():
    slots = [{"day_of_week": 0, "hour": None, "playlist_id": 1, "prio": 1},
             {"day_of_week": 0, "hour": None, "playlist_id": 2, "prio": 5},
             {"day_of_week": 0, "hour": 0, "playlist_id": 3, "prio": 1},
             {"day_of_week": 0, "hour": 0, "playlist_id": 4, "prio": 2},
             {"day_of_week": 1, "hour": 0, "playlist_id": 9, "prio": 9}]
    assert template_playlist(slots, 0, 5) == 2
    assert template_playlist(slots, 0, 0) == 4          # час 0 — это час, а не «весь день»
    assert template_playlist(slots, 1, 5) is None
    assert template_playlist([], 0, 0) is None


# --- недельная сетка ---------------------------------------------------------

DAYS = [date(2026, 11, 2) + timedelta(days=i) for i in range(7)]      # пн 02.11 … вс 08.11
ALL_DAY_1 = [{"day_of_week": d, "hour": None, "playlist_id": 1, "prio": 1} for d in range(7)]


def cells_of(placements, decisions=None, overrides=(), slots=(), names=None, screen_id=5):
    out = week_cells(DAYS, screen_id, list(placements), decisions or {}, list(overrides), list(slots),
                     names if names is not None else {})
    return {(c["date"], c["hour"]): c for c in out}


def test_week_cells_priorities():
    days = [date(2026, 11, 2) + timedelta(days=i) for i in range(7)]
    pl = [P(1, N1, N30, 9, 13, [5], pl=10)]
    ovs = [{"id": 9, "screen_id": 5, "on_date": date(2026, 11, 3), "playlist_id": None, "playlist_name": None, "is_off": True}]
    slots = [{"day_of_week": d, "hour": None, "playlist_id": 1, "prio": 1} for d in range(7)]
    cells = {(c["date"], c["hour"]): c for c in week_cells(days, 5, pl, {}, ovs, slots, {10: "Утро", 1: "Общий"})}
    assert cells[(date(2026, 11, 2), 9)]["kind"] == "placement" and cells[(date(2026, 11, 2), 9)]["playlists"] == ["Утро"]
    assert cells[(date(2026, 11, 2), 13)]["kind"] == "template"
    assert cells[(date(2026, 11, 3), 9)]["kind"] == "off"
    assert len(cells) == 168
    # R1: рядом с названиями — id плейлистов (в том же порядке), по ним UI открывает плейлист
    assert cells[(date(2026, 11, 2), 9)]["playlist_ids"] == [10]
    assert cells[(date(2026, 11, 2), 13)]["playlists"] == ["Общий"] and cells[(date(2026, 11, 2), 13)]["playlist_ids"] == [1]


def test_week_cells_shape_order_and_placement_id():
    out = week_cells(DAYS, 5, [P(7, N1, N30, 9, 13, [5], pl=10)], {}, [], [], {10: "Утро"})
    assert [(c["date"], c["hour"]) for c in out] == [(d, h) for d in DAYS for h in range(24)]   # по дням, внутри — по часам
    assert all(set(c) == {"date", "hour", "kind", "playlists", "playlist_ids", "placement_id"} for c in out)
    c = {(c["date"], c["hour"]): c for c in out}
    assert c[(DAYS[0], 9)]["placement_id"] == 7
    assert c[(DAYS[0], 8)] == {"date": DAYS[0], "hour": 8, "kind": "empty", "playlists": [],
                               "playlist_ids": [], "placement_id": None}


def test_week_cells_placement_hours_are_half_open_and_dates_inclusive():
    p = P(1, date(2026, 11, 3), date(2026, 11, 4), 9, 13, [5], pl=10)       # вт 03.11 … ср 04.11, часы 9–12
    c = cells_of([p], names={10: "Утро"})
    assert [c[(DAYS[1], h)]["kind"] for h in (8, 9, 12, 13)] == ["empty", "placement", "placement", "empty"]
    assert [c[(DAYS[2], h)]["kind"] for h in (9, 12, 13)] == ["placement", "placement", "empty"]   # последний день — включительно
    assert c[(DAYS[0], 10)]["kind"] == "empty" and c[(DAYS[3], 10)]["kind"] == "empty"             # 02.11 и 05.11 вне дат


def test_week_cells_other_screen_and_stopped_placements_do_not_show():
    other_screen = P(1, N1, N30, 9, 13, [6], pl=10)
    stopped = P(2, N1, N30, 9, 13, [5], pl=20, stopped=datetime(2026, 11, 2, 0, 0))
    c = cells_of([other_screen, stopped], slots=ALL_DAY_1, names={10: "A", 20: "B", 1: "Общий"})
    assert c[(DAYS[0], 10)]["kind"] == "template"                                           # шаблон, а не чужой/остановленный показ


def test_week_cells_stopped_placement_keeps_finished_hours():
    p = P(2, N1, N30, 9, 13, [5], pl=20, stopped=datetime(2026, 11, 2, 10, 37))             # остановлен в 10:37
    c = cells_of([p], slots=ALL_DAY_1, names={20: "B", 1: "Общий"})
    assert c[(DAYS[0], 9)]["kind"] == "placement" and c[(DAYS[0], 10)]["kind"] == "template"
    assert c[(DAYS[1], 9)]["kind"] == "template"


def test_week_cells_alternate_two_placements_newest_first():
    older = P(1, N1, N30, 9, 13, [5], pl=10, saved=datetime(2026, 10, 27))
    newer = P(2, N1, N30, 9, 13, [5], pl=20, saved=datetime(2026, 10, 28))
    dec = {(2, 5, "placement", 1): "alternate"}
    cell = cells_of([older, newer], dec, names={10: "Утро", 20: "Акция"})[(DAYS[0], 10)]
    assert (cell["kind"], cell["playlists"], cell["playlist_ids"], cell["placement_id"]) == \
        ("alternate", ["Акция", "Утро"], [20, 10], 2)
    replaced = cells_of([older, newer], {(2, 5, "placement", 1): "replace"}, names={10: "Утро", 20: "Акция"})[(DAYS[0], 10)]
    assert (replaced["kind"], replaced["playlists"], replaced["placement_id"]) == ("placement", ["Акция"], 2)


def test_week_cells_override_with_playlist_and_its_alternate_with_placement():
    ov = OV(9, 5, DAYS[2], pl=30)
    c = cells_of([P(1, N1, N30, 9, 13, [5], pl=10)], {(1, 5, "override", 9): "alternate"}, [ov],
                 ALL_DAY_1, {10: "Утро", 30: "Акция дня", 1: "Общий"})
    assert (c[(DAYS[2], 15)]["kind"], c[(DAYS[2], 15)]["playlists"], c[(DAYS[2], 15)]["placement_id"]) == \
        ("override", ["Акция дня"], None)                                                  # переопределение бьёт шаблон
    both = c[(DAYS[2], 10)]
    assert (both["kind"], both["playlists"], both["playlist_ids"], both["placement_id"]) == \
        ("alternate", ["Утро", "Акция дня"], [10, 30], 1)
    no_decision = cells_of([P(1, N1, N30, 9, 13, [5], pl=10)], {}, [ov], ALL_DAY_1, {10: "Утро", 30: "Акция дня"})
    assert no_decision[(DAYS[2], 10)]["kind"] == "placement" and no_decision[(DAYS[2], 10)]["playlists"] == ["Утро"]


def test_week_cells_override_of_other_screen_or_day_ignored_and_off_beats_placement():
    ovs = [OV(1, 6, DAYS[0], pl=30), OV(2, 5, DAYS[3], off=True)]
    c = cells_of([P(1, N1, N30, 9, 13, [5], pl=10)], {}, ovs, [], {10: "Утро", 30: "Чужой"})
    assert c[(DAYS[0], 10)]["kind"] == "placement"
    off = c[(DAYS[3], 10)]
    assert (off["kind"], off["playlists"], off["playlist_ids"], off["placement_id"]) == ("off", [], [], None)


def test_week_cells_template_hour_beats_all_day_and_missing_name_has_fallback():
    slots = [{"day_of_week": 0, "hour": None, "playlist_id": 1, "prio": 3},
             {"day_of_week": 0, "hour": 9, "playlist_id": 2, "prio": 1}]
    c = cells_of([], slots=slots, names={1: "Весь день"})
    assert (c[(DAYS[0], 9)]["kind"], c[(DAYS[0], 9)]["playlist_ids"]) == ("template", [2])
    assert c[(DAYS[0], 9)]["playlists"] == ["Плейлист #2"]                                  # имени нет в names — не падаем
    assert c[(DAYS[0], 10)]["playlists"] == ["Весь день"]
    assert c[(DAYS[1], 9)]["kind"] == "empty"                                              # во вторник слотов нет


def test_week_cells_names_fall_back_to_names_inside_placements_and_overrides():
    c = cells_of([P(1, N1, N30, 9, 13, [5], pl=10)], {(1, 5, "override", 9): "alternate"}, [OV(9, 5, DAYS[0], pl=30)])
    assert c[(DAYS[0], 10)]["playlists"] == ["PL10", "PL30"]                              # имена из самих показа и переопределения


# --- «по очереди» в карточке показа -------------------------------------------

def test_alternate_overlaps_follow_decision_of_the_newer_side():
    older = P(1, N1, N30, 9, 13, [1, 2], pl=10, saved=datetime(2026, 10, 27))
    newer = P(2, date(2026, 11, 5), date(2026, 11, 7), 11, 15, [1], pl=20, saved=datetime(2026, 10, 28))
    dec = {(2, 1, "placement", 1): "alternate"}                    # решение лежит у новейшего (2)
    from_newer = alternate_overlaps(newer, [older, newer], [], dec)
    assert [(a["screen_id"], a["other_id"], a["other_playlist_name"], a["date_from"], a["date_to"],
             a["hour_from"], a["hour_to"]) for a in from_newer] == [(1, 1, "PL10", date(2026, 11, 5), date(2026, 11, 7), 11, 13)]
    from_older = alternate_overlaps(older, [older, newer], [], dec)    # со стороны старого — то же пересечение
    assert [(a["screen_id"], a["other_id"], a["other_playlist_name"]) for a in from_older] == [(1, 2, "PL20")]
    assert alternate_overlaps(older, [older, newer], [], {(2, 1, "placement", 1): "replace"}) == []
    assert alternate_overlaps(older, [older, newer], [], {}) == []


def test_alternate_overlaps_override_stopped_other_and_same_playlist():
    p = P(1, N1, N30, 9, 13, [1], pl=10, saved=datetime(2026, 10, 28))
    ov = OV(4, 1, date(2026, 11, 9), pl=30)
    stopped = P(3, N1, N30, 9, 13, [1], pl=40, stopped=datetime(2026, 11, 1), saved=datetime(2026, 10, 20))
    twin = P(5, N1, N30, 9, 13, [1], pl=10, saved=datetime(2026, 10, 21))      # тот же плейлист — сам с собой не чередуется
    dec = {(1, 1, "override", 4): "alternate", (1, 1, "placement", 3): "alternate", (1, 1, "placement", 5): "alternate"}
    out = alternate_overlaps(p, [p, stopped, twin], [ov], dec)
    assert [(a["other_kind"], a["other_id"], a["date_from"], a["hour_from"], a["hour_to"]) for a in out] == \
        [("override", 4, date(2026, 11, 9), 9, 13)]
