"""Симулятор эфира GET /schedule/simulate: шаг «недельный шаблон» — тот же выбор слота,
что у ленты экрана (routers/screens.py, DISTINCT ON до фильтра роликов) и агента
(ds_agent.current_playlist_files), и у «Эфира сети» (placements_logic.template_playlist).

Вершина часа (экран > группа > сеть) выбирается ДО фильтра роликов. Если её плейлисту
нечего играть, слот уровнем ниже на тот же час её не подменяет — идёт вершина
«весь день»; если пуста и она — заглушки. Срок показа роликов у симулятора — на
выбранную дату (это не меняется).

Настоящего Postgres здесь нет: RoutedDB отдаёт строки по куску текста SQL, ролики —
по id плейлиста. Слоты отдаются в том порядке, в каком их сортировал бы SQL
(почасовые выше, потом больший prio), если не сказано иное.
"""
from datetime import datetime
from types import SimpleNamespace as Row

import pytest

import routers.schedule as schedule_router

ON_DATE, DOW, HOUR = "2026-11-02", 0, 9          # понедельник, 09:00 МСК
SCREEN = Row(id=1, name="Экран 1", group_id=7, venue_type="other")
CLIP = Row(filename="c.mp4", title="Ролик", repeat_count=1)
FILLER = Row(filename="f.mp4", title="Заглушка")
NAMES = {10: "Утро экрана", 20: "Утро группы", 30: "День сети", 40: "День экрана"}


class _Result:
    def __init__(self, rows):
        self._rows = list(rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows


class RoutedDB:
    """Мок сессии: строки по подстроке SQL (пробелы схлопнуты), без совпадения — пусто."""

    def __init__(self, routes):
        self.routes = routes
        self.calls: list[tuple[str, dict]] = []

    def execute(self, query, params=None):
        sql = " ".join(str(query).split())
        self.calls.append((sql, params or {}))
        for needle, rows in self.routes:
            if needle in sql:
                return _Result(rows(params or {}) if callable(rows) else rows)
        return _Result([])

    def params(self, needle):
        return [p for sql, p in self.calls if needle in sql]


class MRow(Row):
    """Строка с _mapping: симулятор берёт слоты как dict(r._mapping)."""

    @property
    def _mapping(self):
        return vars(self)


def SLOT(pl, hour=HOUR, prio=3):
    """Слот шаблона на понедельник: prio 3 — экран, 2 — группа, 1 — сеть; hour None — весь день."""
    return MRow(day_of_week=DOW, hour=hour, playlist_id=pl, name=NAMES[pl], prio=prio)


def sim_db(slots, playable, fillers=(FILLER,)):
    """Слоты экрана 1 и плейлисты, которым есть что играть (у остальных роликов нет)."""
    return RoutedDB([("FROM screens s WHERE s.id", [SCREEN]),
                     ("FROM schedule_slots ss", list(slots)),
                     ("FROM playlist_items pi JOIN media m",
                      lambda p: [CLIP] if p.get("p") in playable else []),
                     ("WHERE m.is_filler = TRUE", list(fillers))])


@pytest.fixture
def simulate(monkeypatch):
    """Ответ симулятора для экрана 1 на понедельник 09:00 (общего эфира и показов нет)."""
    def run(db):
        monkeypatch.setattr(schedule_router, "now_msk", lambda: datetime(2026, 10, 30, 12, 0))
        monkeypatch.setattr(schedule_router, "get_broadcast", lambda db_, now: {"is_on": False})
        monkeypatch.setattr(schedule_router, "resolve_placement_hour", lambda db_, sid, at: [])
        return schedule_router.simulate_schedule(screen_id=1, on_date=ON_DATE, hour=HOUR, admin={}, db=db)
    return run


def test_empty_hour_slot_gives_way_to_all_day_slot(simulate):
    out = simulate(sim_db([SLOT(10), SLOT(30, hour=None, prio=1)], playable={30}))
    assert out["source"] == "slot" and out["playlist"] == "День сети"
    assert out["source_label"] == "Недельный шаблон (слот всей сети, на весь день)"
    assert out["items"] == [{"filename": "c.mp4", "title": "Ролик"}]
    (note,) = [n for n in out["notes"] if "пуст после фильтров" in n]
    assert "«Утро экрана»" in note and "на весь день" in note
    assert "уровнем ниже" not in note             # других почасовых слотов на 09:00 нет


def test_lower_level_hour_slot_does_not_replace_empty_top(simulate):
    """Пустой слот экрана на 09:00 не подменяется слотом группы на 09:00 — как в ленте экрана."""
    out = simulate(sim_db([SLOT(10), SLOT(20, prio=2), SLOT(30, hour=None, prio=1)], playable={20, 30}))
    assert out["source"] == "slot" and out["playlist"] == "День сети"
    note = next(n for n in out["notes"] if "«Утро экрана»" in n)
    assert "уровнем ниже" in note


def test_empty_hour_and_all_day_slots_go_to_fillers(simulate):
    out = simulate(sim_db([SLOT(10), SLOT(40, hour=None)], playable=set()))
    assert out["source"] == "fallback" and out["items"] == [{"filename": "f.mp4", "title": "Заглушка"}]
    empty = [n for n in out["notes"] if "пуст после фильтров" in n]
    assert len(empty) == 2 and "заглушки" in empty[-1]


def test_empty_hour_slot_without_all_day_slot_goes_to_fillers(simulate):
    out = simulate(sim_db([SLOT(10)], playable=set()))
    assert out["source"] == "fallback"
    (note,) = [n for n in out["notes"] if "пуст после фильтров" in n]
    assert "заглушки" in note and "на весь день" not in note


def test_hour_slot_with_clips_wins_and_screen_beats_group_and_network(simulate):
    """Вершину выбирает общее правило, а не порядок строк из БД."""
    rows = [SLOT(30, hour=None, prio=1), SLOT(20, prio=2), SLOT(10)]
    out = simulate(sim_db(rows, playable={10, 20, 30}))
    assert out["source"] == "slot" and out["playlist"] == "Утро экрана"
    assert out["source_label"] == "Недельный шаблон (слот экрана, почасовой)"
    assert not any("пуст после фильтров" in n for n in out["notes"])


def test_slot_query_is_for_this_weekday_hour_and_screen(simulate):
    db = sim_db([], playable=set())
    simulate(db)
    (params,) = db.params("FROM schedule_slots ss")
    assert params == {"dow": DOW, "h": HOUR, "sid": 1, "gid": 7}
