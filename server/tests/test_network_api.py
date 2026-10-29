"""API обзора сети (server/routers/network.py): сборка ответа `/network/live`.

build_live — чистая сборка (без SQL): экраны, клетки сетки, состояние эфира, «проигрываемые
плейлисты» по типам площадки. SQL-часть (экраны, «играет ролик», проигрываемые плейлисты,
пакетная загрузка показов) проверяет smoke_test.sh на стенде; здесь — что она вызывается
один раз на всех, а не по разу на экран.

Все даты и часы — наивное МСК. Клетка — как в week_cells:
{"date", "hour", "kind", "playlist_ids", "playlists", "placement_id"}.
"""
from datetime import date, datetime

import pytest

from deps import is_path_allowed_for_advertiser
from routers import network
from routers.network import build_live
from screen_online import ONLINE_MINUTES

D = date(2026, 10, 28)        # «сегодня» в тестах (среда)
D2 = date(2026, 10, 29)       # «завтра»
NOW = datetime(2026, 10, 28, 10, 30)


def cells(spec_today=(), spec_tomorrow=()):
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


def scr(sid, name, online=True, venue="other", offline_for=10, playing=None, city="Тверь", location="Зал"):
    return {"id": sid, "name": name, "city": city, "location": location, "venue_type": venue,
            "online": online, "offline_for_seconds": offline_for, "playing": playing}


def bc(playlist_id=9, until_at=datetime(2026, 10, 28, 15, 0), name="Эфир"):
    """Действующий общий эфир в виде get_broadcast."""
    return {"is_on": True, "playlist_id": playlist_id, "playlist_name": name, "until_at": until_at,
            "enabled_by": "admin", "enabled_at": datetime(2026, 10, 28, 9, 0)}


BC_OFF = {"is_on": False, "playlist_id": None, "playlist_name": None, "until_at": None,
          "enabled_by": None, "enabled_at": None}

TEMPLATE_DAY = cells([(0, 24, "template", [1])])


def live(screens, cells_by_screen=None, broadcast=BC_OFF, playable=None):
    cells_by_screen = cells_by_screen if cells_by_screen is not None else {s["id"]: TEMPLATE_DAY for s in screens}
    return build_live(screens, cells_by_screen, NOW, broadcast, playable or {})


# --- build_live: общий вид ---------------------------------------------------

def test_screens_sorted_by_name():
    r = live([scr(3, "Ярославль"), scr(1, "Берёза"), scr(2, "апельсин"), scr(4, "Берёза")])
    assert [(s["name"], s["id"]) for s in r["screens"]] == [
        ("апельсин", 2), ("Берёза", 1), ("Берёза", 4), ("Ярославль", 3)]


def test_sort_treats_yo_as_ye():
    r = live([scr(1, "Яблоко"), scr(2, "Жук"), scr(3, "Ёлка"), scr(4, "Дом")])
    assert [s["name"] for s in r["screens"]] == ["Дом", "Ёлка", "Жук", "Яблоко"]


def test_header_fields():
    r = live([scr(1, "А")])
    assert r["generated_at"] == "2026-10-28T10:30:00"
    assert r["online_minutes"] == ONLINE_MINUTES == 5
    assert set(r) == {"generated_at", "online_minutes", "counts", "broadcast", "screens", "playlists"}


def test_counts_total_online_offline():
    r = live([scr(1, "А"), scr(2, "Б", online=False), scr(3, "В", online=False, offline_for=None), scr(4, "Г")])
    assert r["counts"] == {"total": 4, "online": 2, "offline": 2}   # offline — и «ни разу не выходил на связь»


def test_empty_network():
    r = live([])
    assert r["counts"] == {"total": 0, "online": 0, "offline": 0}
    assert r["screens"] == [] and r["playlists"] == {}
    assert r["broadcast"]["screens_total"] == 0


def test_screen_row_shape():
    playing = {"media_id": 5, "title": "Ролик", "filename": "r.mp4"}
    s = scr(7, "Касса", online=False, offline_for=4000, playing=playing, city="Тверь", location="Вход")
    row = live([s])["screens"][0]
    assert set(row) == {"id", "name", "city", "location", "online", "offline_for_seconds", "playing",
                        "now", "next", "after_broadcast", "broadcast_skipped"}
    assert (row["id"], row["name"], row["city"], row["location"]) == (7, "Касса", "Тверь", "Вход")
    assert row["online"] is False and row["offline_for_seconds"] == 4000
    assert row["playing"] == playing
    assert row["now"] == {"kind": "template", "playlist_ids": [1], "playlists": ["PL1"], "until": None}
    assert row["next"] is None and row["after_broadcast"] is None and row["broadcast_skipped"] is False


def test_never_seen_screen_has_null_offline_for():
    row = live([scr(1, "А", online=False, offline_for=None)])["screens"][0]
    assert row["online"] is False and row["offline_for_seconds"] is None


def test_screen_without_cells_is_empty():
    r = live([scr(1, "А")], cells_by_screen={})
    assert r["screens"][0]["now"] == {"kind": "empty", "playlist_ids": [], "playlists": [], "until": None}


def test_now_and_next_come_from_the_screens_cells():
    c = cells([(0, 13, "template", [1]), (13, 15, "placement", [2]), (15, 24, "template", [1])])
    r = live([scr(1, "А")], cells_by_screen={1: c})
    row = r["screens"][0]
    assert row["now"]["kind"] == "template" and row["now"]["until"] == "2026-10-28T13:00:00"
    assert row["next"] == {"kind": "placement", "playlist_ids": [2], "playlists": ["PL2"],
                           "from": "2026-10-28T13:00:00"}


# --- общий эфир --------------------------------------------------------------

def test_broadcast_off_zero_counters():
    r = live([scr(1, "А"), scr(2, "Б", online=False)], broadcast=BC_OFF)
    assert r["broadcast"] == {"is_on": False, "playlist_id": None, "playlist_name": None, "until_at": None,
                              "enabled_by": None, "enabled_at": None,
                              "screens_total": 2, "offline_screens": 0, "unplayable_screens": 0}


def test_broadcast_state_is_json_ready():
    r = live([scr(1, "А")], broadcast=bc(), playable={"other": {9}})
    b = r["broadcast"]
    assert (b["is_on"], b["playlist_id"], b["playlist_name"], b["enabled_by"]) == (True, 9, "Эфир", "admin")
    assert b["until_at"] == "2026-10-28T15:00:00" and b["enabled_at"] == "2026-10-28T09:00:00"


def test_broadcast_until_cancelled_is_null():
    r = live([scr(1, "А")], broadcast=bc(until_at=None), playable={"other": {9}})
    assert r["broadcast"]["until_at"] is None
    assert r["screens"][0]["now"] == {"kind": "broadcast", "playlist_ids": [9], "playlists": ["Эфир"], "until": None}


def test_offline_screens_counted_only_while_broadcast_effective():
    screens = [scr(1, "А"), scr(2, "Б", online=False), scr(3, "В", online=False, offline_for=None)]
    on = live(screens, broadcast=bc(), playable={"other": {9}})
    off = live(screens, broadcast=BC_OFF, playable={"other": {9}})
    assert on["broadcast"]["offline_screens"] == 2 and on["counts"]["offline"] == 2
    assert off["broadcast"]["offline_screens"] == 0 and off["counts"]["offline"] == 2


def test_unplayable_screens_count_broadcast_skipped():
    # плейлист эфира играет только на площадке «магазин с алколицензией»; на «other» ему нечего играть
    screens = [scr(1, "А", venue="store_alcohol"), scr(2, "Б", venue="other"), scr(3, "В", venue="other")]
    r = live(screens, broadcast=bc(), playable={"store_alcohol": {9}, "other": {1}})
    assert r["broadcast"]["unplayable_screens"] == 2 and r["broadcast"]["screens_total"] == 3
    by_id = {s["id"]: s for s in r["screens"]}
    assert by_id[1]["now"]["kind"] == "broadcast" and by_id[1]["broadcast_skipped"] is False
    assert by_id[2]["now"]["kind"] == "template" and by_id[2]["broadcast_skipped"] is True
    assert by_id[3]["broadcast_skipped"] is True


def test_venue_without_playable_set_cannot_play_broadcast():
    r = live([scr(1, "А", venue="unknown")], broadcast=bc(), playable={"other": {9}})
    assert r["screens"][0]["broadcast_skipped"] is True and r["broadcast"]["unplayable_screens"] == 1


def test_no_broadcast_nothing_skipped():
    r = live([scr(1, "А")], broadcast=BC_OFF, playable={})
    assert r["screens"][0]["broadcast_skipped"] is False and r["broadcast"]["unplayable_screens"] == 0


def test_broadcast_after_broadcast_and_no_next():
    c = cells([(0, 12, "template", [1]), (12, 24, "placement", [2])])
    r = live([scr(1, "А")], cells_by_screen={1: c}, broadcast=bc(until_at=datetime(2026, 10, 28, 13, 0)),
             playable={"other": {9}})
    row = r["screens"][0]
    assert row["now"]["kind"] == "broadcast" and row["now"]["until"] == "2026-10-28T13:00:00"
    assert row["next"] is None
    assert row["after_broadcast"] == {"kind": "placement", "playlist_ids": [2], "playlists": ["PL2"]}


def test_offline_screen_still_gets_broadcast_row():
    # не в сети — «получит при подключении»: строка эфира есть, считаем его как у остальных
    r = live([scr(1, "А", online=False)], broadcast=bc(), playable={"other": {9}})
    assert r["screens"][0]["now"]["kind"] == "broadcast" and r["screens"][0]["online"] is False


# --- playlists ---------------------------------------------------------------

def test_playlists_contain_every_id_from_now_next_after():
    c = cells([(0, 13, "template", [1]), (13, 15, "alternate", [2, 3]), (15, 24, "template", [1])])
    plain = live([scr(1, "А")], cells_by_screen={1: c})
    assert plain["playlists"] == {"1": {"name": "PL1"}, "2": {"name": "PL2"}, "3": {"name": "PL3"}}

    c2 = cells([(0, 24, "template", [1])], [(0, 24, "placement", [4])])
    with_bc = live([scr(1, "А"), scr(2, "Б", venue="x")], cells_by_screen={1: c2, 2: c2},
                   broadcast=bc(until_at=datetime(2026, 10, 29, 3, 0)), playable={"other": {9}})
    # у экрана 1 — эфир (9), после него — завтрашний показ (4); у экрана 2 эфир пропущен: идёт шаблон (1)
    ids = set(with_bc["playlists"])
    assert ids == {"1", "4", "9"}
    assert with_bc["playlists"]["9"] == {"name": "Эфир"} and with_bc["playlists"]["4"] == {"name": "PL4"}
    for row in with_bc["screens"]:
        for part in ("now", "next", "after_broadcast"):
            for pid in (row[part] or {}).get("playlist_ids", []):
                assert str(pid) in with_bc["playlists"]


def test_playlists_only_what_the_page_shows():
    # плейлист, который стоит только в клетках после «сейчас/дальше», в ответ не попадает
    c = cells([(0, 24, "template", [1])], [(0, 24, "placement", [5])])
    r = live([scr(1, "А")], cells_by_screen={1: c})
    assert r["playlists"] == {"1": {"name": "PL1"}}


# --- «играет ролик» -----------------------------------------------------------

def test_playing_none_without_file():
    assert network.playing_of(None, None, None) is None
    assert network.playing_of("", None, None) is None


def test_playing_matched_media():
    assert network.playing_of("a.mp4", 5, "Ролик") == {"media_id": 5, "title": "Ролик", "filename": "a.mp4"}


def test_playing_file_without_media_keeps_filename():
    assert network.playing_of("gone.mp4", None, None) == {"media_id": None, "title": None, "filename": "gone.mp4"}


# --- доступ ------------------------------------------------------------------

@pytest.mark.parametrize("path", ["/network/live", "/network/playlist/3/usage"])
def test_advertiser_cannot_open_network(path):
    assert is_path_allowed_for_advertiser(path) is False


def test_routes_registered_for_admin():
    paths = {r.path: r for r in network.router.routes}
    assert {"/network/live", "/network/playlist/{playlist_id}/usage"} <= set(paths)
    from deps import get_current_admin
    for p in ("/network/live", "/network/playlist/{playlist_id}/usage"):
        deps = [d.call for d in paths[p].dependant.dependencies]
        assert get_current_admin in deps


# --- сборка из БД: пакетно ---------------------------------------------------

class Calls:
    def __init__(self):
        self.log = []

    def rec(self, name, result):
        def f(*a, **kw):
            self.log.append((name, a[1:], kw))
            return result(*a, **kw) if callable(result) else result
        return f


def test_snapshot_batches_queries_and_uses_one_now(monkeypatch):
    calls = Calls()
    screens = [scr(1, "Б", venue="other"), scr(2, "А", venue="other"), scr(3, "В", venue="store_alcohol")]
    placement = {"id": 11, "playlist_id": 2, "playlist_name": "PL2", "date_from": D, "date_to": D,
                 "hour_from": 10, "hour_to": 12, "stopped_at": None, "saved_at": datetime(2026, 10, 27, 9, 0),
                 "screens": {1, 3}, "all_screens": False}
    seen_cells = {}

    def fake_week_cells(days, screen_id, placements, decisions, overrides, slots, names, playable=None):
        seen_cells[screen_id] = {"days": days, "playable": playable, "slots": slots, "names": names}
        return TEMPLATE_DAY

    monkeypatch.setattr(network, "now_msk", calls.rec("now", NOW))
    monkeypatch.setattr(network, "get_broadcast", calls.rec("broadcast", BC_OFF))
    monkeypatch.setattr(network, "load_screens", calls.rec("screens", screens))
    monkeypatch.setattr(network, "load_playable_by_venue", calls.rec("playable", lambda db, venues: {v: {2} for v in venues}))
    monkeypatch.setattr(network, "load_placements", calls.rec("placements", [placement]))
    monkeypatch.setattr(network, "load_overrides", calls.rec("overrides", []))
    monkeypatch.setattr(network, "load_decisions", calls.rec("decisions", {}))
    monkeypatch.setattr(network, "load_screen_slots", calls.rec("slots", lambda db, sid: [{"playlist_id": 1}]))
    monkeypatch.setattr(network, "load_playlist_names", calls.rec("names", {1: "PL1", 2: "PL2"}))
    monkeypatch.setattr(network, "week_cells", fake_week_cells)

    r = network.live_snapshot(object())

    names = [c[0] for c in calls.log]
    assert names.count("now") == 1 and names.count("broadcast") == 1
    assert names.count("screens") == 1 and names.count("placements") == 1
    assert names.count("overrides") == 1 and names.count("decisions") == 1
    assert names.count("playable") == 1 and names.count("names") == 1        # на всех, не на экран
    assert names.count("slots") == 3                                       # слоты — по экрану (допустимо)
    playable_call = next(c for c in calls.log if c[0] == "playable")
    assert set(playable_call[1][0]) == {"other", "store_alcohol"}          # по разу на тип площадки
    # окно — сегодня и завтра от одного now; показы грузятся на то же окно
    assert all(v["days"] == [D, D2] for v in seen_cells.values())
    assert next(c for c in calls.log if c[0] == "placements")[1] == (D, D2)
    assert seen_cells[1]["playable"] == {2} and seen_cells[3]["playable"] == {2}
    assert r["generated_at"] == "2026-10-28T10:30:00"
    assert [s["name"] for s in r["screens"]] == ["А", "Б", "В"]
