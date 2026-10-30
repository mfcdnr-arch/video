"""Правило «ролик можно играть» (server/playable_store.py): один SQL-фрагмент на все выборки
эфира — лента экрана, часовой блок, обзор сети и флаги списка плейлистов.

Здесь — сам фрагмент, загрузчики и паритет: каждое из мест собирает свой запрос через
playable_sql, своих копий фильтра (модерация, площадка, срок) не держит. Сами выборки
гоняет smoke_test.sh против живого стека.
"""
import inspect
from types import SimpleNamespace

import admin_panel
import playable_store
from playable_store import (DEFAULT_VENUE, LICENSED_VENUE, NOW_MSK_SQL, UNLICENSED_VENUE, load_playable_by_venue,
                            load_playable_items, playable_sql, venue_sql)


class _Row(SimpleNamespace):
    @property
    def _mapping(self):
        return vars(self)


class _Result:
    def __init__(self, rows):
        self._rows = list(rows)

    def fetchall(self):
        return self._rows


class RecDB:
    """Мок сессии: копит (SQL, параметры) каждого запроса и отдаёт заданные строки."""

    def __init__(self, rows=()):
        self.rows = list(rows)
        self.calls: list[tuple[str, dict]] = []

    def execute(self, query, params=None):
        self.calls.append((str(query), params or {}))
        return _Result(self.rows)


# --- фрагмент ------------------------------------------------------------------

def test_venue_constants():
    assert LICENSED_VENUE == "store_alcohol"
    assert UNLICENSED_VENUE == "other" and DEFAULT_VENUE == "other"


def test_playable_sql_has_every_condition():
    sql = playable_sql("m")
    assert "m.status = 'ready'" in sql
    assert "m.review_status = 'approved'" in sql                      # модерация 38-ФЗ
    assert venue_sql("m") in sql                                      # алкоголь — по площадке
    assert f"(m.valid_from IS NULL OR m.valid_from <= {NOW_MSK_SQL})" in sql   # срок показа — по Москве
    assert f"(m.valid_until IS NULL OR m.valid_until >= {NOW_MSK_SQL})" in sql


def test_venue_sql_lets_alcohol_only_on_licensed_venue():
    assert venue_sql("m") == "(m.category IS NULL OR m.category <> 'alcohol' OR :venue = 'store_alcohol')"


def test_alias_is_applied_and_default_is_m():
    assert playable_sql() == playable_sql("m") and venue_sql() == venue_sql("m")
    assert "x.status" in playable_sql("x") and "m." not in playable_sql("x")
    assert "x.category" in venue_sql("x") and "m." not in venue_sql("x")


def test_fragment_is_free_of_unfilled_placeholders():
    assert "{" not in playable_sql("m") and "}" not in playable_sql("m")


# --- загрузчики ----------------------------------------------------------------

def test_load_playable_items_uses_the_rule_and_keeps_order_fields():
    row = _Row(media_id=7, filename="a.mp4", filesize=10, md5_hash="h", repeat_count=2, position=1)
    db = RecDB([row])
    assert load_playable_items(db, 5, "store") == [
        {"media_id": 7, "filename": "a.mp4", "filesize": 10, "md5_hash": "h", "repeat_count": 2, "position": 1}]
    ((sql, params),) = db.calls
    assert playable_sql("m") in sql and "ORDER BY pi.position" in sql
    assert params == {"p": 5, "venue": "store"}


def test_load_playable_by_venue_one_query_per_venue_with_the_rule():
    db = RecDB([SimpleNamespace(playlist_id=3), SimpleNamespace(playlist_id=4)])
    out = load_playable_by_venue(db, ["other", "store_alcohol", "other"])
    assert out == {"other": {3, 4}, "store_alcohol": {3, 4}}
    assert [p for _, p in db.calls] == [{"venue": "other"}, {"venue": "store_alcohol"}]
    assert all(playable_sql("m") in sql for sql, _ in db.calls)


# --- паритет: все места берут один фрагмент ---------------------------------------

def test_hourly_block_ads_use_the_rule():
    db = RecDB([SimpleNamespace(filename="a.mp4", duration=10, repeat_count=2)])
    assert admin_panel.ad_sequence(db, 5, "mall") == [("a.mp4", 10.0), ("a.mp4", 10.0)]
    ((sql, params),) = db.calls
    assert playable_sql("m") in sql and params == {"pid": 5, "venue": "mall"}


def test_feed_network_and_playlists_take_the_shared_rule():
    from routers import network, playlists, screens
    feed = inspect.getsource(screens.schedule_for_minipc)
    assert "load_playable_items(" in feed                             # ролики эфира, показа, переопределения
    assert network.load_playable_by_venue is load_playable_by_venue   # обзор сети
    assert playlists.load_playable_by_venue is load_playable_by_venue  # флаги playable в списке плейлистов
    assert playlists.LICENSED_VENUE == LICENSED_VENUE and playlists.UNLICENSED_VENUE == UNLICENSED_VENUE
    assert network.DEFAULT_VENUE == DEFAULT_VENUE


def test_no_private_copies_of_the_filter_left():
    # срок показа (valid_until) в эфирных выборках проверяет только общий фрагмент;
    # симулятор (routers/schedule.py) — свой вариант «на выбранную дату», он здесь не в счёт
    from routers import network, playlists, screens
    for mod in (screens, network, playlists, admin_panel):
        assert "valid_until" not in inspect.getsource(mod), mod.__name__
        assert "'store_alcohol'" not in inspect.getsource(mod).replace('"store_alcohol"', ""), mod.__name__


def test_playlists_no_longer_import_from_the_network_router():
    from routers import playlists
    src = inspect.getsource(playlists)
    assert "from routers.network" not in src and "from playable_store import" in src


def test_module_has_no_router_dependencies():
    imports = [ln for ln in inspect.getsource(playable_store).splitlines() if ln.startswith(("from ", "import "))]
    assert imports == ["from sqlalchemy import text"]                 # ни fastapi, ни роутеров


class _AnyResult:
    def fetchall(self):
        return []

    def fetchone(self):
        return None

    def scalar(self):
        return None


class FeedDB:
    """Мок сессии для ленты экрана целиком: всё пусто, тексты запросов копятся."""

    def __init__(self):
        self.sqls: list[str] = []

    def execute(self, query, params=None):
        self.sqls.append(" ".join(str(query).split()))
        return _AnyResult()

    def commit(self):
        pass

    def rollback(self):
        pass


def test_feed_template_slots_use_the_rule_with_validity():
    # слоты недельного шаблона в ленте — то же правило, со сроком показа: ролик, у которого
    # срок вышел или ещё не начался, в шаблоне не играет — как у эфира, показов и замен
    from routers import screens
    db = FeedDB()
    screens.schedule_for_minipc(1, db=db, _screen=None)
    (slots_sql,) = [s for s in db.sqls if "FROM best b" in s and "json_agg" in s]
    assert " ".join(playable_sql("m").split()) in slots_sql


# --- срок показа — по московскому времени ------------------------------------------
# valid_from / valid_until — TIMESTAMP без пояса, панель пишет их по Москве, а NOW() БД
# живёт в UTC: голое сравнение с NOW() сдвигало срок на 3 часа («по 18:00» играло до 21:00).

def test_now_msk_sql_is_moscow_time_of_the_db_clock():
    assert NOW_MSK_SQL == "(NOW() AT TIME ZONE 'Europe/Moscow')"


def test_playable_sql_compares_validity_with_moscow_time_only():
    sql = playable_sql("m")
    assert sql.count(NOW_MSK_SQL) == 2
    assert "NOW()" not in sql.replace(NOW_MSK_SQL, "")             # голого NOW() (UTC) нет


def test_validity_report_uses_moscow_time():
    from routers import reports
    db = RecDB()
    reports._data_validity(db)
    ((sql, _),) = db.calls
    assert sql.count(NOW_MSK_SQL) == 2 and "NOW()" not in sql.replace(NOW_MSK_SQL, "")


def test_dashboard_expiring_media_uses_moscow_time():
    from routers import system
    src = inspect.getsource(system.dashboard)
    block = src[src.index("expiring = db.execute"):src.index(".fetchall()", src.index("expiring = db.execute"))]
    assert block.count("NOW_MSK_SQL") == 2 and "NOW()" not in block
