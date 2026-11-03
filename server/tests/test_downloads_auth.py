"""Скачивание файлов только по входу в панель.

Бэкап — полный дамп БД (хеши паролей, X-Token всех экранов, рекламодатели,
счета), архив диагностики — логи и конфиг мини-ПК. Номера у обоих идут
подряд, поэтому открытая ссылка позволяла забрать всё перебором id без входа.
Теперь:
  * GET /backups/{id}/download — только пишущий админ (require_write);
  * GET /diagnostics/{id}/download — любой вошедший в панель (get_current_admin).
Токен экрана (X-Token) эти файлы не открывает, а без входа сервер не говорит
даже, есть ли такой номер (401 раньше 404).

Запросы идут настоящим HTTP-клиентом (TestClient) с настоящим JWT; подменена
только сессия БД.
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from deps import create_access_token, get_db
from routers import backups as backups_mod
from routers import screens as screens_mod

USERS = {
    "boss": "superadmin",
    "admin": "admin",
    "audit": "auditor",
    "mod": "moderator",
    "client": "advertiser",
}
DEVICE_TOKEN = "screen-token-abc"


class _Res:
    def __init__(self, rows):
        self._rows = rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def first(self):
        return self.fetchone()

    def mappings(self):
        return self


class _Row(dict):
    __getattr__ = dict.get


class FilesDB:
    """Сессия-мок: пользователи из USERS, один бэкап (id 1) и один архив
    диагностики (id 1); экран с токеном DEVICE_TOKEN."""

    def execute(self, query, params=None):
        q = " ".join(str(query).split())
        p = params or {}
        if "FROM users" in q:
            name = p.get("username") or p.get("u")
            if name not in USERS:
                return _Res([])
            return _Res([_Row(username=name, role=USERS[name], is_active=True,
                              is_blocked=False, advertiser_id=None)])
        if "FROM backups" in q:
            return _Res([_Row(filename="backup_test.sql.gz")] if p.get("id") == 1 else [])
        if "FROM screen_diagnostics" in q:
            return _Res([_Row(filename="diag_scr1_test.tar.gz")] if p.get("id") == 1 else [])
        if "FROM screens" in q:
            return _Res([_Row(id=1)] if p.get("t") == DEVICE_TOKEN else [])
        return _Res([])


@pytest.fixture
def client(tmp_path, monkeypatch):
    bdir = tmp_path / "backups"
    ddir = tmp_path / "diagnostics"
    bdir.mkdir()
    ddir.mkdir()
    (bdir / "backup_test.sql.gz").write_bytes(b"DUMP")
    (ddir / "diag_scr1_test.tar.gz").write_bytes(b"DIAG")
    monkeypatch.setattr(backups_mod, "BACKUP_DIR", str(bdir))
    monkeypatch.setattr(screens_mod, "DIAG_DIR", str(ddir))

    app = FastAPI()
    app.include_router(backups_mod.router)
    app.include_router(screens_mod.router)
    app.dependency_overrides[get_db] = lambda: FilesDB()
    return TestClient(app)


def bearer(username):
    return {"Authorization": "Bearer " + create_access_token({"sub": username})}


BACKUP = "/backups/{}/download"
DIAG = "/diagnostics/{}/download"


# --- без входа: 401, и номер не угадать ---------------------------------------

@pytest.mark.parametrize("url", [BACKUP, DIAG])
@pytest.mark.parametrize("item_id", [1, 2147483000])
def test_download_without_login_is_refused(client, url, item_id):
    r = client.get(url.format(item_id))
    assert r.status_code == 401
    assert b"DUMP" not in r.content and b"DIAG" not in r.content


@pytest.mark.parametrize("url", [BACKUP, DIAG])
def test_screen_token_does_not_open_admin_files(client, url):
    r = client.get(url.format(1), headers={"X-Token": DEVICE_TOKEN})
    assert r.status_code == 401


@pytest.mark.parametrize("url", [BACKUP, DIAG])
def test_forged_token_is_refused(client, url):
    r = client.get(url.format(1), headers={"Authorization": "Bearer not-a-jwt"})
    assert r.status_code == 401


# --- бэкап: только пишущий админ ------------------------------------------------

@pytest.mark.parametrize("user", ["boss", "admin"])
def test_backup_download_for_writer(client, user):
    r = client.get(BACKUP.format(1), headers=bearer(user))
    assert r.status_code == 200
    assert r.content == b"DUMP"
    assert "backup_test.sql.gz" in r.headers["content-disposition"]


@pytest.mark.parametrize("user", ["audit", "mod", "client"])
def test_backup_download_forbidden_for_read_only_roles(client, user):
    r = client.get(BACKUP.format(1), headers=bearer(user))
    assert r.status_code == 403
    assert r.content != b"DUMP"


def test_backup_unknown_id_after_login_is_404(client):
    assert client.get(BACKUP.format(2), headers=bearer("admin")).status_code == 404


# --- диагностика: любой вошедший в панель ---------------------------------------

@pytest.mark.parametrize("user", ["boss", "admin", "audit"])
def test_diagnostics_download_after_login(client, user):
    r = client.get(DIAG.format(1), headers=bearer(user))
    assert r.status_code == 200
    assert r.content == b"DIAG"


def test_diagnostics_unknown_id_after_login_is_404(client):
    assert client.get(DIAG.format(2), headers=bearer("audit")).status_code == 404


# Рекламодателя к обоим файлам не пускает ещё и middleware (белый список путей)
@pytest.mark.parametrize("path", ["/backups/1/download", "/diagnostics/1/download"])
def test_advertiser_paths_closed(path):
    from deps import is_path_allowed_for_advertiser
    assert is_path_allowed_for_advertiser(path) is False
