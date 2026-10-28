"""Учёт показов агентом (play_log): когда считать, что начался новый показ.

По строкам play_log считаются биллинг и кабинет рекламодателя, поэтому
повторы картинок-баннеров и коротких роликов не должны теряться: у картинки
time-pos стоит около нуля, и по «отмотке времени» её повторы не видны —
новый показ определяется сменой записи плейлиста mpv (playlist-pos).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ds_player import VLCPlayer, is_new_play


def S(pos, filename, count=3, time_pos=None):
    return {"pos": pos, "count": count, "filename": filename, "time_pos": time_pos}


def count_plays(samples):
    """Прогнать замеры так же, как главный цикл агента: замер без имени
    файла пропускается и прошлый не затирает."""
    prev, plays = None, []
    for cur in samples:
        if not cur.get("filename"):
            continue
        if is_new_play(prev, cur):
            plays.append(cur["filename"])
        prev = cur
    return plays


# --- первый замер и пустые ---------------------------------------------------

def test_first_sample_with_file_is_a_play():
    assert is_new_play(None, S(0, "a.mp4")) is True


def test_sample_without_file_is_never_a_play():
    assert is_new_play(None, S(None, None)) is False
    assert is_new_play(S(0, "a.mp4"), S(1, None)) is False


# --- плейлист из нескольких записей: по playlist-pos -------------------------

def test_three_identical_banners_count_three_plays_per_round():
    # [IMG, IMG, IMG]: time-pos у картинки ~0, имя одно и то же
    samples = [S(0, "b.png"), S(0, "b.png"), S(1, "b.png"), S(1, "b.png"),
               S(2, "b.png"), S(0, "b.png"), S(1, "b.png")]
    assert count_plays(samples) == ["b.png"] * 5


def test_short_clip_repeated_counts_every_entry():
    # ролик 2 с ×3: каждая запись видна хотя бы одним замером
    samples = [S(0, "c.mp4", time_pos=0.4), S(1, "c.mp4", time_pos=0.3),
               S(2, "c.mp4", time_pos=1.1), S(0, "c.mp4", time_pos=0.2)]
    assert count_plays(samples) == ["c.mp4"] * 4


def test_wrap_around_to_first_entry_is_a_play():
    assert is_new_play(S(2, "c.mp4"), S(0, "a.mp4")) is True
    assert is_new_play(S(1, "a.mp4", count=2), S(0, "a.mp4", count=2)) is True


def test_mixed_playlist_counts_each_switch_once():
    samples = [S(0, "a.mp4"), S(0, "a.mp4"), S(1, "b.png"), S(1, "b.png"),
               S(1, "b.png"), S(2, "a.mp4"), S(0, "a.mp4")]
    assert count_plays(samples) == ["a.mp4", "b.png", "a.mp4", "a.mp4"]


def test_rewind_inside_long_playlist_does_not_double_count():
    # ×N одного ролика: смена записи уже посчитана, отмотка time-pos на той же
    # позиции (замер свойств по очереди) второй раз не считается
    assert is_new_play(S(1, "a.mp4", time_pos=9.0), S(1, "a.mp4", time_pos=0.2)) is False


def test_same_position_other_file_or_other_length_is_a_play():
    assert is_new_play(S(0, "a.mp4"), S(0, "z.mp4")) is True
    assert is_new_play(S(0, "a.mp4", count=3), S(0, "a.mp4", count=5)) is True


def test_same_entry_still_playing_is_not_a_play():
    assert is_new_play(S(1, "b.png"), S(1, "b.png")) is False


def test_missing_samples_between_entries_are_skipped():
    samples = [S(0, "a.mp4"), S(None, None), S(1, "a.mp4"), S(None, None), S(1, "a.mp4")]
    assert count_plays(samples) == ["a.mp4", "a.mp4"]


# --- плейлист из одной записи: по отмотке time-pos ----------------------------

def test_single_entry_loop_detected_by_rewind():
    samples = [S(0, "a.mp4", 1, 0.5), S(0, "a.mp4", 1, 4.0), S(0, "a.mp4", 1, 7.5),
               S(0, "a.mp4", 1, 0.8), S(0, "a.mp4", 1, 4.2)]
    assert count_plays(samples) == ["a.mp4", "a.mp4"]


def test_single_entry_small_jitter_is_not_a_play():
    assert is_new_play(S(0, "a.mp4", 1, 5.0), S(0, "a.mp4", 1, 4.5)) is False
    assert is_new_play(S(0, "a.mp4", 1, 5.0), S(0, "a.mp4", 1, None)) is False


# --- старый mpv / сбой IPC: без playlist-pos — как раньше ---------------------

def test_without_position_falls_back_to_filename_and_rewind():
    assert is_new_play(S(None, "a.mp4", None), S(None, "b.mp4", None)) is True
    assert is_new_play(S(None, "a.mp4", None, 8.0), S(None, "a.mp4", None, 0.3)) is True
    assert is_new_play(S(None, "a.mp4", None, 3.0), S(None, "a.mp4", None, 4.0)) is False


# --- замер состояния mpv ------------------------------------------------------

class FakePlayer(VLCPlayer):
    """VLCPlayer без mpv: ответы IPC из словаря {свойство: data}; нет ключа — ошибка."""

    def __init__(self, props):
        super().__init__(media_dir="/nonexistent")
        self.props = props
        self.asked = []

    def _mpv_cmd(self, command):
        name = command[1]
        self.asked.append(name)
        if name in self.props:
            return {"data": self.props[name], "error": "success", "request_id": 0}
        return {"error": "property unavailable", "request_id": 0}


def test_play_state_takes_name_of_the_entry_at_position():
    p = FakePlayer({"playlist-pos": 2, "playlist-count": 3,
                    "playlist/2/filename": "/opt/ds-agent/media/b.png",
                    "filename": "other.mp4", "time-pos": 0.0})
    assert p.get_play_state() == {"pos": 2, "count": 3, "filename": "b.png", "time_pos": None}
    assert "time-pos" not in p.asked and "filename" not in p.asked


def test_play_state_single_entry_reads_time_pos():
    p = FakePlayer({"playlist-pos": 0, "playlist-count": 1,
                    "playlist/0/filename": "/opt/ds-agent/media/a.mp4", "time-pos": 3.5})
    assert p.get_play_state() == {"pos": 0, "count": 1, "filename": "a.mp4", "time_pos": 3.5}


def test_play_state_idle_player():
    p = FakePlayer({"playlist-pos": -1, "playlist-count": 0})
    assert p.get_play_state() == {"pos": None, "count": None, "filename": None, "time_pos": None}


def test_play_state_without_position_uses_filename_property():
    p = FakePlayer({"filename": "a.mp4", "time-pos": 2.0})
    assert p.get_play_state() == {"pos": None, "count": None, "filename": "a.mp4", "time_pos": 2.0}


def test_play_state_entry_name_unavailable_gives_no_file():
    # позиция есть, а имя записи не прочиталось: замер неполный, его пропустят
    p = FakePlayer({"playlist-pos": 1, "playlist-count": 2, "filename": "a.mp4"})
    assert p.get_play_state()["filename"] is None


# --- ответ mpv среди событий --------------------------------------------------

class _FakeSocket:
    reply = b""

    def __init__(self, *args):
        pass

    def settimeout(self, t):
        pass

    def connect(self, path):
        pass

    def sendall(self, data):
        pass

    def recv(self, n):
        return self.reply

    def close(self):
        pass


def _ipc(monkeypatch, reply):
    import ds_player
    _FakeSocket.reply = reply
    monkeypatch.setattr(ds_player.socket, "socket", _FakeSocket)
    monkeypatch.setattr(ds_player.time, "sleep", lambda s: None)
    return VLCPlayer(media_dir="/nonexistent")._mpv_cmd(["get_property", "playlist-pos"])


def test_ipc_skips_events_before_the_reply(monkeypatch):
    # на стыке роликов mpv может прислать событие раньше ответа на запрос
    r = _ipc(monkeypatch, b'{"event":"start-file","playlist_entry_id":3}\n'
                          b'{"data":2,"request_id":0,"error":"success"}\n')
    assert r == {"data": 2, "request_id": 0, "error": "success"}


def test_ipc_only_events_gives_empty_answer(monkeypatch):
    assert _ipc(monkeypatch, b'{"event":"playback-restart"}\n') == {}
    assert _ipc(monkeypatch, b'') == {}
