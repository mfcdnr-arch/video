"""Ночной и ручной дамп базы (tasks.nightly_backup, POST /backups/create).

Оба снимают дамп той же функцией, что и «Копия на флешку» (db_dump.pg_dump_gz):
  * пароль БД не попадает в командную строку pg_dump (её видят `ps` и `docker top`) —
    адрес без пароля, сам пароль в окружении процесса (PGPASSWORD);
  * из шапки дампа убрана строка `SET transaction_timeout = 0;`: pg_dump 17 в образе api
    пишет её, а PostgreSQL 15 такой параметр не знает, и заливка с ON_ERROR_STOP вставала;
  * `--lock-wait-timeout=60s`: дамп не ждёт чужую блокировку таблицы без конца;
  * текст ошибки короткий и без пароля БД, недописанный файл удаляется, записи о
    бэкапе в базе нет.

Утилита pg_dump поддельная (скрипт в PATH): она печатает свои аргументы и PGPASSWORD или
выдаёт заранее заготовленный дамп. База подменена.
"""
import gzip
import os
from contextlib import contextmanager

import pytest
import sqlalchemy
from fastapi import FastAPI
from fastapi.testclient import TestClient

import tasks
from db_dump import pg_dump_target
from deps import create_access_token, get_db
from routers import backups as backups_mod

DB_PW = "p%40ss%3As3cr3t"                 # в адресе пароль %-кодирован
DB_PW_PLAIN = "p@ss:s3cr3t"               # pg_dump получает раскодированный
DB_URL = f"postgresql://display_user:{DB_PW}@postgres:5432/display_system"
DB_URL_NO_PW = "postgresql://display_user@postgres:5432/display_system"
TT = "SET transaction_timeout = 0;"
DUMP = (
    "--\n-- PostgreSQL database dump\n--\n\n"
    "\\restrict aBcD1234xyz\n\n"
    "-- Dumped from database version 15.18\n-- Dumped by pg_dump version 17.10\n\n"
    "SET statement_timeout = 0;\n"
    f"{TT}\n"
    "SET client_encoding = 'UTF8';\n"
    "SELECT pg_catalog.set_config('search_path', '', false);\n\n"
    "CREATE TABLE public.notes (id integer, body text);\n\n"
    f"COPY public.notes (id, body) FROM stdin;\n1\t{TT}\n\\.\n\n"
    "\\unrestrict aBcD1234xyz\n"
)
DUMP_FOR_PG15 = DUMP.replace(TT + "\n", "", 1)     # из шапки ушла только эта строка, в данных — осталась


def _fake_pg_dump(tmp_path, monkeypatch, body: str) -> None:
    """Положить в PATH поддельный pg_dump: аргументы и PGPASSWORD пишет в файлы рядом, в stdout — body."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    p = bindir / "pg_dump"
    p.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$@\" > '{tmp_path}/argv.txt'\n"
                 f"printf '%s' \"$PGPASSWORD\" > '{tmp_path}/pgpassword.txt'\n{body}")
    p.chmod(0o755)
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ["PATH"])
    monkeypatch.delenv("PGPASSWORD", raising=False)


def _dump_ok(tmp_path, monkeypatch):
    src = tmp_path / "payload.sql"
    src.write_text(DUMP)
    _fake_pg_dump(tmp_path, monkeypatch, f"cat '{src}'\n")


def _dump_fails(tmp_path, monkeypatch):
    # пароль в stderr и 900 нулей после него: текст ошибки длиннее 300 символов
    _fake_pg_dump(tmp_path, monkeypatch,
                  f"echo '-- начало'\nprintf 'pg_dump: error: %s %s %0900d\\n' '{DB_PW_PLAIN}' '{DB_PW}' 0 >&2\n"
                  "exit 1\n")


def _assert_password_only_in_env(tmp_path):
    argv = (tmp_path / "argv.txt").read_text()
    assert argv.splitlines() == ["--lock-wait-timeout=60s", DB_URL_NO_PW]
    for secret in (DB_PW, DB_PW_PLAIN, "s3cr3t"):
        assert secret not in argv
    assert (tmp_path / "pgpassword.txt").read_text() == DB_PW_PLAIN


def _backup_files(bdir):
    return sorted(os.listdir(bdir)) if os.path.isdir(bdir) else []


@pytest.mark.parametrize("url", ["postgresql://u@h:5432/d", "postgresql:///d"])
def test_pg_dump_target_without_password_is_unchanged(monkeypatch, url):
    monkeypatch.setenv("PGPASSWORD", "from-outside")
    target, env = pg_dump_target(url)
    assert target == url and env["PGPASSWORD"] == "from-outside"    # своё окружение не трогаем


# --- ночной дамп (Celery) ----------------------------------------------------------------------

class _Conn:
    def __init__(self, log):
        self.log = log

    def execute(self, query, params=None):
        self.log.append(" ".join(str(query).split()))
        return _Rows()


class _Rows:
    def fetchall(self):
        return []                    # старых копий для ротации нет


class _Engine:
    def __init__(self, log):
        self.log = log

    @contextmanager
    def begin(self):
        yield _Conn(self.log)


@pytest.fixture
def nightly(tmp_path, monkeypatch):
    """Окружение ночного дампа: DATABASE_URL с паролем, BACKUP_DIR во временной папке, подменная база.
    Возвращает (папка бэкапов, журнал SQL)."""
    log = []
    bdir = tmp_path / "backups"
    monkeypatch.setattr(sqlalchemy, "create_engine", lambda *a, **k: _Engine(log))
    monkeypatch.setenv("DATABASE_URL", DB_URL)
    monkeypatch.setenv("BACKUP_DIR", str(bdir))
    return bdir, log


def test_nightly_backup_password_not_in_command_line(tmp_path, monkeypatch, nightly):
    _dump_ok(tmp_path, monkeypatch)
    name = tasks.nightly_backup()
    assert name.startswith("backup_") and name.endswith(".sql.gz")
    _assert_password_only_in_env(tmp_path)


def test_nightly_backup_dump_restores_into_postgres_15(tmp_path, monkeypatch, nightly):
    bdir, log = nightly
    _dump_ok(tmp_path, monkeypatch)
    name = tasks.nightly_backup()
    assert gzip.decompress((bdir / name).read_bytes()).decode() == DUMP_FOR_PG15
    assert any("INSERT INTO backups" in q for q in log)
    assert any("INSERT INTO audit_log" in q for q in log)


def test_nightly_backup_failure_is_short_without_password_and_leaves_nothing(tmp_path, monkeypatch, nightly):
    bdir, log = nightly
    _dump_fails(tmp_path, monkeypatch)
    res = tasks.nightly_backup()
    assert res.startswith("error: Ошибка pg_dump: pg_dump: error:")
    for secret in (DB_PW, DB_PW_PLAIN, "s3cr3t"):
        assert secret not in res
    assert len(res) <= 300 + 60
    assert _backup_files(bdir) == []         # недописанный дамп убран
    assert log == []                         # и в базе о нём ни строчки


def test_nightly_backup_without_pg_dump_binary(tmp_path, monkeypatch, nightly):
    bdir, log = nightly
    monkeypatch.setenv("PATH", str(tmp_path / "пусто"))
    assert tasks.nightly_backup() == "error: pg_dump не найден на сервере"
    assert _backup_files(bdir) == [] and log == []


# --- ручной дамп (кнопка «Создать бэкап» в Настройках) -------------------------------------------

class _Res:
    def __init__(self, row=None):
        self._row = row

    def fetchone(self):
        return self._row

    def first(self):
        return self._row

    def mappings(self):
        return self


class _Row(dict):
    __getattr__ = dict.get

    @property
    def _mapping(self):
        return self


class BackupsDB:
    """Сессия-мок: пользователь admin; INSERT в backups возвращает строку, всё записанное — в log."""

    def __init__(self):
        self.log = []
        self.committed = False

    def execute(self, query, params=None):
        q = " ".join(str(query).split())
        p = params or {}
        if "FROM users" in q:
            return _Res(_Row(username="admin", role="admin", is_active=True,
                             is_blocked=False, advertiser_id=None))
        self.log.append(q)
        if "INSERT INTO backups" in q:
            return _Res(_Row(id=7, filename=p["fn"], size_bytes=p["sz"], created_at="2026-11-08T12:00:00"))
        return _Res()

    def commit(self):
        self.committed = True


@pytest.fixture
def manual(tmp_path, monkeypatch):
    """Клиент с роутером бэкапов: DATABASE_URL с паролем, BACKUP_DIR во временной папке, подменная база.
    Возвращает (клиент, папка бэкапов, база)."""
    bdir = tmp_path / "backups"
    bdir.mkdir()
    monkeypatch.setattr(backups_mod, "BACKUP_DIR", str(bdir))
    monkeypatch.setattr(backups_mod, "DATABASE_URL", DB_URL)
    db = BackupsDB()
    app = FastAPI()
    app.include_router(backups_mod.router)
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app), bdir, db


ADMIN = {"Authorization": "Bearer " + create_access_token({"sub": "admin"})}


def test_create_backup_password_not_in_command_line(tmp_path, monkeypatch, manual):
    client, _, _ = manual
    _dump_ok(tmp_path, monkeypatch)
    r = client.post("/backups/create", headers=ADMIN)
    assert r.status_code == 200, r.text
    _assert_password_only_in_env(tmp_path)


def test_create_backup_dump_restores_into_postgres_15(tmp_path, monkeypatch, manual):
    client, bdir, db = manual
    _dump_ok(tmp_path, monkeypatch)
    r = client.post("/backups/create", headers=ADMIN)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] == 7 and body["filename"].startswith("backup_")
    data = (bdir / body["filename"]).read_bytes()
    assert body["size_bytes"] == len(data)
    assert gzip.decompress(data).decode() == DUMP_FOR_PG15
    assert db.committed


def test_create_backup_failure_is_short_without_password_and_leaves_nothing(tmp_path, monkeypatch, manual):
    client, bdir, db = manual
    _dump_fails(tmp_path, monkeypatch)
    r = client.post("/backups/create", headers=ADMIN)
    assert r.status_code == 500
    detail = r.json()["detail"]
    assert detail.startswith("Ошибка pg_dump: pg_dump: error:")
    for secret in (DB_PW, DB_PW_PLAIN, "s3cr3t"):
        assert secret not in r.text
    assert len(detail) <= 300 + 60
    assert _backup_files(bdir) == []
    assert db.log == [] and not db.committed


def test_create_backup_without_pg_dump_binary(tmp_path, monkeypatch, manual):
    client, bdir, db = manual
    monkeypatch.setenv("PATH", str(tmp_path / "пусто"))
    r = client.post("/backups/create", headers=ADMIN)
    assert r.status_code == 500
    assert r.json()["detail"] == "pg_dump не найден на сервере"
    assert _backup_files(bdir) == [] and db.log == []
