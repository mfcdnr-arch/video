"""Единое правило «экран в сети» (этап 2, §4.5).

Экран в сети, если его последний сигнал (heartbeat, screens.last_seen) не старше
ONLINE_MINUTES минут. Правило считается в SQL: last_seen пишется в UTC и NOW()
в БД тоже UTC, поэтому сравнивать его с московскими значениями Python нельзя.

screens.status в расчёт не берётся: в 'offline' его никто не переводит, и
пропавший экран оставался бы «в сети» навсегда. Поле status в ответах API
сохранено для обратной совместимости, но панель по нему «в сети» не определяет.

Пороги уведомлений (offline_minutes в настройках) и кабинета рекламодателя —
отдельные настройки со своим смыслом, этого правила не используют.
"""

# Окно «в сети»: столько минут после последнего сигнала экран считается в сети.
ONLINE_MINUTES = 5

# Шаблоны с алиасом таблицы screens. Число минут подставляется из ONLINE_MINUTES.
_ONLINE_TMPL = ("({a}.last_seen IS NOT NULL "
                "AND {a}.last_seen > NOW() - INTERVAL '%d minutes')" % ONLINE_MINUTES)
_OFFLINE_FOR_TMPL = ("CASE WHEN {a}.last_seen IS NULL THEN NULL "
                     "ELSE CAST(EXTRACT(EPOCH FROM (NOW() - {a}.last_seen)) AS INTEGER) END")


def online_sql(alias: str = "s") -> str:
    """SQL-условие «экран в сети» (boolean) для таблицы screens под алиасом alias."""
    return _ONLINE_TMPL.format(a=alias)


def offline_for_sql(alias: str = "s") -> str:
    """SQL-выражение: сколько секунд прошло с последнего сигнала; NULL — сигнала не было."""
    return _OFFLINE_FOR_TMPL.format(a=alias)
