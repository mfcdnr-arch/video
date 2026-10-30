"""«Что идёт сейчас / до какого часа / что дальше» для страницы «Эфир сети»
(server/network_live.py) и фильтр `playable` у недельной сетки
(placements_logic.week_cells). БД не нужна — на вход простые dict и клетки.

Все даты и часы — наивное МСК. Клетка — как в week_cells:
{"date", "hour", "kind", "playlist_ids", "playlists", "placement_id"}.
"""
from datetime import date, datetime

from network_live import screen_live
from placements_logic import week_cells

D = date(2026, 10, 28)        # «сегодня» в тестах (среда)
D2 = date(2026, 10, 29)       # «завтра»


def cells(spec_today, spec_tomorrow=()):
    """spec: [(hour_from, hour_to, kind, ids)] → 48 клеток week_cells (имена = «PL<id>»)."""
    out = []
    for day, spec in ((D, spec_today), (D2, spec_tomorrow)):
        for h in range(24):
            kind, ids = "empty", []
            for a, b, k, i in spec:
                if a <= h < b:
                    kind, ids = k, list(i)
            out.append({"date": day, "hour": h, "kind": kind, "playlist_ids": ids,
                        "playlists": [f"PL{i}" for i in ids], "placement_id": None})
    return out


def bc(until_at, playlist_id=9, name="Эфир"):
    """Действующий общий эфир в виде get_broadcast."""
    return {"is_on": True, "playlist_id": playlist_id, "playlist_name": name, "until_at": until_at,
            "enabled_by": "admin", "enabled_at": datetime(2026, 10, 28, 9, 0)}


# --- screen_live: без общего эфира ---------------------------------------------

def test_screen_live_template_until_next_change():
    c = cells([(0, 13, "template", [1]), (13, 15, "placement", [2]), (15, 24, "template", [1])])
    r = screen_live(c, datetime(2026, 10, 28, 10, 30), None, False)
    assert r["now"] == {"kind": "template", "playlist_ids": [1], "playlists": ["PL1"], "until": "2026-10-28T13:00:00"}
    assert r["next"] == {"kind": "placement", "playlist_ids": [2], "playlists": ["PL2"], "from": "2026-10-28T13:00:00"}
    assert r["after_broadcast"] is None and r["broadcast_skipped"] is False


def test_screen_live_no_change_until_end_of_day():
    # сегодня шаблон весь день, а завтра уже другое: «дальше» смотрит только до конца сегодняшнего дня
    c = cells([(0, 24, "template", [1])], [(0, 24, "placement", [2])])
    r = screen_live(c, datetime(2026, 10, 28, 10, 30), None, False)
    assert r["now"] == {"kind": "template", "playlist_ids": [1], "playlists": ["PL1"], "until": None}
    assert r["next"] is None
    assert r["after_broadcast"] is None and r["broadcast_skipped"] is False


def test_screen_live_alternate_kind():
    c = cells([(0, 9, "template", [1]), (9, 12, "alternate", [2, 3]), (12, 24, "template", [1])])
    r = screen_live(c, datetime(2026, 10, 28, 10, 0), None, False)
    assert r["now"] == {"kind": "alternate", "playlist_ids": [2, 3], "playlists": ["PL2", "PL3"],
                        "until": "2026-10-28T12:00:00"}
    assert r["next"] == {"kind": "template", "playlist_ids": [1], "playlists": ["PL1"], "from": "2026-10-28T12:00:00"}


def test_screen_live_alternate_is_next():
    c = cells([(0, 9, "template", [1]), (9, 12, "alternate", [2, 3]), (12, 24, "template", [1])])
    r = screen_live(c, datetime(2026, 10, 28, 8, 15), None, False)
    assert r["now"]["until"] == "2026-10-28T09:00:00"
    assert r["next"] == {"kind": "alternate", "playlist_ids": [2, 3], "playlists": ["PL2", "PL3"],
                         "from": "2026-10-28T09:00:00"}


def test_screen_live_off_override():
    c = cells([(0, 24, "off", [])])
    r = screen_live(c, datetime(2026, 10, 28, 10, 30), None, False)
    assert r["now"] == {"kind": "off", "playlist_ids": [], "playlists": [], "until": None}
    assert r["next"] is None and r["after_broadcast"] is None and r["broadcast_skipped"] is False


def test_screen_live_empty_schedule_until_first_placement():
    c = cells([(14, 16, "placement", [2])])
    r = screen_live(c, datetime(2026, 10, 28, 10, 30), None, False)
    assert r["now"] == {"kind": "empty", "playlist_ids": [], "playlists": [], "until": "2026-10-28T14:00:00"}
    assert r["next"] == {"kind": "placement", "playlist_ids": [2], "playlists": ["PL2"], "from": "2026-10-28T14:00:00"}


def test_screen_live_same_playlist_but_other_kind_is_a_change():
    # тот же плейлист 1: сначала по шаблону, потом показом — это смена источника
    c = cells([(0, 10, "template", [1]), (10, 24, "placement", [1])])
    r = screen_live(c, datetime(2026, 10, 28, 8, 0), None, False)
    assert r["now"]["until"] == "2026-10-28T10:00:00"
    assert r["next"] == {"kind": "placement", "playlist_ids": [1], "playlists": ["PL1"], "from": "2026-10-28T10:00:00"}


def test_screen_live_same_kind_other_playlist_is_a_change_and_equal_hours_merge():
    # 13–15 и 15–16 — один и тот же показ (без смены), «до» считается от конца склейки
    c = cells([(0, 13, "template", [1]), (13, 16, "placement", [2]), (16, 20, "placement", [3]),
               (20, 24, "template", [1])])
    r1 = screen_live(c, datetime(2026, 10, 28, 14, 30), None, False)
    assert r1["now"] == {"kind": "placement", "playlist_ids": [2], "playlists": ["PL2"], "until": "2026-10-28T16:00:00"}
    assert r1["next"] == {"kind": "placement", "playlist_ids": [3], "playlists": ["PL3"], "from": "2026-10-28T16:00:00"}
    r2 = screen_live(c, datetime(2026, 10, 28, 8, 0), None, False)        # час 8..12 — один шаблон, склейка до 13
    assert r2["now"]["until"] == "2026-10-28T13:00:00"


def test_screen_live_now_exactly_on_hour_boundary_belongs_to_new_hour():
    c = cells([(0, 13, "template", [1]), (13, 15, "placement", [2]), (15, 24, "template", [1])])
    r = screen_live(c, datetime(2026, 10, 28, 13, 0, 0), None, False)
    assert r["now"] == {"kind": "placement", "playlist_ids": [2], "playlists": ["PL2"], "until": "2026-10-28T15:00:00"}
    assert r["next"] == {"kind": "template", "playlist_ids": [1], "playlists": ["PL1"], "from": "2026-10-28T15:00:00"}
    r = screen_live(c, datetime(2026, 10, 28, 12, 59, 59), None, False)   # секундой раньше — ещё шаблон
    assert r["now"] == {"kind": "template", "playlist_ids": [1], "playlists": ["PL1"], "until": "2026-10-28T13:00:00"}


def test_screen_live_change_at_23_00():
    c = cells([(0, 23, "template", [1]), (23, 24, "placement", [2])], [(0, 24, "template", [1])])
    r = screen_live(c, datetime(2026, 10, 28, 22, 59), None, False)
    assert r["now"]["until"] == "2026-10-28T23:00:00"
    assert r["next"] == {"kind": "placement", "playlist_ids": [2], "playlists": ["PL2"], "from": "2026-10-28T23:00:00"}
    r = screen_live(c, datetime(2026, 10, 28, 23, 0), None, False)        # в последний час дня смены уже нет
    assert r["now"] == {"kind": "placement", "playlist_ids": [2], "playlists": ["PL2"], "until": None}
    assert r["next"] is None


def test_screen_live_result_does_not_alias_cells():
    c = cells([(0, 13, "template", [1]), (13, 24, "placement", [2])])
    r = screen_live(c, datetime(2026, 10, 28, 10, 0), None, False)
    r["now"]["playlist_ids"].append(99)
    r["next"]["playlists"].append("X")
    assert c[10]["playlist_ids"] == [1] and c[13]["playlists"] == ["PL2"]


# --- screen_live: общий эфир ----------------------------------------------------

def test_screen_live_broadcast_with_until_and_after():
    c = cells([(0, 16, "template", [1]), (16, 24, "placement", [2])])
    r = screen_live(c, datetime(2026, 10, 28, 14, 30), bc(datetime(2026, 10, 28, 16, 0)), True)
    assert r["now"] == {"kind": "broadcast", "playlist_ids": [9], "playlists": ["Эфир"], "until": "2026-10-28T16:00:00"}
    assert r["next"] is None
    assert r["after_broadcast"] == {"kind": "placement", "playlist_ids": [2], "playlists": ["PL2"]}
    assert r["broadcast_skipped"] is False


def test_screen_live_broadcast_until_cancel_after_is_current():
    c = cells([(0, 16, "template", [1]), (16, 24, "placement", [2])])
    r = screen_live(c, datetime(2026, 10, 28, 14, 30), bc(None), True)
    assert r["now"] == {"kind": "broadcast", "playlist_ids": [9], "playlists": ["Эфир"], "until": None}
    assert r["next"] is None
    assert r["after_broadcast"] == {"kind": "template", "playlist_ids": [1], "playlists": ["PL1"]}
    assert r["broadcast_skipped"] is False


def test_screen_live_broadcast_until_midnight_after_is_tomorrow_00():
    # «до 24:00» хранится как завтра 00:00 — «после эфира» берётся из завтрашней клетки 00:00
    c = cells([(0, 24, "template", [1])], [(0, 6, "placement", [2]), (6, 24, "template", [1])])
    r = screen_live(c, datetime(2026, 10, 28, 20, 10), bc(datetime(2026, 10, 29, 0, 0)), True)
    assert r["now"] == {"kind": "broadcast", "playlist_ids": [9], "playlists": ["Эфир"], "until": "2026-10-29T00:00:00"}
    assert r["after_broadcast"] == {"kind": "placement", "playlist_ids": [2], "playlists": ["PL2"]}
    assert r["next"] is None


def test_screen_live_broadcast_until_unaligned_minute_uses_that_hour_cell():
    c = cells([(0, 16, "template", [1]), (16, 24, "placement", [2])])
    r = screen_live(c, datetime(2026, 10, 28, 14, 30), bc(datetime(2026, 10, 28, 16, 30)), True)
    assert r["now"]["until"] == "2026-10-28T16:30:00"
    assert r["after_broadcast"] == {"kind": "placement", "playlist_ids": [2], "playlists": ["PL2"]}


def test_screen_live_broadcast_until_beyond_cells_has_no_after():
    c = cells([(0, 24, "template", [1])], [(0, 24, "template", [1])])
    r = screen_live(c, datetime(2026, 10, 28, 14, 30), bc(datetime(2026, 10, 30, 3, 0)), True)
    assert r["now"]["until"] == "2026-10-30T03:00:00"
    assert r["after_broadcast"] is None


def test_screen_live_broadcast_hides_schedule_changes():
    # при эфире «дальше» нет — смены расписания в это время перекрыты; «до» — срок эфира
    c = cells([(0, 12, "template", [1]), (12, 24, "placement", [2])])
    r = screen_live(c, datetime(2026, 10, 28, 10, 0), bc(datetime(2026, 10, 28, 18, 0)), True)
    assert r["now"]["until"] == "2026-10-28T18:00:00" and r["next"] is None
    assert r["after_broadcast"] == {"kind": "placement", "playlist_ids": [2], "playlists": ["PL2"]}


def test_screen_live_broadcast_unplayable_falls_to_schedule():
    c = cells([(0, 13, "template", [1]), (13, 15, "placement", [2]), (15, 24, "template", [1])])
    r = screen_live(c, datetime(2026, 10, 28, 10, 30), bc(datetime(2026, 10, 28, 15, 0)), False)
    assert r["broadcast_skipped"] is True
    # на экране идёт расписание, а не эфир
    assert r["now"] == {"kind": "template", "playlist_ids": [1], "playlists": ["PL1"], "until": "2026-10-28T13:00:00"}
    # эфир на этом экране не идёт — экран живёт по расписанию, поэтому «дальше» — его ближайшая смена
    assert r["next"] == {"kind": "placement", "playlist_ids": [2], "playlists": ["PL2"], "from": "2026-10-28T13:00:00"}
    assert r["after_broadcast"] == {"kind": "template", "playlist_ids": [1], "playlists": ["PL1"]}


def test_screen_live_skipped_screen_next_equals_next_without_broadcast():
    c = cells([(0, 13, "template", [1]), (13, 15, "placement", [2]), (15, 24, "template", [1])])
    now = datetime(2026, 10, 28, 10, 30)
    skipped = screen_live(c, now, bc(datetime(2026, 10, 28, 15, 0)), False)
    without = screen_live(c, now, None, True)
    assert skipped["next"] == without["next"] and skipped["next"] is not None
    assert skipped["now"] == without["now"]


def test_screen_live_skipped_screen_without_further_changes_has_no_next():
    c = cells([(0, 24, "template", [1])])
    r = screen_live(c, datetime(2026, 10, 28, 10, 30), bc(None), False)
    assert r["broadcast_skipped"] is True and r["next"] is None


def test_screen_live_playing_broadcast_still_hides_next():
    c = cells([(0, 13, "template", [1]), (13, 24, "placement", [2])])
    r = screen_live(c, datetime(2026, 10, 28, 10, 30), bc(datetime(2026, 10, 28, 18, 0)), True)
    assert r["broadcast_skipped"] is False and r["next"] is None


def test_screen_live_broadcast_unplayable_until_cancel():
    c = cells([(0, 13, "template", [1]), (13, 24, "placement", [2])])
    r = screen_live(c, datetime(2026, 10, 28, 14, 0), bc(None), False)
    assert r["broadcast_skipped"] is True
    assert r["now"] == {"kind": "placement", "playlist_ids": [2], "playlists": ["PL2"], "until": None}
    assert r["after_broadcast"] == {"kind": "placement", "playlist_ids": [2], "playlists": ["PL2"]}


def test_screen_live_broadcast_unplayable_nothing_scheduled():
    c = cells([])
    r = screen_live(c, datetime(2026, 10, 28, 14, 0), bc(None), False)
    assert r["broadcast_skipped"] is True
    assert r["now"] == {"kind": "empty", "playlist_ids": [], "playlists": [], "until": None}
    assert r["after_broadcast"] == {"kind": "empty", "playlist_ids": [], "playlists": []}


def test_screen_live_no_broadcast_ignores_playable_flag():
    c = cells([(0, 24, "template", [1])])
    r = screen_live(c, datetime(2026, 10, 28, 14, 0), None, True)
    assert r["now"] == {"kind": "template", "playlist_ids": [1], "playlists": ["PL1"], "until": None}
    assert r["after_broadcast"] is None and r["broadcast_skipped"] is False


def test_screen_live_switched_off_broadcast_dict_is_not_a_broadcast():
    off = {"is_on": False, "playlist_id": None, "playlist_name": None, "until_at": None,
           "enabled_by": None, "enabled_at": None}
    c = cells([(0, 13, "template", [1]), (13, 24, "placement", [2])])
    r = screen_live(c, datetime(2026, 10, 28, 10, 0), off, False)
    assert r["now"]["kind"] == "template" and r["next"]["kind"] == "placement"
    assert r["after_broadcast"] is None and r["broadcast_skipped"] is False


def test_screen_live_broadcast_name_falls_back_when_missing():
    c = cells([(0, 24, "template", [1])])
    r = screen_live(c, datetime(2026, 10, 28, 10, 0), bc(None, playlist_id=9, name=None), True)
    assert r["now"]["playlists"] == ["Плейлист #9"]


# --- screen_live поверх настоящей week_cells --------------------------------------

def test_screen_live_over_real_week_cells():
    placement = {"id": 1, "playlist_id": 2, "playlist_name": "Утро", "date_from": D, "date_to": D2,
                 "hour_from": 13, "hour_to": 15, "stopped_at": None, "screens": {5}}
    slots = [{"day_of_week": d, "hour": None, "playlist_id": 1, "prio": 1} for d in range(7)]
    c = week_cells([D, D2], 5, [placement], {}, [], slots, {1: "Общий", 2: "Утро"})
    r = screen_live(c, datetime(2026, 10, 28, 10, 30), None, False)
    assert r["now"] == {"kind": "template", "playlist_ids": [1], "playlists": ["Общий"], "until": "2026-10-28T13:00:00"}
    assert r["next"] == {"kind": "placement", "playlist_ids": [2], "playlists": ["Утро"], "from": "2026-10-28T13:00:00"}
    r = screen_live(c, datetime(2026, 10, 28, 14, 0), bc(datetime(2026, 10, 29, 0, 0)), True)
    assert r["after_broadcast"] == {"kind": "template", "playlist_ids": [1], "playlists": ["Общий"]}


# --- week_cells(playable=…) --------------------------------------------------------

DAYS = [D, D2]
ALL_DAY_1 = [{"day_of_week": d, "hour": None, "playlist_id": 1, "prio": 1} for d in range(7)]


def P(id, h1, h2, pl, screens=(5,), saved=None):
    p = {"id": id, "playlist_id": pl, "playlist_name": f"PL{pl}", "date_from": D, "date_to": D2,
         "hour_from": h1, "hour_to": h2, "stopped_at": None, "screens": set(screens)}
    if saved is not None:
        p["saved_at"] = saved
    return p


def OV(id, on_date, pl=30, off=False):
    return {"id": id, "screen_id": 5, "on_date": on_date,
            "playlist_id": None if off else pl, "playlist_name": None if off else f"PL{pl}", "is_off": off}


def grid(placements, decisions=None, overrides=(), slots=ALL_DAY_1, playable=None, names=None):
    out = week_cells(DAYS, 5, list(placements), decisions or {}, list(overrides), list(slots),
                     names if names is not None else {1: "Общий"}, playable)
    return {(c["date"], c["hour"]): c for c in out}


def test_week_cells_playable_filters_placements():
    # плейлист показа (10) без проигрываемых роликов — час проваливается к шаблону
    c = grid([P(7, 9, 13, 10)], playable={1})
    cell = c[(D, 10)]
    assert (cell["kind"], cell["playlist_ids"], cell["playlists"], cell["placement_id"]) == \
        ("template", [1], ["Общий"], None)
    assert all(c[(D, h)]["kind"] == "template" for h in range(24))


def test_week_cells_playable_keeps_playable_placement():
    c = grid([P(7, 9, 13, 10)], playable={1, 10})
    cell = c[(D, 10)]
    assert (cell["kind"], cell["playlist_ids"], cell["placement_id"]) == ("placement", [10], 7)
    assert c[(D, 13)]["kind"] == "template" and c[(D, 8)]["kind"] == "template"


def test_week_cells_playable_none_is_the_old_behaviour():
    placements = [P(7, 9, 13, 10), P(8, 11, 15, 20, saved=datetime(2026, 10, 28, 12, 0))]
    decisions = {(8, 5, "placement", 7): "alternate"}
    overrides = [OV(3, D2, pl=30)]
    base = week_cells(DAYS, 5, placements, decisions, overrides, ALL_DAY_1, {})
    assert week_cells(DAYS, 5, placements, decisions, overrides, ALL_DAY_1, {}, None) == base
    assert week_cells(DAYS, 5, placements, decisions, overrides, ALL_DAY_1, {}, playable={1, 10, 20, 30}) == base


def test_week_cells_playable_unplayable_placement_falls_to_override_with_playlist():
    # порядок как в ленте: показ (пустой) → переопределение с плейлистом → шаблон
    c = grid([P(7, 9, 13, 10)], overrides=[OV(3, D, pl=30)], playable={1, 30})
    cell = c[(D, 10)]
    assert (cell["kind"], cell["playlist_ids"], cell["playlists"], cell["placement_id"]) == \
        ("override", [30], ["PL30"], None)
    assert c[(D, 14)]["kind"] == "override"                  # переопределение на весь день
    assert c[(D2, 10)]["kind"] == "template"                 # завтра переопределения нет


def test_week_cells_playable_off_override_still_beats_everything():
    c = grid([P(7, 9, 13, 10)], overrides=[OV(3, D, off=True)], playable=set())
    assert all(c[(D, h)]["kind"] == "off" and c[(D, h)]["playlist_ids"] == [] for h in range(24))


def test_week_cells_playable_empty_set_drops_all_placements():
    c = grid([P(7, 9, 13, 10)], playable={1})                # играет только шаблон
    assert all(cell["kind"] == "template" for cell in c.values())
    c = grid([P(7, 9, 13, 10)], slots=[], playable=set())    # шаблона нет — пусто
    assert c[(D, 10)]["kind"] == "empty" and c[(D, 10)]["playlist_ids"] == []
    c = grid([P(7, 9, 13, 10)], playable=set())              # нечего играть ни показу, ни шаблону — заглушки
    assert all(cell["kind"] == "empty" and cell["playlist_ids"] == [] for cell in c.values())


def test_week_cells_playable_alternate_with_one_unplayable_becomes_single():
    older = P(1, 9, 13, 10, saved=datetime(2026, 10, 27))
    newer = P(2, 9, 13, 20, saved=datetime(2026, 10, 28))
    decisions = {(2, 5, "placement", 1): "alternate"}
    both = grid([older, newer], decisions, playable={1, 10, 20})[(D, 10)]
    assert (both["kind"], both["playlist_ids"], both["placement_id"]) == ("alternate", [20, 10], 2)
    # вершина (20) пуста — остаётся старший показ, он и считается вершиной часа
    only_old = grid([older, newer], decisions, playable={1, 10})[(D, 10)]
    assert (only_old["kind"], only_old["playlist_ids"], only_old["playlists"], only_old["placement_id"]) == \
        ("placement", [10], ["PL10"], 1)
    # пуст старший — играет вершина одна
    only_new = grid([older, newer], decisions, playable={1, 20})[(D, 10)]
    assert (only_new["kind"], only_new["playlist_ids"], only_new["placement_id"]) == ("placement", [20], 2)
    # пусты оба — шаблон
    none = grid([older, newer], decisions, playable={1})[(D, 10)]
    assert (none["kind"], none["playlist_ids"], none["placement_id"]) == ("template", [1], None)


def test_week_cells_playable_override_alternating_with_placement():
    p = P(1, 9, 13, 10)
    decisions = {(1, 5, "override", 3): "alternate"}
    ov = OV(3, D, pl=30)
    both = grid([p], decisions, [ov], playable={1, 10, 30})[(D, 10)]
    assert (both["kind"], both["playlist_ids"]) == ("alternate", [10, 30])
    # плейлист переопределения пуст — играет один показ
    only_p = grid([p], decisions, [ov], playable={1, 10})[(D, 10)]
    assert (only_p["kind"], only_p["playlist_ids"], only_p["placement_id"]) == ("placement", [10], 1)
    # пуст показ, а переопределение играет: как в ленте (pick_active) — один плейлист, показа-вершины нет
    only_ov = grid([p], decisions, [ov], playable={1, 30})[(D, 10)]
    assert (only_ov["kind"], only_ov["playlist_ids"], only_ov["playlists"], only_ov["placement_id"]) == \
        ("placement", [30], ["PL30"], None)


def test_week_cells_playable_other_screen_placements_unaffected():
    c = grid([P(7, 9, 13, 10, screens=(6,))], playable={1})
    assert all(cell["kind"] == "template" for cell in c.values())


# --- шаблон и замена на дату, которым нечего играть: как в ленте экрана ------------------
# Лента выбирает слот шаблона на (день, час) с наибольшим prio ДО фильтра роликов; если его
# плейлисту играть нечего, слот выпадает целиком (слот уровнем ниже на тот же час его не
# подменяет), и агент берёт слот «весь день», а без него — заглушки. Замена на дату с
# пустым плейлистом агентом пропускается так же — дальше шаблон.

def SLOT(hour, pl, prio, dow=D.weekday()):
    return {"day_of_week": dow, "hour": hour, "playlist_id": pl, "prio": prio}


def test_week_cells_unplayable_hour_slot_falls_to_all_day_slot():
    slots = [SLOT(10, 40, 3), SLOT(None, 1, 1)]
    c = grid([], slots=slots, playable={1})
    cell = c[(D, 10)]
    assert (cell["kind"], cell["playlist_ids"], cell["playlists"]) == ("template", [1], ["Общий"])
    assert grid([], slots=slots, playable={1, 40})[(D, 10)]["playlist_ids"] == [40]     # играет — как раньше


def test_week_cells_unplayable_top_slot_is_not_replaced_by_lower_level_same_hour():
    # экранный слот 10:00 пуст, групповой слот того же часа играл бы — но лента выбирает
    # вершину до фильтра и выбрасывает её: дальше «весь день», а не групповой
    slots = [SLOT(10, 40, 3), SLOT(10, 50, 2), SLOT(None, 1, 1)]
    assert grid([], slots=slots, playable={1, 50})[(D, 10)]["playlist_ids"] == [1]


def test_week_cells_unplayable_all_day_slot_leaves_the_hour_empty():
    c = grid([], slots=[SLOT(None, 1, 1)], playable=set())
    assert (c[(D, 10)]["kind"], c[(D, 10)]["playlist_ids"], c[(D, 10)]["playlists"]) == ("empty", [], [])
    # почасовой пуст и «весь день» пуст — тоже заглушки
    c = grid([], slots=[SLOT(10, 40, 3), SLOT(None, 1, 1)], playable=set())
    assert c[(D, 10)]["kind"] == "empty"


def test_week_cells_unplayable_override_falls_to_template():
    c = grid([], overrides=[OV(3, D, pl=30)], playable={1})
    cell = c[(D, 10)]
    assert (cell["kind"], cell["playlist_ids"], cell["placement_id"]) == ("template", [1], None)
    assert grid([], overrides=[OV(3, D, pl=30)], playable={1, 30})[(D, 10)]["kind"] == "override"


def test_week_cells_unplayable_placement_and_override_fall_to_template():
    c = grid([P(7, 9, 13, 10)], overrides=[OV(3, D, pl=30)], playable={1})
    assert (c[(D, 10)]["kind"], c[(D, 10)]["playlist_ids"]) == ("template", [1])


def test_week_cells_without_playable_template_and_override_are_unfiltered():
    # «Расписание → По датам» фильтр не передаёт: шаблон и замена — как записаны
    slots = [SLOT(10, 40, 3), SLOT(None, 1, 1)]
    assert grid([], slots=slots)[(D, 10)]["playlist_ids"] == [40]
    assert grid([], overrides=[OV(3, D, pl=30)])[(D, 10)]["kind"] == "override"
