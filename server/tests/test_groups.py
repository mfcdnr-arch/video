"""Группы экранов (server/routers/screens.py): одно имя — одна группа.

POST /groups отвечает 409 на занятое имя (и при гонке, когда дубль ловит
уникальный индекс миграции 033), принимает JSON панели и query-параметры;
регистрация экрана берёт «Все экраны» детерминированно (ORDER BY id).
Эндпоинты зовутся напрямую на подменной сессии, которая записывает SQL.
Само слияние дублей (миграция 033) проверяется на живом Postgres —
см. docs/Техдокументация/04_ТЕСТЫ.md.
"""
import os
import re
from types import SimpleNamespace as R

import pytest
from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError

from routers import screens as screens_mod

MIGRATIONS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "migrations")


class _Res:
    def __init__(self, rows):
        self._rows = rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class RecDB:
    """Сессия-мок: пишет каждый SQL; на SELECT по имени группы отдаёт
    существующую группу, если имя есть в `existing`."""

    def __init__(self, existing=(), insert_error=False):
        self.existing = set(existing)
        self.insert_error = insert_error
        self.sql = []
        self.params = []
        self.committed = self.rolled_back = False

    def execute(self, query, params=None):
        q = " ".join(str(query).split())
        self.sql.append(q)
        self.params.append(params or {})
        if q.startswith("SELECT 1 FROM sync_groups WHERE name"):
            return _Res([R(x=1)] if params["n"] in self.existing else [])
        if q.startswith("INSERT INTO sync_groups"):
            if self.insert_error:
                raise IntegrityError(q, params, Exception("uq_sync_groups_name"))
            return _Res([(7,)])
        if q.startswith("INSERT INTO screens"):
            return _Res([(42,)])
        return _Res([])

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True


ADMIN = {"username": "admin", "role": "superadmin"}


def create(db, name=None, description="", body=None):
    return screens_mod.create_group(name=name, description=description, body=body, admin=ADMIN, db=db)


# --- POST /groups ----------------------------------------------------------------

def test_create_group_from_panel_json_body():
    db = RecDB()
    assert create(db, body={"name": "  Зал 2 ", "description": "Вход"}) == {"id": 7, "name": "Зал 2"}
    assert db.params[-1] == {"n": "Зал 2", "d": "Вход"}
    assert db.committed


def test_create_group_query_params_still_work():
    db = RecDB()
    assert create(db, name="Зал 3", description="Касса") == {"id": 7, "name": "Зал 3"}
    assert db.params[-1] == {"n": "Зал 3", "d": "Касса"}


def test_create_group_existing_name_is_409_and_nothing_inserted():
    db = RecDB(existing={"Все экраны"})
    with pytest.raises(HTTPException) as e:
        create(db, body={"name": "Все экраны"})
    assert e.value.status_code == 409
    assert "Все экраны" in e.value.detail
    assert not any(q.startswith("INSERT") for q in db.sql)
    assert not db.committed


def test_create_group_name_is_trimmed_before_duplicate_check():
    db = RecDB(existing={"Зал 1"})
    with pytest.raises(HTTPException) as e:
        create(db, body={"name": " Зал 1  "})
    assert e.value.status_code == 409


def test_create_group_race_caught_by_unique_index_is_409():
    db = RecDB(insert_error=True)
    with pytest.raises(HTTPException) as e:
        create(db, body={"name": "Зал 4"})
    assert e.value.status_code == 409
    assert db.rolled_back and not db.committed


@pytest.mark.parametrize("body", [None, {}, {"name": ""}, {"name": "   "}, {"name": None}])
def test_create_group_empty_name_is_400(body):
    db = RecDB()
    with pytest.raises(HTTPException) as e:
        create(db, body=body)
    assert e.value.status_code == 400
    assert db.sql == []


# --- регистрация экрана ------------------------------------------------------------

def test_register_screen_takes_all_screens_group_deterministically():
    db = RecDB()
    out = screens_mod.register_minipc(name="Экран 1", city="", location="", group_id=None, admin=ADMIN, db=db)
    assert out["screen_id"] == 42
    upd = [q for q in db.sql if q.startswith("UPDATE screens SET group_id")]
    assert len(upd) == 1
    assert re.search(r"WHERE name = 'Все экраны' ORDER BY id LIMIT 1", upd[0])


# --- сиды и миграция 033 (текст файлов) -------------------------------------------

def _read(name):
    with open(os.path.join(MIGRATIONS, name), encoding="utf-8") as f:
        return f.read()


@pytest.mark.parametrize("fname,group", [("001_init_schema.sql", "Зал 1"),
                                         ("002_admin_panel.sql", "Все экраны")])
def test_group_seed_checks_name_not_on_conflict(fname, group):
    sql = _read(fname)
    i = sql.index("INSERT INTO sync_groups")
    stmt = sql[i:sql.index(";", i)]
    assert "ON CONFLICT" not in stmt
    assert f"WHERE NOT EXISTS (SELECT 1 FROM sync_groups WHERE name = '{group}')" in stmt


def test_migration_033_moves_every_reference_and_adds_unique_index():
    sql = _read("033_sync_groups_unique_name.sql")
    for table in ("screens", "schedule_slots", "schedule_overrides", "campaigns"):
        assert re.search(rf"UPDATE {table}\s+t SET group_id = m\.keep_id", sql), table
    assert "CREATE UNIQUE INDEX IF NOT EXISTS uq_sync_groups_name ON sync_groups(name)" in sql
