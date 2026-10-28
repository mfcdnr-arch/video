"""Лента экрана (GET /schedule/minipc/{id}) переживает ошибку БД в часовой
последовательности.

В Postgres после ошибки запроса транзакция прервана: любой следующий запрос
падает, пока не сделан rollback. Сбой при сборке часовой последовательности
(общий час, fill_to_hour) должен оставить ленту без неё, а не уронить весь
ответ экрану в 500. AbortingDB повторяет это поведение Postgres: после
исключения каждый execute падает до вызова rollback().
"""
from routers.screens import schedule_for_minipc


class _Result:
    def __init__(self, rows):
        self._rows = list(rows)

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def scalar(self):
        return self._rows[0] if self._rows else None


class AbortingDB:
    """Строки по подстроке SQL; запрос с подстрокой fail_on падает и прерывает транзакцию."""

    def __init__(self, routes, fail_on):
        self.routes, self.fail_on = routes, fail_on
        self.aborted = False
        self.rollbacks = 0
        self.commits = 0

    def execute(self, query, params=None):
        sql = " ".join(str(query).split())
        if self.aborted:
            raise RuntimeError("current transaction is aborted, commands ignored until end of transaction block")
        if self.fail_on in sql:
            self.aborted = True
            raise RuntimeError("relation does not exist")
        for needle, rows in self.routes:
            if needle in sql:
                return _Result(rows)
        return _Result([])

    def rollback(self):
        self.aborted = False
        self.rollbacks += 1

    def commit(self):
        self.commits += 1


def test_hourly_sequence_db_error_is_rolled_back_and_feed_still_answers():
    # эфир сети включён — у ленты есть активный плейлист, и сервер собирает
    # для него часовую последовательность; её первый запрос падает
    db = AbortingDB([("FROM network_broadcast b", [(5, "Эфир")])],
                    fail_on="SELECT fill_to_hour")
    feed = schedule_for_minipc(1, db=db, _screen=None)
    assert db.rollbacks == 1
    assert feed["active_source"] == "broadcast" and feed["active_playlist"]["playlist_id"] == 5
    assert feed["hourly_sequence"] == []
    # запросы после сбоя (заглушки, длительности картинок, питание) отработали
    assert feed["fallback"] == [] and feed["image_durations"] == {} and feed["power"] == {"on": None, "off": None}
