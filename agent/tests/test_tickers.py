"""Агент 1.4.0: очередь бегущих строк из ленты экрана.

Агент берёт строки из ленты (`tickers`), сам знает их срок (наивное МСК) и
крутит по очереди: по одному полному проходу каждой живой строки. Поток mpv в
тестах не запускается: рисование заменено записью команд (`_tk_send`), время —
подменой часов (`_ticker_clock`) и аргументами шага `_ticker_step(mono, wall)`.
Проверяются чистые функции, состояние и шаги очереди.

Модули агента импортируют requests; в тестовом образе его нет — подкладываем
пустой модуль, а объекты с сетевой сессией собираем минуя __init__.
"""
import copy
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    import requests  # noqa: F401
except ImportError:
    sys.modules["requests"] = types.ModuleType("requests")

import ds_heartbeat                                          # noqa: E402
from ds_player import VLCPlayer, live_tickers, next_ticker_index   # noqa: E402
from ds_sync import SyncHandler, drain_commands              # noqa: E402

NOW = datetime(2026, 11, 2, 14, 0, 0)          # наивное МСК


def tk(tid, message="Текст", until=None, color="#ffd34d", speed="medium"):
    return {"id": tid, "message": message, "color": color, "speed": speed, "until_at": until}


def ids(items):
    return [t["id"] for t in items]


@pytest.fixture
def player(tmp_path):
    """Плеер без mpv: рисование пишется в p.sent, часы агента — p.clock[0]."""
    p = VLCPlayer(media_dir=str(tmp_path))
    p.sent = []
    p.send_ok = True
    p.clock = [NOW]

    def fake_send(command):
        if p.send_ok:
            p.sent.append(command)
        return p.send_ok

    p._tk_send = fake_send
    p._ticker_clock = lambda: p.clock[0]
    return p


def step(p, mono):
    return p._ticker_step(mono, p.clock[0])


def drawn(p):
    """Что нарисовано: [(x, текст)] для кадров и 'hide' для снятия оверлея."""
    out = []
    for cmd in p.sent:
        if cmd[2] == "none":
            out.append("hide")
        else:
            tags, _, text = cmd[3].partition("}")
            x = float(tags.split("\\pos(")[1].split(",")[0])
            out.append((x, text))
    return out


# --- live_tickers: что живо по часам агента --------------------------------

def test_live_tickers_drops_expired_keeps_order():
    items = [tk(1, until="2026-11-02T13:59:00"), tk(2, until=None), tk(3, until="2026-11-02T18:00:00")]
    assert ids(live_tickers(items, NOW)) == [2, 3]


def test_live_tickers_uses_given_now_not_server_time():
    item = [tk(7, until="2026-11-02T15:00:00")]
    assert ids(live_tickers(item, datetime(2026, 11, 2, 14, 59, 59))) == [7]
    assert live_tickers(item, datetime(2026, 11, 2, 15, 0, 0)) == []


def test_live_tickers_accepts_aware_now():
    aware = datetime(2026, 11, 2, 14, 0, tzinfo=timezone(timedelta(hours=3)))
    assert ids(live_tickers([tk(1, until="2026-11-02T14:30:00")], aware)) == [1]


def test_live_tickers_skips_garbage():
    items = [
        {"message": "без id", "until_at": None},
        tk(2, until="завтра"),
        tk(3, until=12345),
        "строка", None, 5, ["x"],
        {"id": "4", "message": "id не число"},
        {"id": True, "message": "id булево"},
        {"id": 5, "message": ""},
        {"id": 6, "message": "   \n "},
        {"id": 7, "message": 123},
        tk(8),
    ]
    assert ids(live_tickers(items, NOW)) == [8]


def test_live_tickers_not_a_list_is_empty():
    assert live_tickers(None, NOW) == []
    assert live_tickers({"id": 1}, NOW) == []


# --- какую строку показывать следующей --------------------------------------

def test_next_index_cycles_after_last():
    live = [tk(1), tk(2), tk(3)]
    assert next_ticker_index(live, None, 0) == 0
    assert next_ticker_index(live, 1, 0) == 1
    assert next_ticker_index(live, 2, 1) == 2
    assert next_ticker_index(live, 3, 2) == 0


def test_next_index_single_ticker_repeats():
    assert next_ticker_index([tk(9)], 9, 0) == 0


def test_next_index_removed_last_takes_its_place():
    # прошла строка 2 (на месте 1), её сняли: следом идёт та, что встала на её место
    assert next_ticker_index([tk(1), tk(3), tk(4)], 2, 1) == 1
    # сняли последнюю — круг начинается заново
    assert next_ticker_index([tk(1), tk(3)], 4, 2) == 0


def test_next_index_empty():
    assert next_ticker_index([], 1, 0) is None


# --- состояние для heartbeat ----------------------------------------------------

def test_ticker_state_paused_reports_no_ids(player):
    player.set_tickers([tk(1), tk(2)])
    player.set_tickers_paused(True, True, False)
    assert player.ticker_state() == {"ids": [], "stopped": True, "screen_off": False}


def test_ticker_state_screen_off_reports_flag(player):
    player.set_tickers([tk(1)])
    player.set_tickers_paused(True, False, True)
    assert player.ticker_state() == {"ids": [], "stopped": False, "screen_off": True}


def test_ticker_state_running_reports_live_ids(player):
    player.set_tickers([tk(1, until="2026-11-02T14:10:00"), tk(2), tk(3, until="2026-11-02T13:00:00")])
    player.set_tickers_paused(False, False, False)
    assert player.ticker_state() == {"ids": [1, 2], "stopped": False, "screen_off": False}
    player.clock[0] = datetime(2026, 11, 2, 14, 10, 0)         # срок первой вышел по часам агента
    assert player.ticker_state()["ids"] == [2]


def test_ticker_state_no_tickers(player):
    assert player.ticker_state() == {"ids": [], "stopped": False, "screen_off": False}


def test_resume_after_pause_reports_ids_again(player):
    player.set_tickers([tk(1)])
    player.set_tickers_paused(True, True, False)
    player.set_tickers_paused(False, False, False)
    assert player.ticker_state()["ids"] == [1]


# --- лента: ключ tickers и режим «из ленты» (правило R6) ----------------------------

def test_feed_key_replaces_list_and_enters_feed_mode(player):
    assert player.tickers_from_feed is False
    assert player.apply_feed_tickers({"files": [], "tickers": [tk(1), tk(2)]}) is True
    assert player.tickers_from_feed is True
    assert player.ticker_state()["ids"] == [1, 2]


def test_feed_without_key_keeps_list(player):
    player.apply_feed_tickers({"tickers": [tk(1)]})
    assert player.apply_feed_tickers({"files": []}) is False
    assert player.ticker_state()["ids"] == [1]


def test_feed_empty_list_replaces(player):
    player.apply_feed_tickers({"tickers": [tk(1)]})
    assert player.apply_feed_tickers({"tickers": []}) is True
    assert player.ticker_state()["ids"] == []


def test_feed_mode_stays_after_key_disappears(player):
    player.apply_feed_tickers({"tickers": [tk(1)]})
    player.apply_feed_tickers({"files": []})            # сервер на миг не дал ключ
    assert player.tickers_from_feed is True


def test_feed_without_key_never_enters_feed_mode(player):
    player.apply_feed_tickers({"files": []})
    assert player.tickers_from_feed is False


@pytest.mark.parametrize("bad", [None, "x", 5, {"id": 1}])
def test_feed_key_not_a_list_is_ignored(player, bad):
    player.apply_feed_tickers({"tickers": [tk(1)]})
    assert player.apply_feed_tickers({"tickers": bad}) is False
    assert player.ticker_state()["ids"] == [1]
    fresh = VLCPlayer(media_dir=str(player.media_dir))
    assert fresh.apply_feed_tickers({"tickers": bad}) is False
    assert fresh.tickers_from_feed is False


def test_feed_not_a_dict_is_ignored(player):
    assert player.apply_feed_tickers(None) is False
    assert player.apply_feed_tickers([]) is False


def test_first_feed_removes_legacy_marquee(player):
    cleared = []
    player.clear_marquee = lambda: cleared.append(1)
    player._marquee_thread = object()                   # от прежней команды ticker_show
    player.apply_feed_tickers({"tickers": []})
    assert cleared == [1]
    player.apply_feed_tickers({"tickers": []})          # второй раз — уже не трогаем
    assert cleared == [1]


def test_first_feed_without_legacy_marquee_touches_nothing(player):
    player.clear_marquee = lambda: pytest.fail("clear_marquee не нужен")
    player.apply_feed_tickers({"tickers": [tk(1)]})


# --- очередь: шаги потока ----------------------------------------------------------

def test_step_without_tickers_draws_nothing(player):
    """Старый режим: очередь пуста и ничего не рисовала — оверлей set_marquee не трогаем."""
    assert step(player, 0.0) > 0.1
    assert player.sent == []


def test_step_draws_first_ticker_from_right_edge(player):
    player.set_tickers([tk(1, "Первая"), tk(2, "Вторая")])
    step(player, 100.0)
    assert drawn(player) == [(1280.0, "Первая")]
    step(player, 101.0)
    x, text = drawn(player)[-1]
    assert text == "Первая" and x == pytest.approx(1280 - 140)       # medium = 140 px/с


def test_step_frame_command_matches_old_marquee_look(player):
    player.set_tickers([tk(1, "Привет", color="#112233", speed="fast")])
    step(player, 0.0)
    assert player.sent == [[
        "osd-overlay", 47, "ass-events",
        "{\\an1\\pos(1280.0,708)\\fs42\\c&H332211&\\bord2\\3c&H000000&\\shad1}Привет", 1280, 720]]


def test_step_rotates_one_full_pass_each(player):
    player.set_tickers([tk(1, "A", speed="fast"), tk(2, "B", speed="fast")])
    span = VLCPlayer._marquee_span("A")
    pass_s = span / 220.0
    step(player, 0.0)
    step(player, pass_s - 0.1)
    assert drawn(player)[-1][1] == "A"                      # проход ещё не закончен
    step(player, pass_s + 0.01)
    assert drawn(player)[-1][1] == "B"                      # следом вторая, с правого края
    assert drawn(player)[-1][0] == pytest.approx(1280.0)    # без пропуска кадра
    step(player, 2 * pass_s + 0.02)
    assert drawn(player)[-1][1] == "A"                      # и по кругу


def test_step_single_ticker_repeats_passes(player):
    player.set_tickers([tk(1, "A", speed="fast")])
    pass_s = VLCPlayer._marquee_span("A") / 220.0
    step(player, 0.0)
    step(player, pass_s + 0.01)
    assert drawn(player)[-1] == (pytest.approx(1280.0), "A")


def test_step_removed_ticker_disappears_next_step(player):
    player.set_tickers([tk(1, "A"), tk(2, "B")])
    step(player, 0.0)
    player.set_tickers([tk(2, "B")])
    step(player, 0.03)
    assert drawn(player)[-1] == (pytest.approx(1280.0), "B")


def test_step_all_removed_hides_overlay_once(player):
    player.set_tickers([tk(1, "A")])
    step(player, 0.0)
    player.set_tickers([])
    step(player, 0.03)
    assert drawn(player)[-1] == "hide"
    n = len(player.sent)
    step(player, 0.06)
    step(player, 0.5)
    assert len(player.sent) == n                           # больше оверлей не трогаем


def test_step_edited_ticker_restarts_with_new_look(player):
    player.set_tickers([tk(1, "Старый", color="#ff0000")])
    step(player, 0.0)
    step(player, 1.0)
    player.set_tickers([tk(1, "Новый", color="#00ff00")])
    step(player, 1.03)
    cmd = player.sent[-1][3]
    assert cmd.endswith("}Новый") and "&H00FF00&" in cmd
    assert drawn(player)[-1][0] == pytest.approx(1280.0)   # начали заново с края


def test_step_unrelated_change_does_not_restart_current(player):
    player.set_tickers([tk(1, "A")])
    step(player, 0.0)
    step(player, 2.0)
    player.set_tickers([tk(1, "A"), tk(2, "B")])
    step(player, 2.03)
    assert drawn(player)[-1] == (pytest.approx(1280 - 2.03 * 140), "A")


def test_step_same_list_is_idempotent(player):
    items = [tk(1, "A"), tk(2, "B")]
    player.set_tickers(items)
    step(player, 0.0)
    player.set_tickers(copy.deepcopy(items))               # тот же список из новой загрузки ленты
    step(player, 3.0)
    assert drawn(player)[-1] == (pytest.approx(1280 - 3.0 * 140), "A")


def test_set_tickers_same_list_is_silent(player, caplog):
    """Лента читается каждую минуту: тот же список не должен каждый раз писать в лог."""
    items = [tk(1, "A")]
    with caplog.at_level("INFO", logger="ds_player"):
        player.set_tickers(items)
        player.set_tickers(copy.deepcopy(items))
        assert len([r for r in caplog.records if "Бегущие строки из ленты" in r.getMessage()]) == 1
        player.set_tickers([tk(1, "A"), tk(2, "B")])
        assert len([r for r in caplog.records if "Бегущие строки из ленты" in r.getMessage()]) == 2


def test_step_expired_by_agent_clock_drops_out(player):
    player.set_tickers([tk(1, "A", until="2026-11-02T14:00:10"), tk(2, "B")])
    step(player, 0.0)
    player.clock[0] = datetime(2026, 11, 2, 14, 0, 10)
    step(player, 10.0)
    assert drawn(player)[-1] == (pytest.approx(1280.0), "B")   # A выпала посреди прохода
    player.set_tickers([tk(1, "A", until="2026-11-02T14:00:15")])
    player.clock[0] = datetime(2026, 11, 2, 14, 0, 15)
    step(player, 15.0)
    assert drawn(player)[-1] == "hide"


def test_step_removed_mid_pass_continues_with_its_successor(player):
    player.set_tickers([tk(1, "A", speed="fast"), tk(2, "B", speed="fast"), tk(3, "C", speed="fast")])
    pass_s = VLCPlayer._marquee_span("A") / 220.0
    step(player, 0.0)
    step(player, pass_s + 0.01)                             # теперь идёт B
    player.set_tickers([tk(1, "A", speed="fast"), tk(3, "C", speed="fast")])
    step(player, pass_s + 0.04)                             # B снята
    assert drawn(player)[-1][1] == "C"                      # следом её преемница, а не A


def test_step_pause_hides_and_resume_shows(player):
    player.set_tickers([tk(1, "A")])
    step(player, 0.0)
    player.set_tickers_paused(True, True, False)
    step(player, 0.5)
    assert drawn(player)[-1] == "hide"
    n = len(player.sent)
    step(player, 1.0)
    assert len(player.sent) == n                            # на паузе ничего не рисуем
    player.set_tickers_paused(False, False, False)
    step(player, 1.5)
    assert drawn(player)[-1][1] == "A"


def test_step_pause_without_overlay_sends_nothing(player):
    player.set_tickers([tk(1, "A")])
    player.set_tickers_paused(True, False, True)
    step(player, 0.0)
    assert player.sent == []


def test_step_hide_failure_is_retried(player):
    player.set_tickers([tk(1, "A")])
    step(player, 0.0)
    player.set_tickers([])
    player.send_ok = False
    assert step(player, 0.1) > 0.2                          # mpv недоступен — пауза и повтор
    player.send_ok = True
    step(player, 0.6)
    assert drawn(player)[-1] == "hide"


def test_step_draw_failure_does_not_crash_and_retries(player):
    player.set_tickers([tk(1, "A")])
    player.send_ok = False
    assert step(player, 0.0) > 0.2
    player.send_ok = True
    step(player, 0.5)
    assert drawn(player)[-1][1] == "A"


def test_step_text_is_ass_escaped_and_single_line(player):
    player.set_tickers([tk(1, "{\\an8}Касса\nзакрыта")])
    step(player, 0.0)
    text = drawn(player)[-1][1]
    assert text == "(/an8)Касса закрыта"


def test_step_garbage_items_do_not_break_queue(player):
    player.set_tickers([None, {"id": 1}, tk(2, "B", until="мусор"), tk(3, "C")])
    step(player, 0.0)
    assert drawn(player)[-1][1] == "C"


# --- вид строки ------------------------------------------------------------------------

def test_marquee_text_is_ass_escaped():
    out = VLCPlayer._ass_escape("{\\an8}x")
    assert "{" not in out and "}" not in out and "\\" not in out


def test_marquee_overlay_command_format():
    p = VLCPlayer()
    assert p._marquee_overlay_cmd(10.25, "&H4DD3FF&", "текст") == [
        "osd-overlay", 47, "ass-events",
        "{\\an1\\pos(10.2,708)\\fs42\\c&H4DD3FF&\\bord2\\3c&H000000&\\shad1}текст", 1280, 720]


def test_marquee_span_matches_old_geometry():
    assert VLCPlayer._marquee_span("x" * 10) == 1280 + int(10 * 42 * 0.6) + 80


# --- команды ticker_show / ticker_hide ---------------------------------------------------

class FakePlayer:
    def __init__(self, from_feed):
        self.tickers_from_feed = from_feed
        self.calls = []
        self.user_stopped = False

    def set_marquee(self, *args):
        self.calls.append(("set_marquee",) + args)

    def clear_marquee(self):
        self.calls.append(("clear_marquee",))


def make_sync(player):
    h = object.__new__(SyncHandler)
    h.player = player
    h.refresh_requested = False
    h.acked = []
    h.ack_command = h.acked.append
    return h


def test_sync_ticker_show_requests_refresh_in_feed_mode():
    pl = FakePlayer(True)
    h = make_sync(pl)
    ok = h.handle({"id": 5, "type": "ticker_show",
                   "payload": {"message": "Привет", "color": "#fff", "speed": "slow", "duration": 60}})
    assert ok is True
    assert h.refresh_requested is True
    assert pl.calls == []
    assert h.acked == [5]


def test_sync_ticker_hide_requests_refresh_in_feed_mode():
    pl = FakePlayer(True)
    h = make_sync(pl)
    assert h.handle({"id": 6, "type": "ticker_hide", "payload": {}}) is True
    assert h.refresh_requested is True
    assert pl.calls == []
    assert h.acked == [6]


def test_sync_ticker_show_legacy_without_feed():
    pl = FakePlayer(False)
    h = make_sync(pl)
    h.handle({"id": 7, "type": "ticker_show",
              "payload": {"message": "Привет", "color": "#ff0000", "speed": "fast", "duration": 90}})
    assert pl.calls == [("set_marquee", "Привет", "#ff0000", "fast", 90)]
    assert h.refresh_requested is False
    assert h.acked == [7]


def test_sync_ticker_hide_legacy_without_feed():
    pl = FakePlayer(False)
    h = make_sync(pl)
    h.handle({"id": 8, "type": "ticker_hide", "payload": {}})
    assert pl.calls == [("clear_marquee",)]
    assert h.refresh_requested is False
    assert h.acked == [8]


# --- heartbeat -----------------------------------------------------------------------------

class FakeSession:
    def __init__(self):
        self.posts = []

    def post(self, url, json=None, timeout=None):
        self.posts.append((url, json))
        return types.SimpleNamespace(raise_for_status=lambda: None)


def make_heartbeat(tmp_path, monkeypatch):
    monkeypatch.setattr(ds_heartbeat, "DRM_PATH", str(tmp_path / "нет-drm"))
    hb = object.__new__(ds_heartbeat.Heartbeat)
    hb.server = "http://srv"
    hb.screen_id = 3
    hb.media_dir = str(tmp_path)
    hb.session = FakeSession()
    hb.os_version = "1.7"
    hb.vlc_version = "0.37"
    hb._last_display = "не читали"
    return hb


def test_heartbeat_sends_ticker_state(tmp_path, monkeypatch):
    hb = make_heartbeat(tmp_path, monkeypatch)
    state = {"ids": [4, 5], "stopped": False, "screen_off": False}
    assert hb.send(status="online", playing_file="a.mp4", ticker_state=state) is True
    url, body = hb.session.posts[-1]
    assert url == "http://srv/api/heartbeat/3"
    assert body["ticker_state"] == state
    assert body["agent_version"] == "1.4.0"
    assert body["playing_file"] == "a.mp4"


def test_heartbeat_without_ticker_state_has_no_field(tmp_path, monkeypatch):
    hb = make_heartbeat(tmp_path, monkeypatch)
    hb.send(status="online")
    _, body = hb.session.posts[-1]
    assert "ticker_state" not in body


def test_agent_version_is_1_4_0():
    assert ds_heartbeat.AGENT_VERSION == "1.4.0"


# --- пачка команд за пробуждение (правило R7) -------------------------------------------------

class FakeQueue:
    """Серверная очередь: poll отдаёт по одной команде; ack убирает её."""

    def __init__(self, cmds, ack=True):
        self.cmds = list(cmds)
        self.handled = []
        self.ack = ack

    def poll_command(self):
        return {"command": self.cmds[0] if self.cmds else None}

    def handle(self, cmd):
        self.handled.append(cmd["id"])
        if self.ack and self.cmds and self.cmds[0] is cmd:
            self.cmds.pop(0)
        return True


def cmd(cid, ctype="stop"):
    return {"id": cid, "type": ctype, "payload": {}}


def test_drain_handles_all_pending_commands():
    q = FakeQueue([cmd(1, "stop"), cmd(2, "ticker_hide")])
    assert drain_commands(q) == 2
    assert q.handled == [1, 2]


def test_drain_empty_queue_polls_once():
    q = FakeQueue([])
    assert drain_commands(q) == 0
    assert q.handled == []


def test_drain_is_capped_at_ten_per_pass():
    q = FakeQueue([cmd(i) for i in range(1, 16)])
    assert drain_commands(q) == 10
    assert q.handled == list(range(1, 11))
    assert len(q.cmds) == 5                                 # остальные — в следующий проход
    assert drain_commands(q, max_n=3) == 3


def test_drain_stops_on_command_that_was_not_acked():
    q = FakeQueue([cmd(7, "play")], ack=False)               # сервер отдаёт её снова и снова
    assert drain_commands(q) == 1
    assert q.handled == [7]


@pytest.mark.parametrize("ctype", ["restart_agent", "reboot", "update_agent"])
def test_drain_stops_after_process_ending_command(ctype):
    q = FakeQueue([cmd(1, ctype), cmd(2, "stop")])
    assert drain_commands(q) == 1
    assert q.handled == [1]


def test_drain_survives_failed_poll():
    class Broken:
        def poll_command(self):
            return None

    assert drain_commands(Broken()) == 0
