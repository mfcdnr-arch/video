"""Правила плейлиста: повторы, мягкие предупреждения, «общий час».

Чистая логика (server/playlist_rules.py) и раскладка заглушек, вынесенная
из admin_panel.build_hourly_detail. БД не нужна; сам build_hourly_detail
проверяется на подменённых запросах (см. _SeqDB) — он кормит агента и
панель, его ответ не должен измениться после рефакторинга.
"""
from playlist_rules import (expand_repeats, interleave, repeat_warnings,
                            build_shared_hour, overflow_minutes)
from admin_panel import distribute_fillers, build_hourly_detail
from conftest import db_row


def it(mid, title, dur, rep=1, md5=None):
    return {"media_id": mid, "title": title, "duration": dur, "repeat_count": rep, "md5": md5 or f"h{mid}"}


def test_expand_repeats():
    assert [x["media_id"] for x in expand_repeats([it(1, "a", 10, 3), it(2, "b", 5)])] == [1, 1, 1, 2]


def test_interleave_uneven():
    assert interleave([["a1", "a2", "a3"], ["b1"]]) == ["a1", "b1", "a2", "a3"]


def test_warn_more_than_three_in_row():
    assert repeat_warnings([it(1, "Пятёрочка_осень", 15, 4), it(2, "Афиша", 30)])[0] == \
        "«Пятёрочка_осень» ×4 подряд — больше 3 повторов"


def test_no_warn_at_exactly_three():
    assert not any("подряд" in w for w in repeat_warnings([it(1, "A", 15, 3), it(2, "B", 100)]))


def test_warn_share_over_half():
    w = repeat_warnings([it(1, "Пятёрочка_осень", 15, 4), it(2, "Афиша", 30), it(3, "Погода", 12, 2)])
    assert "«Пятёрочка_осень» занимает 60 из 114 с (53 %) — больше половины плейлиста" in w


def test_no_share_warning_for_single_item():
    assert repeat_warnings([it(1, "A", 15, 2)]) == []


def test_warn_duplicate_files():
    w = repeat_warnings([it(1, "X", 10, md5="same"), it(2, "X (2)", 10, md5="same")])
    assert "«X» и «X (2)» — это один и тот же файл" in w


def test_shared_hour_without_fill_is_interleave():
    assert build_shared_hour([[("a", 10), ("a", 10)], [("b", 10)]], [("f", 5)], None) == ["a", "b", "a"]


def test_shared_hour_with_fill_adds_fillers_but_keeps_ad_order():
    seq = build_shared_hour([[("a", 10)], [("b", 10)]], [("f", 10)], 60)
    assert [x for x in seq if x != "f"] == ["a", "b"] and seq.count("f") == 4


def test_distribute_fillers_never_exceeds_target():
    seq, secs, n = distribute_fillers([("a", 20)], [("f", 15)], 60)
    assert secs <= 40 and n == 2 and seq[0] == "a"


def test_overflow_minutes():
    assert overflow_minutes(72 * 60) == 72 and overflow_minutes(3600) is None


# ─── Дополнительные случаи: порядок предупреждений, границы, рефакторинг ────

def test_warnings_order_in_row_then_share_then_duplicates():
    # A ×4 (подряд), C ×6 (подряд + доля 60 из 110), A и B — один файл (дубль)
    w = repeat_warnings([it(1, "A", 10, 4, md5="dup"), it(2, "B", 10, 1, md5="dup"), it(3, "C", 10, 6)])
    assert w == [
        "«A» ×4 подряд — больше 3 повторов",
        "«C» ×6 подряд — больше 3 повторов",
        "«C» занимает 60 из 110 с (55 %) — больше половины плейлиста",
        "«A» и «B» — это один и тот же файл",
    ]


def test_share_exactly_half_is_not_warned():
    # 30 из 60 с — ровно половина, «больше половины» не срабатывает
    assert not any("занимает" in w for w in repeat_warnings([it(1, "A", 30), it(2, "B", 30)]))


def test_three_copies_of_one_file_give_two_warnings_by_first_occurrence():
    w = repeat_warnings([it(1, "X", 10, md5="s"), it(2, "X2", 10, md5="s"), it(3, "X3", 10, md5="s")])
    assert w == ["«X» и «X2» — это один и тот же файл", "«X» и «X3» — это один и тот же файл"]


def test_same_media_twice_is_not_a_duplicate_file():
    w = repeat_warnings([it(1, "X", 10, md5="s"), it(1, "X", 10, md5="s")])
    assert not any("один и тот же" in x for x in w)


def test_missing_md5_is_never_a_duplicate():
    a, b = it(1, "A", 10), it(2, "B", 10)
    a["md5"] = b["md5"] = None
    assert not any("один и тот же" in x for x in repeat_warnings([a, b]))


def test_expand_repeats_treats_empty_count_as_one():
    got = expand_repeats([{"media_id": 1, "repeat_count": None}, {"media_id": 2, "repeat_count": 0}])
    assert [x["media_id"] for x in got] == [1, 2]


def test_distribute_fillers_empty_ads():
    assert distribute_fillers([], [("f", 10)], 3600) == ([], 0.0, 0)


def test_distribute_fillers_ignores_fillers_without_duration():
    # раньше такой набор заглушек зацикливал расчёт навсегда (память росла до OOM)
    assert distribute_fillers([("a", 10)], [("f", 0), ("g", 0)], 3600) == (["a"], 0.0, 0)


def test_distribute_fillers_mixed_zero_and_real_duration_still_fills():
    seq, secs, n = distribute_fillers([("a", 10)], [("z", 0), ("f", 10)], 40)
    assert secs <= 30 and n >= 1 and seq[0] == "a"


def test_overflow_minutes_rounds_and_respects_custom_limit():
    assert overflow_minutes(3601) == 60 and overflow_minutes(500, limit_seconds=300) == 8


class _SeqDB:
    """Мок сессии: первый execute() отдаёт рекламу, второй — заглушки."""

    def __init__(self, ad_rows, filler_rows):
        self._batches = [ad_rows, filler_rows]

    def execute(self, query, params=None):
        rows = self._batches.pop(0)
        return type("R", (), {"fetchall": lambda self_, r=rows: r})()


def test_build_hourly_detail_keeps_keys_and_values():
    db = _SeqDB(
        [db_row(filename="a.mp4", duration=20, repeat_count=2), db_row(filename="b.mp4", duration=10, repeat_count=None)],
        [db_row(filename="f.mp4", duration=15)],
    )
    assert build_hourly_detail(db, 1, 120) == {
        "sequence": ["a.mp4", "f.mp4", "a.mp4", "f.mp4", "b.mp4", "f.mp4", "f.mp4"],
        "main_seconds": 50.0, "main_plays": 3,
        "filler_seconds": 60.0, "filler_plays": 4,
        "fillers_available": 1, "fillers_available_seconds": 15.0,
        "target_seconds": 120.0,
    }


def test_build_hourly_detail_without_room_returns_plain_ads():
    db = _SeqDB([db_row(filename="a.mp4", duration=100, repeat_count=1)], [db_row(filename="f.mp4", duration=15)])
    d = build_hourly_detail(db, 1, 60)
    assert d["sequence"] == ["a.mp4"] and d["filler_plays"] == 0 and d["filler_seconds"] == 0.0
