"""Единое правило «экран в сети» (этап 2, §4.5): последний сигнал (screens.last_seen)
не старше 5 минут. Правило считается в SQL — last_seen пишется в UTC, и NOW() в БД
тоже UTC; сравнивать его с московскими значениями Python нельзя.

Здесь проверяем шаблоны SQL и то, что роутеры собирают свои выборки через них
(сами выборки гоняет smoke_test.sh против живого стека).
"""
import inspect

from screen_online import ONLINE_MINUTES, online_sql, offline_for_sql


def test_online_window_is_five_minutes():
    assert ONLINE_MINUTES == 5
    assert "INTERVAL '5 minutes'" in online_sql("s") and "s.last_seen" in online_sql("s")


def test_alias_is_applied():
    assert "x.last_seen" in online_sql("x") and "x.last_seen" in offline_for_sql("x")
    assert "s.last_seen" not in online_sql("x") and "s.last_seen" not in offline_for_sql("x")


def test_default_alias_is_s():
    assert online_sql() == online_sql("s")
    assert offline_for_sql() == offline_for_sql("s")


def test_offline_for_is_null_without_last_seen():
    assert offline_for_sql("s").startswith("CASE WHEN s.last_seen IS NULL THEN NULL")


def test_online_requires_last_seen_and_is_computed_by_db_clock():
    sql = online_sql("s")
    assert "s.last_seen IS NOT NULL" in sql
    assert "NOW()" in sql          # часы БД (UTC), а не Python


def test_offline_for_counts_seconds_from_db_clock():
    sql = offline_for_sql("s")
    assert "EXTRACT(EPOCH FROM (NOW() - s.last_seen))" in sql
    assert "INTEGER" in sql


def test_templates_are_free_of_unfilled_placeholders():
    assert "{" not in online_sql("s") and "}" not in online_sql("s")
    assert "{" not in offline_for_sql("s") and "}" not in offline_for_sql("s")


def test_minipc_list_returns_group_id():
    # вкладка «Экраны → Группы» (screens.js) раскладывает экраны по s.group_id:
    # без этого поля в GET /minipc она всегда показывала «онлайн 0/0» и «без группы»
    from routers import screens
    src = inspect.getsource(screens.list_minipcs)
    # именно в списке выбираемых колонок (JOIN ... ON g.id = s.group_id не в счёт)
    select_list = src.split("SELECT", 1)[1].split("FROM screens", 1)[0]
    assert "s.group_id" in select_list


def test_routers_use_the_shared_rule():
    # «в сети» в списке экранов и на дашборде берётся из одного места, а не из screens.status
    from routers import screens, system
    list_src = inspect.getsource(screens.list_minipcs)
    assert "online_sql(" in list_src and "offline_for_sql(" in list_src
    dash_src = inspect.getsource(system.dashboard)
    assert dash_src.count("online_sql(") >= 3          # сводка, «сейчас в эфире», диски
    assert dash_src.count("offline_for_sql(") >= 2     # «сейчас в эфире», диски
