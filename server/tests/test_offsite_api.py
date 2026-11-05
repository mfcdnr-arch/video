"""HTTP «Копии на флешку»: /offsite/status, /password, /copy, /media, /download.

Запросы идут настоящим HTTP-клиентом (TestClient) с настоящим JWT. Настоящих Postgres и Redis нет:
World — «база» с состоянием (одна строка offsite_copy_state, media, audit_log), Session — её сессия,
которая применяет записи только по commit() (как настоящая: забытый commit перед отказом 403 здесь
теряет запись журнала, и тест это заметит). FakeRedis — словарь с set(ex=) / getdel. Остальное
настоящее: сборка копии (pg_dump подменён), файлы роликов на диске, tar, шифрование.

Что легко сломать незаметно и проверяется здесь: скачивание не отмечается, пока поток не отдан
целиком (обрыв не двигает «водяной знак»); ролик, загруженный во время скачивания, остаётся «новым»;
строка без файла не ломает Content-Length; пароль никогда не попадает в ответ и журнал; сменить пароль
на копию можно только зная текущий (кроме случая «ключ сервера сменился»).
"""
import asyncio
import gc
import io
import json
import os
import re
import tarfile
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace as NS

import pytest
import redis as redis_lib
from fastapi import FastAPI
from fastapi.testclient import TestClient

import offsite_store as store
import reset_offsite_password
from deps import create_access_token, get_db, pwd_context
from offsite_crypto import open_secret, seal_secret
from routers import offsite

NOW = datetime(2026, 11, 4, 9, 30, 15)
USERS = {"boss": "superadmin", "admin": "admin", "audit": "auditor",
         "mod": "moderator", "client": "advertiser"}
LOGIN_PW = "Login-pass-123"
LOGIN_HASH = pwd_context.hash(LOGIN_PW)             # bcrypt медленный: считаем один раз на весь файл
COPY_PW = "Copy-pass-2026!"
NEW_PW = "Brand-new-pass-77"
BAD_TICKET = "Ссылка недействительна или устарела — запросите скачивание заново"
REDIS_DOWN = "Хранилище одноразовых ссылок (Redis) недоступно — повторите позже"
NOT_MOUNTED = "Сервер не обновлён для копии на флешку: запустите install_server.sh"
ALL_PASSWORDS = (LOGIN_PW, COPY_PW, NEW_PW, "wrong-login-pass", "wrong-current-pass")


# --- «база» с состоянием ----------------------------------------------------------------------

class Row(dict):
    """Строка как у SQLAlchemy .mappings(): и dict, и атрибуты."""
    __getattr__ = dict.get


class Res:
    def __init__(self, rows):
        self._rows = list(rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows

    def first(self):
        return self.fetchone()

    def mappings(self):
        return self


def blank_state(**kw):
    s = {c: None for c in store._STATE_COLUMNS}
    s["media_watermark"] = 0
    s.update(kw)
    return s


class World:
    """То, что лежит в «базе» между сессиями: состояние копии, ролики, журнал."""

    def __init__(self):
        self.state = None                   # None — строки offsite_copy_state нет
        self.media = []
        self.audit = []                     # (event_type, title, detail, actor)
        self.counts = {"screens": 3, "media": 5, "media_docs": 1, "playlists": 2,
                       "users": 5, "advertisers": 1, "advertiser_docs": 0}
        self.sessions = []

    def session(self):
        s = Session(self)
        self.sessions.append(s)
        return s

    def titles(self):
        return [a[1] for a in self.audit]

    def mine(self):
        return self.state or blank_state()


class Session:
    def __init__(self, world):
        self.w = world
        self.pending = []
        self.closed = False
        self.commits = 0

    def commit(self):
        for fn in self.pending:
            fn()
        self.pending = []
        self.commits += 1

    def rollback(self):
        self.pending = []

    def close(self):
        self.closed = True
        self.pending = []                   # не закоммичено — пропало, как в настоящей сессии

    def execute(self, query, params=None):
        q = " ".join(str(query).split())
        p = params or {}
        w = self.w
        if "AS screens" in q:               # контрольные числа манифеста: один запрос с подзапросами
            return Res([NS(**w.counts)])
        if "FROM users" in q:
            if "password_hash" in q:
                return Res([NS(password_hash=LOGIN_HASH)] if p.get("u") in USERS else [])
            if "advertiser_id" in q:
                return Res([NS(advertiser_id=None)])
            name = p.get("username")
            if name not in USERS:
                return Res([])
            return Res([Row(username=name, role=USERS[name], is_active=True, is_blocked=False)])
        if "FROM offsite_copy_state" in q and q.startswith("SELECT"):
            return Res([NS(**w.state)] if w.state is not None else [])
        if q.startswith("INSERT INTO offsite_copy_state"):
            self.pending.append(lambda: self._upsert(q, dict(p)))
            return Res([])
        if q.startswith("UPDATE offsite_copy_state") and "password_enc = NULL" in q:
            self.pending.append(self._clear_password)
            return Res([])
        if q.startswith("INSERT INTO audit_log"):
            self.pending.append(lambda: w.audit.append((p["type"], p["t"], p["d"], p["a"])))
            return Res([])
        if "FROM media" in q:
            return Res(self._media(q, p))
        raise AssertionError("неожиданный запрос: " + q)

    def _upsert(self, q, p):
        st = dict(self.w.mine())
        if "password_enc" in q:
            st.update(password_enc=p["enc"], password_set_at=p["now"], password_set_by=p["who"])
        elif "last_copy_at" in q:
            st.update(last_copy_at=p["now"], last_copy_by=p["who"],
                      last_copy_file=p["fn"], last_copy_bytes=p["size"])
        elif "media_watermark" in q:
            st.update(media_watermark=max(st["media_watermark"], p["upto"]),   # GREATEST
                      last_media_at=p["now"], last_media_by=p["who"], last_media_scope=p["scope"],
                      last_media_count=p["cnt"], last_media_bytes=p["size"])
        else:
            raise AssertionError("неожиданная запись состояния: " + q)
        self.w.state = st

    def _clear_password(self):
        if self.w.state is not None:
            self.w.state = {**self.w.state, "password_enc": None,
                            "password_set_at": None, "password_set_by": None}

    def _media(self, q, p):
        docs_only = "status = 'document'" in q
        rows = [m for m in self.w.media if (m["status"] == "document") == docs_only]
        if q.startswith("SELECT COUNT"):
            after = p.get("after", 0)
            new = [m for m in rows if m["id"] > after]
            size = lambda ms: sum(m["filesize"] or 0 for m in ms)  # noqa: E731
            return [NS(new_count=len(new), new_bytes=Decimal(size(new)), all_count=len(rows),
                       all_bytes=Decimal(size(rows)), max_id=max((m["id"] for m in rows), default=0))]
        if "id > :after" in q:
            rows = [m for m in rows if p["after"] < m["id"] <= p["upto"]]
        return [NS(**{k: m[k] for k in ("id", "title", "filename", "filesize", "md5_hash")})
                for m in sorted(rows, key=lambda m: m["id"])]


class FakeRedis:
    """Словарь с теми же вызовами, что у redis-py; down — «Redis лёг»."""

    def __init__(self):
        self.data = {}
        self.down = None

    def set(self, key, value, ex=None):
        if self.down:
            raise self.down
        self.data[key] = value.encode() if isinstance(value, str) else value
        return True

    def getdel(self, key):
        if self.down:
            raise self.down
        return self.data.pop(key, None)


# --- окружение --------------------------------------------------------------------------------

def _write(path, data=b"x", mode=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    if mode is not None:
        path.chmod(mode)
    return path


def _fake_pg_dump(dst_path):
    import gzip
    with gzip.open(dst_path, "wb") as f:
        f.write(b"-- dump")


@pytest.fixture
def world():
    return World()


@pytest.fixture
def env(tmp_path, monkeypatch, world):
    project = tmp_path / "project"
    _write(project / ".env", b"SECRET_KEY=server-secret\nDB_PASSWORD=db-secret-0123456789\n")
    _write(project / "docker-compose.yml", b"services: {}\n")
    _write(project / "server" / "migrations" / "035_offsite_copy.sql", b"-- 035\n")
    media = tmp_path / "media"
    media.mkdir()
    out = tmp_path / "offsite_tmp"
    redis = FakeRedis()
    timers = []

    class FakeTimer:
        """Вместо настоящего threading.Timer на 16 минут: запоминаем и даём «сработать» вручную."""

        def __init__(self, interval, function, args=None, kwargs=None):
            self.interval, self.function, self.args = interval, function, tuple(args or ())
            self.daemon = False
            self.started = False
            timers.append(self)

        def start(self):
            self.started = True

        def fire(self):
            self.function(*self.args)
    monkeypatch.setattr("threading.Timer", FakeTimer)
    monkeypatch.setattr(store, "PROJECT_DIR", str(project))
    monkeypatch.setattr(store, "BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setattr(store, "MEDIA_PATH", str(media))
    monkeypatch.setattr(store, "AGENT_UPDATE_PATH", str(tmp_path / "agent"))
    monkeypatch.setattr(store, "OFFSITE_TMP", str(out))
    monkeypatch.setattr(store, "_pg_dump_gz", _fake_pg_dump)
    monkeypatch.setattr(store, "_redis", lambda: redis)
    monkeypatch.setattr(offsite, "MEDIA_PATH", str(media))
    monkeypatch.setattr(offsite, "SessionLocal", world.session)
    monkeypatch.setattr(offsite, "now_msk", lambda: NOW)
    monkeypatch.setenv("POSTGRES_USER", "display_user")
    monkeypatch.setenv("POSTGRES_DB", "display_system")
    return NS(project=project, media=media, out=out, redis=redis, tmp=tmp_path, timers=timers)


@pytest.fixture
def client(world, env):
    app = FastAPI()
    app.include_router(offsite.router)

    def _db():
        s = world.session()
        try:
            yield s
        finally:
            s.close()
    app.dependency_overrides[get_db] = _db
    return TestClient(app)


def bearer(username="boss"):
    return {"Authorization": "Bearer " + create_access_token({"sub": username})}


def with_copy_password(world, pw=COPY_PW, **kw):
    world.state = blank_state(password_enc=seal_secret(pw, offsite.SECRET_KEY),
                              password_set_at=datetime(2026, 11, 1, 8, 0), password_set_by="boss", **kw)


def add_media(world, env, id, size=100, *, status="ready", title=None, filename=None, file=True):
    name = filename or f"clip{id}.mp4"
    if file:
        base = name.replace("\\", "/").rsplit("/", 1)[-1]
        _write(env.media / base, bytes([id % 251]) * size)
    world.media.append({"id": id, "title": title or f"Ролик {id}", "filename": name,
                        "filesize": size, "md5_hash": f"{id:032x}", "status": status})


def post_copy(client, host=None, login=LOGIN_PW, user="boss"):
    headers = dict(bearer(user))
    if host:
        headers["Host"] = host
    return client.post("/offsite/copy", json={"login_password": login}, headers=headers)


def post_media(client, scope="new", user="boss"):
    return client.post("/offsite/media", json={"scope": scope}, headers=bearer(user))


def token_of(url):
    return url.split("t=", 1)[1]


def get_download(client, url):
    return client.get("/offsite/download", params={"t": token_of(url)})     # без заголовка входа


def tar_members(content):
    with tarfile.open(fileobj=io.BytesIO(content)) as tf:
        return {m.name: tf.extractfile(m).read() for m in tf.getmembers() if m.isfile()}


def tmp_is_empty(env):
    return (not env.out.exists()) or os.listdir(env.out) == []


# --- доступ -----------------------------------------------------------------------------------

REQUESTS = [
    ("GET", "/offsite/status", None),
    ("POST", "/offsite/password", {"new_password": NEW_PW, "new_password2": NEW_PW,
                                   "login_password": LOGIN_PW}),
    ("POST", "/offsite/copy", {"login_password": LOGIN_PW}),
    ("POST", "/offsite/media", {"scope": "all"}),
]


@pytest.mark.parametrize("user", ["admin", "audit", "mod", "client"])
@pytest.mark.parametrize("method,path,body", REQUESTS)
def test_non_superadmin_forbidden(client, world, env, user, method, path, body):
    """Копия содержит все пароли сервера: даже пишущий админ её не получит, только суперадмин."""
    with_copy_password(world)
    before = dict(world.state)
    r = client.request(method, path, json=body, headers=bearer(user))
    assert r.status_code == 403
    assert world.state == before and world.audit == []
    assert env.redis.data == {} and tmp_is_empty(env)


@pytest.mark.parametrize("method,path,body", REQUESTS)
def test_without_login_401(client, world, env, method, path, body):
    r = client.request(method, path, json=body)
    assert r.status_code == 401
    r = client.request(method, path, json=body, headers={"Authorization": "Bearer not-a-jwt"})
    assert r.status_code == 401
    assert world.state is None and tmp_is_empty(env)


# --- статус -----------------------------------------------------------------------------------

def test_status_shape_and_warn(client, world, env):
    r = client.get("/offsite/status", headers=bearer())
    assert r.status_code == 200
    j = r.json()
    assert set(j) == {"password", "copy", "media", "state", "project_mounted"}
    assert j["password"] == {"set": False, "set_at": None, "set_by": None, "needs_reset": False}
    assert j["copy"] == {"last_at": None, "last_by": None, "last_file": None,
                         "last_bytes": None, "today": False}
    assert j["media"] == {"new_count": 0, "new_bytes": 0, "all_count": 0, "all_bytes": 0,
                          "last_at": None, "last_by": None}
    assert j["state"] == "warn"                     # копии ещё не было
    assert j["project_mounted"] is True


def test_status_reports_everything_and_ok_after_todays_copy(client, world, env):
    add_media(world, env, 1, 1000)
    add_media(world, env, 2, 2000)
    add_media(world, env, 3, 500, status="document")        # «Документы» в счёт роликов не идут
    with_copy_password(world, last_copy_at=datetime(2026, 11, 4, 7, 5, 9), last_copy_by="boss",
                       last_copy_file="ds_copy_2026-11-04_0705.tar", last_copy_bytes=12345,
                       media_watermark=2, last_media_at=datetime(2026, 11, 3, 18, 0, 0),
                       last_media_by="admin")
    j = client.get("/offsite/status", headers=bearer()).json()
    assert j["password"] == {"set": True, "set_at": "2026-11-01T08:00:00", "set_by": "boss",
                             "needs_reset": False}
    assert j["copy"] == {"last_at": "2026-11-04T07:05:09", "last_by": "boss",
                         "last_file": "ds_copy_2026-11-04_0705.tar", "last_bytes": 12345, "today": True}
    assert j["media"] == {"new_count": 0, "new_bytes": 0, "all_count": 2, "all_bytes": 3000,
                          "last_at": "2026-11-03T18:00:00", "last_by": "admin"}
    assert j["state"] == "ok"
    # появился новый ролик — снова «warn»
    add_media(world, env, 4, 700)
    j = client.get("/offsite/status", headers=bearer()).json()
    assert (j["media"]["new_count"], j["media"]["new_bytes"]) == (1, 700)
    assert j["state"] == "warn"


def test_status_yesterdays_copy_is_not_today(client, world):
    with_copy_password(world, last_copy_at=datetime(2026, 11, 3, 23, 59, 0), last_copy_by="boss")
    j = client.get("/offsite/status", headers=bearer()).json()
    assert j["copy"]["today"] is False and j["state"] == "warn"


def test_status_never_returns_the_password(client, world):
    with_copy_password(world)
    enc = world.state["password_enc"]
    r = client.get("/offsite/status", headers=bearer())
    assert COPY_PW not in r.text and enc not in r.text and "password_enc" not in r.text


def test_status_project_not_mounted(client, env):
    (env.project / ".env").unlink()
    assert client.get("/offsite/status", headers=bearer()).json()["project_mounted"] is False


# --- пароль на копию --------------------------------------------------------------------------

def set_password(client, new=NEW_PW, new2=None, login=LOGIN_PW, current=None, user="boss"):
    body = {"new_password": new, "new_password2": new if new2 is None else new2,
            "login_password": login}
    if current is not None:
        body["current_password"] = current
    return client.post("/offsite/password", json=body, headers=bearer(user))


def test_set_password_first_time_then_change_needs_current(client, world):
    r = set_password(client)
    assert r.status_code == 200 and r.json() == {"status": "saved"}
    st = world.state
    # в базе лежит не пароль, а зашифрованное значение, которое открывается ключом сервера
    assert st["password_enc"] != NEW_PW and NEW_PW not in st["password_enc"]
    assert open_secret(st["password_enc"], offsite.SECRET_KEY) == NEW_PW
    assert (st["password_set_by"], st["password_set_at"]) == ("boss", NOW)
    assert world.titles() == ["Пароль на копию задан"]
    j = client.get("/offsite/status", headers=bearer()).json()
    assert j["password"] == {"set": True, "set_at": "2026-11-04T09:30:15", "set_by": "boss",
                             "needs_reset": False}

    # менять без текущего пароля на копию нельзя — даже зная пароль входа
    r = set_password(client, new="Another-pass-99")
    assert r.status_code == 403 and r.json()["detail"] == "Неверный текущий пароль на копию"
    r = set_password(client, new="Another-pass-99", current="wrong-current-pass")
    assert r.status_code == 403 and r.json()["detail"] == "Неверный текущий пароль на копию"
    assert open_secret(world.state["password_enc"], offsite.SECRET_KEY) == NEW_PW
    assert world.titles() == ["Пароль на копию задан"] + ["Неверный пароль при смене пароля на копию"] * 2

    r = set_password(client, new="Another-pass-99", current=NEW_PW)
    assert r.status_code == 200
    assert open_secret(world.state["password_enc"], offsite.SECRET_KEY) == "Another-pass-99"
    assert world.titles()[-1] == "Пароль на копию изменён"


def test_set_password_wrong_login_audited_without_secrets(client, world, caplog):
    with_copy_password(world)
    before = dict(world.state)
    r = set_password(client, login="wrong-login-pass", current=COPY_PW)
    assert r.status_code == 403 and r.json()["detail"] == "Неверный пароль входа"
    assert world.state == before                                    # пароль не тронут
    assert [a[:2] for a in world.audit] == [("backup", "Неверный пароль при смене пароля на копию")]
    assert world.audit[0][3] == "boss"
    # ни один пароль не попал ни в журнал, ни в ответ, ни в логи
    blob = " ".join(str(x) for a in world.audit for x in a) + r.text + caplog.text
    for secret in ALL_PASSWORDS:
        assert secret not in blob


def test_wrong_current_password_audited_without_secrets(client, world, caplog):
    with_copy_password(world)
    r = set_password(client, current="wrong-current-pass")
    assert r.status_code == 403
    assert [a[1] for a in world.audit] == ["Неверный пароль при смене пароля на копию"]
    blob = " ".join(str(x) for a in world.audit for x in a) + r.text + caplog.text
    for secret in ALL_PASSWORDS:
        assert secret not in blob


def test_login_password_is_checked_before_everything_else(client, world):
    """Чужой токен без пароля входа не узнает ни про «текущий пароль», ни про правила пароля."""
    with_copy_password(world)
    r = set_password(client, new="short", new2="other", login="wrong-login-pass", current="nope")
    assert r.status_code == 403 and r.json()["detail"] == "Неверный пароль входа"


@pytest.mark.parametrize("new,new2,detail", [
    (NEW_PW, "Another-pass-99", "Пароли не совпадают"),
    ("short", "short", "Пароль на копию — не короче 10 символов"),
    ("Пароль-на-копию-2026", "Пароль-на-копию-2026",
     "Только латинские буквы, цифры и знаки: такой пароль можно набрать в консоли любой машины"),
    ("with space 123456", "with space 123456",
     "Только латинские буквы, цифры и знаки: такой пароль можно набрать в консоли любой машины"),
])
def test_password_rules_400(client, world, new, new2, detail):
    r = set_password(client, new=new, new2=new2)
    assert r.status_code == 400 and r.json()["detail"] == detail
    assert world.state is None and world.audit == []


def test_mismatch_is_reported_before_strength(client):
    r = set_password(client, new="short", new2="other")
    assert r.status_code == 400 and r.json()["detail"] == "Пароли не совпадают"


def test_missing_fields_are_refused_not_crashed(client, world):
    r = client.post("/offsite/password", json={}, headers=bearer())
    assert r.status_code == 403                     # пустой пароль входа не подходит
    r = client.post("/offsite/password", json={"login_password": LOGIN_PW}, headers=bearer())
    assert r.status_code == 400
    assert world.state is None


# --- сменился SECRET_KEY ----------------------------------------------------------------------

@pytest.mark.parametrize("enc", [seal_secret(COPY_PW, "some-other-secret-key"), "garbage-not-a-token"])
def test_secret_key_changed_needs_reset(client, world, env, enc):
    world.state = blank_state(password_enc=enc, password_set_at=datetime(2026, 10, 1, 8, 0),
                              password_set_by="boss")
    j = client.get("/offsite/status", headers=bearer()).json()
    assert j["password"] == {"set": False, "set_at": "2026-10-01T08:00:00", "set_by": "boss",
                             "needs_reset": True}
    # копию этим паролем не запереть: скачать нельзя, пока не задан новый
    r = post_copy(client)
    assert r.status_code == 409 and r.json()["detail"] == "Сначала задайте пароль на копию"
    assert tmp_is_empty(env)
    # старым паролем проверить нечем, поэтому новый задаётся без текущего
    r = set_password(client)
    assert r.status_code == 200
    assert open_secret(world.state["password_enc"], offsite.SECRET_KEY) == NEW_PW
    j = client.get("/offsite/status", headers=bearer()).json()
    assert j["password"]["set"] is True and j["password"]["needs_reset"] is False
    assert world.titles() == ["Пароль на копию задан"]


# --- копия сервера ----------------------------------------------------------------------------

def test_copy_needs_password_409(client, world, env):
    r = post_copy(client)
    assert r.status_code == 409 and r.json()["detail"] == "Сначала задайте пароль на копию"
    assert tmp_is_empty(env) and env.redis.data == {}


def test_copy_wrong_login_403_audited(client, world, env, caplog):
    with_copy_password(world)
    r = post_copy(client, login="wrong-login-pass")
    assert r.status_code == 403 and r.json()["detail"] == "Неверный пароль входа"
    assert [a[:2] for a in world.audit] == [("backup", "Неверный пароль при скачивании копии")]
    assert world.audit[0][3] == "boss"
    blob = " ".join(str(x) for a in world.audit for x in a) + r.text + caplog.text
    for secret in ALL_PASSWORDS:
        assert secret not in blob
    assert tmp_is_empty(env) and env.redis.data == {}           # ничего не собиралось и ссылки нет


def test_copy_wrong_login_beats_missing_password(client, world):
    r = post_copy(client, login="wrong-login-pass")             # пароль на копию не задан
    assert r.status_code == 403


def test_copy_not_mounted_503(client, world, env):
    with_copy_password(world)
    (env.project / ".env").unlink()
    r = post_copy(client)
    assert r.status_code == 503 and r.json()["detail"] == NOT_MOUNTED
    assert tmp_is_empty(env) and env.redis.data == {}


def test_copy_build_failure_500_with_text(client, world, env, monkeypatch):
    with_copy_password(world)

    def boom(dst_path):
        raise RuntimeError("Ошибка pg_dump: connection refused")
    monkeypatch.setattr(store, "_pg_dump_gz", boom)
    r = post_copy(client)
    assert r.status_code == 500 and r.json()["detail"] == "Ошибка pg_dump: connection refused"
    assert tmp_is_empty(env) and env.redis.data == {}


def test_copy_disk_error_is_500_too(client, world, env, monkeypatch):
    with_copy_password(world)

    def boom(*a, **kw):
        raise OSError("No space left on device")
    monkeypatch.setattr(store, "encrypt_file_openssl", boom)
    r = post_copy(client)
    assert r.status_code == 500 and "No space left on device" in r.json()["detail"]
    assert COPY_PW not in r.text and tmp_is_empty(env)


def test_copy_cleans_stale_dirs_before_build(client, world, env):
    with_copy_password(world)
    old = env.out / "forgotten"
    _write(old / "ds_copy.tar", b"x")
    t = os.stat(old).st_mtime - 3600
    os.utime(old, (t, t))
    assert post_copy(client).status_code == 200
    assert not old.exists()


def test_copy_link_downloads_once_and_marks_done(client, world, env):
    with_copy_password(world)
    r = post_copy(client, host="10.0.119.100")
    assert r.status_code == 200
    j = r.json()
    assert set(j) == {"url", "filename", "bytes"}
    assert j["filename"] == "ds_copy_2026-11-04_0930.tar"
    assert re.fullmatch(r"/api/offsite/download\?t=[A-Za-z0-9_-]{32,}", j["url"])
    assert j["bytes"] > 0
    assert len(list(env.out.iterdir())) == 1
    # пока ссылку не открыли — ничего не «скачано»
    assert world.state["last_copy_at"] is None and world.titles() == []

    g = get_download(client, j["url"])                              # без входа: токен и есть право
    assert g.status_code == 200
    assert g.headers["content-type"].startswith("application/x-tar")
    assert g.headers["content-disposition"] == f'attachment; filename="{j["filename"]}"'
    assert g.headers["cache-control"] == "no-store"
    assert int(g.headers["content-length"]) == len(g.content) == j["bytes"]
    members = tar_members(g.content)                                # настоящий tar, читается целиком
    assert list(members) == ["ВОССТАНОВЛЕНИЕ.txt", "copy.tar.gz.enc", "copy.tar.gz.enc.sha256"]
    assert "10.0.119.100" in members["ВОССТАНОВЛЕНИЕ.txt"].decode("utf-8")      # адрес из заголовка Host
    assert members["copy.tar.gz.enc"].startswith(b"Salted__")
    assert COPY_PW.encode() not in g.content

    st = world.state
    assert (st["last_copy_at"], st["last_copy_by"], st["last_copy_file"], st["last_copy_bytes"]) == \
        (NOW, "boss", j["filename"], j["bytes"])
    assert [a[:2] for a in world.audit] == [("backup", "Копия на флешку скачана")]
    assert re.fullmatch(re.escape(j["filename"]) + r", \d+\.\d МБ", world.audit[0][2])
    assert world.audit[0][3] == "boss"
    assert client.get("/offsite/status", headers=bearer()).json()["copy"]["today"] is True
    assert tmp_is_empty(env)                                        # папка копии стёрта

    again = get_download(client, j["url"])                          # вторая попытка по той же ссылке
    assert again.status_code == 404 and again.json()["detail"] == BAD_TICKET
    assert world.titles() == ["Копия на флешку скачана"]


def test_all_sessions_are_closed_and_audit_committed(client, world, env):
    """Запись журнала при отказе должна пережить закрытие сессии запроса (commit до raise)."""
    with_copy_password(world)
    post_copy(client, login="wrong-login-pass")
    assert len(world.audit) == 1 and all(s.closed or not s.pending for s in world.sessions)


@pytest.mark.parametrize("token", ["", "nope", "A" * 43])
def test_download_unknown_token_404(client, token):
    r = client.get("/offsite/download", params={"t": token})
    assert r.status_code == 404 and r.json()["detail"] == BAD_TICKET


def test_download_without_token_param_404(client):
    r = client.get("/offsite/download")
    assert r.status_code == 404 and r.json()["detail"] == BAD_TICKET


def test_copy_redis_down_when_issuing_removes_built_copy(client, world, env):
    with_copy_password(world)
    env.redis.down = redis_lib.exceptions.ConnectionError("Error 111 connecting to redis:6379")
    r = post_copy(client)
    assert r.status_code == 503 and r.json()["detail"] == REDIS_DOWN
    assert tmp_is_empty(env)                                        # собранная копия не осталась лежать
    assert env.timers == []


def test_media_redis_down_503(client, world, env):
    add_media(world, env, 1)
    env.redis.down = redis_lib.exceptions.TimeoutError("Timeout reading from socket")
    r = post_media(client)
    assert r.status_code == 503 and r.json()["detail"] == REDIS_DOWN


def test_download_redis_down_503(client, env):
    env.redis.down = redis_lib.exceptions.ConnectionError("down")
    r = client.get("/offsite/download", params={"t": "x" * 43})
    assert r.status_code == 503 and r.json()["detail"] == REDIS_DOWN


def test_download_ignores_forged_workdir(client, world, env):
    """Токен выдаёт только наш POST. Но если в Redis кто-то подсунул чужую папку, мы её не отдаём и не стираем."""
    victim = env.tmp / "victim"
    _write(victim / "x.tar", b"secret data")
    tok = store.issue_ticket({"kind": "copy", "workdir": str(victim), "filename": "x.tar", "who": "boss"})
    r = client.get("/offsite/download", params={"t": tok})
    assert r.status_code == 404 and r.json()["detail"] == BAD_TICKET
    assert (victim / "x.tar").read_bytes() == b"secret data"
    assert world.audit == []


@pytest.mark.parametrize("payload", [
    {}, {"kind": "other"}, {"kind": "copy"}, {"kind": "copy", "workdir": 5, "filename": "x"},
    {"kind": "media"}, {"kind": "media", "scope": "new", "after": "a", "upto": 3, "filename": "x.tar"},
    {"kind": "media", "scope": "weird", "after": 0, "upto": 3, "filename": "x.tar", "who": "boss"},
])
def test_download_broken_payload_404(client, payload):
    tok = store.issue_ticket(payload)
    r = client.get("/offsite/download", params={"t": tok})
    assert r.status_code == 404 and r.json()["detail"] == BAD_TICKET


def test_copy_file_removed_before_download_404(client, world, env):
    """Папку успели стереть (чистка забытых) — честный 404, а не 500."""
    with_copy_password(world)
    j = post_copy(client).json()
    import shutil
    shutil.rmtree(next(env.out.iterdir()))
    r = get_download(client, j["url"])
    assert r.status_code == 404 and r.json()["detail"] == BAD_TICKET
    assert world.audit == []


def test_mark_failure_does_not_break_a_finished_download(client, world, env, monkeypatch, caplog):
    """Отдали всё, а отметить не вышло (база недоступна): файл у человека целый, ошибка только в логе."""
    with_copy_password(world)
    j = post_copy(client).json()

    def broken():
        raise RuntimeError("БД недоступна")
    monkeypatch.setattr(offsite, "SessionLocal", broken)
    g = get_download(client, j["url"])
    assert g.status_code == 200 and len(g.content) == j["bytes"]
    assert world.state["last_copy_at"] is None
    assert "не удалось отметить" in caplog.text
    assert tmp_is_empty(env)


# --- обрыв скачивания -------------------------------------------------------------------------

def _read_part_then_drop(world, token, chunks=2):
    """Прочитать начало потока и бросить — как закрытая вкладка. Ответ берём прямо из обработчика:
    TestClient дочитывает ответ до конца, а нам нужен именно обрыв посередине."""
    resp = offsite.download(t=token, db=world.session())
    got = []

    async def go():
        it = resp.body_iterator
        for _ in range(chunks):
            got.append(await it.__anext__())
        await it.aclose()
    asyncio.run(go())
    del resp
    gc.collect()
    return got


def test_copy_interrupted_does_not_mark_and_cleans_up(client, world, env, monkeypatch):
    monkeypatch.setattr(offsite, "CHUNK", 1024)
    with_copy_password(world)
    j = post_copy(client).json()
    got = _read_part_then_drop(world, token_of(j["url"]))
    assert len(got) == 2 and sum(map(len, got)) < j["bytes"]
    assert world.state["last_copy_at"] is None and world.titles() == []
    assert tmp_is_empty(env)                                        # недокачанная копия на сервере не остаётся
    assert get_download(client, j["url"]).status_code == 404        # и ссылка уже использована


def test_copy_response_never_read_is_cleaned_by_background(client, world, env):
    """Клиент пропал, не начав читать тело: генератор не стартовал и его finally не сработает.
    Задача после ответа подстраховывает — расшифрованная копия на диске не остаётся."""
    with_copy_password(world)
    j = post_copy(client).json()
    resp = offsite.download(t=token_of(j["url"]), db=world.session())
    asyncio.run(resp.background())
    assert tmp_is_empty(env)
    assert world.state["last_copy_at"] is None and world.titles() == []


def _asgi_get_then_disconnect(app, path, query, after_chunks):
    """Настоящий ASGI-вызов: клиент «отключается» после нескольких кусков тела."""
    sent = []

    async def go():
        gone = asyncio.Event()
        first = True

        async def receive():
            nonlocal first
            if first:
                first = False
                return {"type": "http.request", "body": b"", "more_body": False}
            await gone.wait()
            return {"type": "http.disconnect"}

        async def send(msg):
            sent.append(msg)
            if sum(1 for m in sent if m["type"] == "http.response.body") >= after_chunks:
                gone.set()
        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET",
                 "path": path, "raw_path": path.encode(), "query_string": query.encode(),
                 "headers": [(b"host", b"testserver")], "scheme": "http", "root_path": "",
                 "server": ("testserver", 80), "client": ("127.0.0.1", 5000)}
        await app(scope, receive, send)
    asyncio.run(go())
    return sent


def test_copy_client_disconnect_through_asgi(client, world, env, monkeypatch):
    monkeypatch.setattr(offsite, "CHUNK", 512)
    with_copy_password(world)
    j = post_copy(client).json()
    sent = _asgi_get_then_disconnect(client.app, "/offsite/download", "t=" + token_of(j["url"]), 2)
    gc.collect()
    bodies = [m for m in sent if m["type"] == "http.response.body"]
    assert sent[0]["status"] == 200
    assert sum(len(m["body"]) for m in bodies) < j["bytes"]         # поток оборван, не дочитан
    assert world.state["last_copy_at"] is None and world.titles() == []
    assert tmp_is_empty(env)


# --- ролики -----------------------------------------------------------------------------------

def test_media_new_flow_moves_watermark(client, world, env):
    add_media(world, env, 1, 1000)
    add_media(world, env, 2, 2000)
    add_media(world, env, 3, 3000)
    add_media(world, env, 4, 777, status="document")                # «Документы» — только в копии сервера
    with_copy_password(world, last_copy_at=datetime(2026, 11, 4, 7, 0, 0), last_copy_by="boss")
    j = client.get("/offsite/status", headers=bearer()).json()
    assert j["media"]["new_count"] == 3 and j["state"] == "warn"    # копия сегодня, но ролики не скачаны

    r = post_media(client, "new")
    assert r.status_code == 200
    m = r.json()
    assert set(m) == {"url", "filename", "count", "bytes", "missing"}
    assert (m["count"], m["bytes"], m["missing"]) == (3, 6000, 0)
    assert m["filename"] == "ds_media_new_2026-11-04_0930_id1-3.tar"
    assert re.fullmatch(r"/api/offsite/download\?t=[A-Za-z0-9_-]{32,}", m["url"])
    assert world.state["media_watermark"] == 0                      # ссылку выдали — знак не двигается

    g = get_download(client, m["url"])
    assert g.status_code == 200
    assert g.headers["content-type"].startswith("application/x-tar")
    assert g.headers["content-disposition"] == f'attachment; filename="{m["filename"]}"'
    assert g.headers["cache-control"] == "no-store"
    assert int(g.headers["content-length"]) == len(g.content)       # tar_size совпал с телом до байта
    members = tar_members(g.content)
    assert sorted(members) == ["media/MANIFEST_media.txt", "media/clip1.mp4", "media/clip2.mp4",
                               "media/clip3.mp4"]
    assert members["media/clip2.mp4"] == bytes([2]) * 2000
    manifest = members["media/MANIFEST_media.txt"].decode("utf-8").splitlines()
    assert manifest == [f"{i}\tРолик {i}\tclip{i}.mp4\t{i * 1000}\t{i:032x}" for i in (1, 2, 3)]

    st = world.state
    assert st["media_watermark"] == 3
    assert (st["last_media_at"], st["last_media_by"], st["last_media_scope"],
            st["last_media_count"], st["last_media_bytes"]) == (NOW, "boss", "new", 3, 6000)
    assert [a[:2] for a in world.audit] == [("backup", "Ролики скачаны на флешку")]
    assert re.fullmatch(r"новые, 3 шт, \d+\.\d МБ, id 1–3", world.audit[0][2])
    j = client.get("/offsite/status", headers=bearer()).json()
    assert j["media"]["new_count"] == 0 and j["media"]["last_by"] == "boss"
    assert j["media"]["last_at"] == "2026-11-04T09:30:15"
    assert j["state"] == "ok"                                       # копия сегодняшняя и новых роликов нет

    assert get_download(client, m["url"]).status_code == 404        # ссылка одноразовая
    r = post_media(client, "new")
    assert r.status_code == 409 and r.json()["detail"] == "Новых роликов нет"


def test_media_new_continues_after_watermark(client, world, env):
    for i in (1, 2, 3, 4, 5):
        add_media(world, env, i, 50)
    world.state = blank_state(media_watermark=3)
    m = post_media(client, "new").json()
    assert (m["count"], m["bytes"]) == (2, 100)
    assert m["filename"] == "ds_media_new_2026-11-04_0930_id4-5.tar"
    members = tar_members(get_download(client, m["url"]).content)
    assert sorted(members) == ["media/MANIFEST_media.txt", "media/clip4.mp4", "media/clip5.mp4"]
    assert world.state["media_watermark"] == 5


def test_media_all_flow(client, world, env):
    for i in (1, 2, 3):
        add_media(world, env, i, 50)
    world.state = blank_state(media_watermark=3)                    # всё уже скачано раньше
    m = post_media(client, "all").json()
    assert (m["count"], m["bytes"]) == (3, 150)
    assert m["filename"] == "ds_media_all_2026-11-04_0930.tar"
    g = get_download(client, m["url"])
    assert sorted(tar_members(g.content)) == ["media/MANIFEST_media.txt", "media/clip1.mp4",
                                              "media/clip2.mp4", "media/clip3.mp4"]
    assert world.state["media_watermark"] == 3                      # назад не откатывается
    assert world.state["last_media_scope"] == "all"
    assert re.fullmatch(r"все, 3 шт, \d+\.\d МБ, id 1–3", world.audit[0][2])


def test_media_interrupted_keeps_watermark(client, world, env, monkeypatch):
    monkeypatch.setattr(offsite, "CHUNK", 4096)
    for i in (1, 2, 3):
        add_media(world, env, i, 40_000)
    m = post_media(client, "new").json()
    got = _read_part_then_drop(world, token_of(m["url"]), chunks=3)
    assert len(got) == 3
    assert world.state is None or world.state["media_watermark"] == 0       # прежний
    assert world.state is None or world.state["last_media_at"] is None
    assert world.audit == []
    # ролики по-прежнему «новые»: в следующий раз их выдадут снова
    j = client.get("/offsite/status", headers=bearer()).json()
    assert j["media"]["new_count"] == 3
    assert post_media(client, "new").status_code == 200


def test_media_uploaded_during_download_stays_new(client, world, env):
    for i in (1, 2, 3):
        add_media(world, env, i, 50)
    m = post_media(client, "new").json()
    add_media(world, env, 4, 50)                                    # загрузили, пока шла выдача
    g = get_download(client, m["url"])
    assert sorted(tar_members(g.content)) == ["media/MANIFEST_media.txt", "media/clip1.mp4",
                                              "media/clip2.mp4", "media/clip3.mp4"]
    assert world.state["media_watermark"] == 3                      # не 4: ролика 4 на флешке нет
    j = client.get("/offsite/status", headers=bearer()).json()
    assert j["media"]["new_count"] == 1
    assert re.fullmatch(r"новые, 3 шт, \d+\.\d МБ, id 1–3", world.audit[0][2])


def test_media_missing_file_skipped(client, world, env):
    add_media(world, env, 1, 100)
    add_media(world, env, 2, 200, file=False)                       # строка есть, файла на диске нет
    add_media(world, env, 3, 300)
    m = post_media(client, "new").json()
    assert m["count"] == 3                                          # по базе
    assert m["missing"] == 1                                        # из них без файла на диске
    g = get_download(client, m["url"])
    assert g.status_code == 200
    assert int(g.headers["content-length"]) == len(g.content)       # размер посчитан по тем, что реально идут
    members = tar_members(g.content)
    assert sorted(members) == ["media/MANIFEST_media.txt", "media/clip1.mp4", "media/clip3.mp4"]
    lines = members["media/MANIFEST_media.txt"].decode("utf-8").splitlines()
    assert len(lines) == 3
    assert "нет файла" not in lines[0] and "нет файла" not in lines[2]
    assert lines[1].startswith("2\tРолик 2\tclip2.mp4\t") and lines[1].endswith("нет файла")
    # отметка — про то, что реально ушло на флешку; водяной знак при этом всё равно двигается
    # (потерянный файл повторным скачиванием не вернуть, и он назван в манифесте)
    assert world.state["media_watermark"] == 3
    assert world.state["last_media_count"] == 2
    assert world.state["last_media_bytes"] == 400
    # потерянное видно и в журнале, не только в манифесте
    assert re.fullmatch(r"новые, 2 шт, \d+\.\d МБ, id 1–3, нет файла: 1", world.audit[0][2])


def test_media_non_regular_file_counts_as_missing(client, world, env):
    add_media(world, env, 1, 50)
    add_media(world, env, 2, 50, file=False)
    (env.media / "clip2.mp4").mkdir()                               # на месте файла папка
    os.symlink(env.tmp / "nowhere", env.media / "clip3.mp4")
    world.media.append({"id": 3, "title": "Ссылка", "filename": "clip3.mp4", "filesize": 5,
                        "md5_hash": "", "status": "ready"})
    g = get_download(client, post_media(client, "all").json()["url"])
    assert sorted(tar_members(g.content)) == ["media/MANIFEST_media.txt", "media/clip1.mp4"]


def test_media_archive_names_are_basenames_only(client, world, env):
    add_media(world, env, 1, 30, filename="../../etc/evil.mp4")
    add_media(world, env, 2, 30, filename="sub\\dir\\win.mp4")
    add_media(world, env, 3, 30, filename="/abs/path/abs.mp4")
    add_media(world, env, 4, 30, filename="Ролик №1 — длинное имя.mp4")
    g = get_download(client, post_media(client, "all").json()["url"])
    names = sorted(tar_members(g.content))
    assert names == ["media/MANIFEST_media.txt", "media/abs.mp4", "media/evil.mp4",
                     "media/win.mp4", "media/Ролик №1 — длинное имя.mp4"]
    assert all(".." not in n.split("/") and not n.startswith("/") for n in names)


def test_media_same_file_twice_is_archived_once(client, world, env):
    add_media(world, env, 1, 40, filename="same.mp4")
    world.media.append({"id": 2, "title": "Копия", "filename": "same.mp4", "filesize": 40,
                        "md5_hash": "", "status": "ready"})
    g = get_download(client, post_media(client, "all").json()["url"])
    with tarfile.open(fileobj=io.BytesIO(g.content)) as tf:
        names = tf.getnames()
    assert names.count("media/same.mp4") == 1
    assert int(g.headers["content-length"]) == len(g.content)


def test_media_manifest_cells_stay_on_one_line(client, world, env):
    add_media(world, env, 1, 30, title="Строка1\nСтрока2\tс табом")
    g = get_download(client, post_media(client, "all").json()["url"])
    lines = tar_members(g.content)["media/MANIFEST_media.txt"].decode("utf-8").splitlines()
    assert lines == [f"1\tСтрока1 Строка2 с табом\tclip1.mp4\t30\t{1:032x}"]


def test_media_new_none_409(client, world, env):
    r = post_media(client, "new")                                   # пустая медиатека
    assert r.status_code == 409 and r.json()["detail"] == "Новых роликов нет"
    add_media(world, env, 1, 50)
    add_media(world, env, 2, 50, status="document")                 # документы новыми не считаются
    world.state = blank_state(media_watermark=1)
    r = post_media(client, "new")
    assert r.status_code == 409 and r.json()["detail"] == "Новых роликов нет"
    assert env.redis.data == {}


def test_media_all_on_empty_library_409(client):
    r = post_media(client, "all")
    assert r.status_code == 409
    assert r.json()["detail"] == "В медиатеке нет роликов"


@pytest.mark.parametrize("body", [{"scope": "everything"}, {"scope": ""}, {}, {"scope": "NEW"}])
def test_media_bad_scope_400(client, body):
    r = client.post("/offsite/media", json=body, headers=bearer())
    assert r.status_code == 400
    assert "new" in r.json()["detail"] and "all" in r.json()["detail"]


# --- забытые копии не лежат на диске --------------------------------------------------------

def _old_dir(env, name="forgotten", age_s=3600):
    d = env.out / name
    _write(d / "ds_copy.tar", b"x")
    t = os.stat(d).st_mtime - age_s
    os.utime(d, (t, t))
    return d


def test_status_sweeps_stale_dirs(client, env):
    """Копию собрали, ссылку так и не открыли (закрытая вкладка, 504): панель опрашивает статус, и
    забытая папка уходит без ожидания следующего «Скачать»."""
    old = _old_dir(env)
    fresh = env.out / "fresh"
    _write(fresh / "ds_copy.tar", b"x")
    assert client.get("/offsite/status", headers=bearer()).status_code == 200
    assert not old.exists()
    assert fresh.exists()                                           # свежую не трогаем


def test_status_for_non_superadmin_sweeps_nothing(client, env):
    old = _old_dir(env)
    assert client.get("/offsite/status", headers=bearer("admin")).status_code == 403
    assert old.exists()


def test_status_survives_sweep_failure(client, env, monkeypatch):
    def boom(*a, **kw):
        raise OSError("диск недоступен")
    monkeypatch.setattr(store, "cleanup_stale", boom)
    assert client.get("/offsite/status", headers=bearer()).status_code == 200


def test_copy_arms_expiry_timer_that_removes_workdir(client, world, env):
    with_copy_password(world)
    j = post_copy(client).json()
    assert len(env.timers) == 1
    t = env.timers[0]
    assert t.started and t.daemon                                   # демон: остановке сервера не мешает
    assert t.interval >= 15 * 60                                    # не раньше порога чистки по возрасту
    assert len(list(env.out.iterdir())) == 1
    t.fire()                                                        # «прошло 16 минут», ссылку не открыли
    assert tmp_is_empty(env)
    assert get_download(client, j["url"]).status_code == 404        # скачивать уже нечего


def test_expiry_also_sweeps_other_forgotten_dirs(client, world, env):
    with_copy_password(world)
    post_copy(client)
    old = _old_dir(env, "killed_build")
    env.timers[0].fire()
    assert not old.exists() and tmp_is_empty(env)


def test_expiry_callback_is_harmless_when_dir_is_gone(env):
    offsite._expire_copy(str(env.out / "no_such"))                  # не падает


def test_expiry_does_not_break_a_download_in_progress(client, world, env):
    """Таймер сработал, пока файл качают (большая копия по медленной флешке): открытый файл дочитается."""
    with_copy_password(world)
    j = post_copy(client).json()
    resp = offsite.download(t=token_of(j["url"]), db=world.session())
    env.timers[0].fire()
    assert tmp_is_empty(env)

    async def drain():
        return b"".join([c async for c in resp.body_iterator])
    body = asyncio.run(drain())
    assert len(body) == j["bytes"] and "copy.tar.gz.enc" in tar_members(body)
    assert world.state["last_copy_at"] == NOW                       # отдано целиком — отмечено


def test_no_timer_when_build_failed(client, world, env):
    with_copy_password(world)
    (env.project / ".env").unlink()
    assert post_copy(client).status_code == 503
    assert env.timers == []


def test_ticket_payload_has_no_password(client, world, env):
    with_copy_password(world)
    j = post_copy(client).json()
    (raw,) = env.redis.data.values()
    assert set(json.loads(raw)) == {"kind", "workdir", "filename", "who"}
    for secret in (COPY_PW, LOGIN_PW):
        assert secret not in raw.decode("utf-8")
    assert j["url"].split("t=")[1] in next(iter(env.redis.data))


@pytest.mark.parametrize("exc", [KeyError("неожиданно"), RuntimeError("redis-py сломался"), ValueError("x")])
def test_any_error_issuing_ticket_removes_built_copy(client, world, env, monkeypatch, exc):
    """Не только Redis: что бы ни упало после сборки, расшифрованная копия на диске не остаётся."""
    with_copy_password(world)

    def boom(payload, ttl=120):
        raise exc
    monkeypatch.setattr(store, "issue_ticket", boom)
    quiet = TestClient(client.app, raise_server_exceptions=False)
    r = post_copy(quiet)
    assert r.status_code == 500
    assert tmp_is_empty(env) and env.timers == []
    assert not offsite._build_lock.locked()


def test_timer_start_failure_removes_built_copy(client, world, env, monkeypatch):
    with_copy_password(world)

    def boom(workdir):
        raise RuntimeError("can't start new thread")
    monkeypatch.setattr(offsite, "_arm_expiry", boom)
    quiet = TestClient(client.app, raise_server_exceptions=False)
    assert post_copy(quiet).status_code == 500
    assert tmp_is_empty(env)


def test_startup_wipes_everything_in_offsite_tmp(client, env):
    """Процесс один: после перезапуска ни одна сборка не жива, всё в OFFSITE_TMP — остатки убитой
    (в них расшифрованный архив с .env). Свежесть не важна."""
    killed = env.out / "killed_build"
    _write(killed / store.BUILD_MARKER, b"")
    _write(killed / "copy.tar.gz", b"plaintext with .env")
    _write(env.out / "stray.tar", b"x")
    outside = env.tmp / "outside"
    _write(outside / "keep.txt", b"keep")
    os.symlink(outside, env.out / "link")
    with TestClient(client.app):                                    # вход в with запускает события старта
        assert os.listdir(env.out) == []
    assert (outside / "keep.txt").read_bytes() == b"keep"           # по ссылке наружу не ходим


def test_startup_hook_is_registered_and_import_wipes_nothing(client, env):
    assert offsite._wipe_on_start in offsite.router.on_startup
    leftover = env.out / "build"
    _write(leftover / "copy.tar.gz", b"x")
    client.get("/offsite/status", headers=bearer())                 # обычные запросы без запуска приложения
    assert leftover.exists()


def test_startup_wipe_failure_does_not_block_the_app(client, env, monkeypatch):
    def boom():
        raise OSError("нет доступа")
    monkeypatch.setattr(store, "cleanup_all", boom)
    with TestClient(client.app) as c:
        assert c.get("/offsite/status", headers=bearer()).status_code == 200


# --- одна сборка за раз ---------------------------------------------------------------------

def test_copy_busy_409_while_another_build_runs(client, world, env):
    with_copy_password(world)
    assert offsite._build_lock.acquire(blocking=False)
    try:
        r = post_copy(client)
    finally:
        offsite._build_lock.release()
    assert r.status_code == 409 and r.json()["detail"] == "Копия уже собирается — подождите и повторите"
    assert tmp_is_empty(env) and env.redis.data == {} and env.timers == []


def test_busy_check_comes_after_the_login_check(client, world):
    """Занятость сборки — не повод выдать её постороннему: сначала пароль входа."""
    with_copy_password(world)
    assert offsite._build_lock.acquire(blocking=False)
    try:
        r = post_copy(client, login="wrong-login-pass")
    finally:
        offsite._build_lock.release()
    assert r.status_code == 403
    assert [a[1] for a in world.audit] == ["Неверный пароль при скачивании копии"]


def test_second_click_during_real_build_gets_409(client, world, env, monkeypatch):
    """Настоящий сценарий: пока идёт сборка, приходит второй «Скачать» — собирается одна копия, не две."""
    with_copy_password(world)
    nested = {}

    def slow_dump(dst_path):
        _fake_pg_dump(dst_path)
        if "r" not in nested:                   # второй клик — один раз (без замка он сам запустил бы сборку)
            nested["r"] = None
            nested["r"] = post_copy(client)
    monkeypatch.setattr(store, "_pg_dump_gz", slow_dump)
    r = post_copy(client)
    assert r.status_code == 200
    assert nested["r"].status_code == 409
    assert len(env.timers) == 1 and len(list(env.out.iterdir())) == 1
    assert not offsite._build_lock.locked()


@pytest.mark.parametrize("exc", [store.ProjectNotMounted("нет .env"), RuntimeError("boom"),
                                 OSError("диск"), KeyError("неожиданно")])
def test_build_lock_released_after_failure(client, world, env, monkeypatch, exc):
    with_copy_password(world)

    real, calls = store.build_copy, []

    def flaky(*a, **kw):
        if not calls:
            calls.append(1)
            raise exc                                               # первая сборка падает, вторая — настоящая
        return real(*a, **kw)
    monkeypatch.setattr(store, "build_copy", flaky)
    quiet = TestClient(client.app, raise_server_exceptions=False)
    assert post_copy(quiet).status_code in (500, 503)
    assert not offsite._build_lock.locked()
    assert post_copy(client).status_code == 200                     # замок свободен — следующая сборка идёт


def test_build_lock_released_after_success(client, world):
    with_copy_password(world)
    assert post_copy(client).status_code == 200
    assert not offsite._build_lock.locked()
    assert post_copy(client).status_code == 200                     # и следующая сборка не упирается в замок


# --- ролики без файлов ----------------------------------------------------------------------

def test_media_all_files_missing_409_and_nothing_moves(client, world, env):
    """Архив без единого файла — не архив: ссылки нет, водяной знак на месте, напоминание не зеленеет."""
    for i in (1, 2, 3):
        add_media(world, env, i, 50, file=False)
    with_copy_password(world, last_copy_at=datetime(2026, 11, 4, 7, 0, 0), last_copy_by="boss")
    before = dict(world.state)
    for scope in ("new", "all"):
        r = post_media(client, scope)
        assert r.status_code == 409
        assert r.json()["detail"] == "Файлы роликов не найдены на диске — архив не собран"
    assert env.redis.data == {} and world.state == before and world.audit == []
    j = client.get("/offsite/status", headers=bearer()).json()
    assert j["media"]["new_count"] == 3 and j["state"] == "warn"


def test_media_files_vanish_before_download_404(client, world, env):
    for i in (1, 2, 3):
        add_media(world, env, i, 50)
    m = post_media(client, "new").json()
    for i in (1, 2, 3):
        os.remove(env.media / f"clip{i}.mp4")                       # файлы пропали, пока ссылку не открыли
    g = get_download(client, m["url"])
    assert g.status_code == 404
    assert g.json()["detail"] == "Файлы роликов не найдены на диске — архив не собран"
    assert world.state is None or world.state["media_watermark"] == 0
    assert world.audit == []


def test_media_rows_deleted_before_download_404(client, world, env):
    add_media(world, env, 1, 50)
    m = post_media(client, "new").json()
    world.media.clear()                                             # ролики удалили из медиатеки
    g = get_download(client, m["url"])
    assert g.status_code == 404
    assert world.state is None or world.state["last_media_at"] is None
    assert world.audit == []


def test_media_partial_loss_is_counted_in_post_and_audit(client, world, env):
    add_media(world, env, 1, 100)
    add_media(world, env, 2, 100, file=False)
    add_media(world, env, 3, 100, file=False)
    m = post_media(client, "new").json()
    assert (m["count"], m["missing"]) == (3, 2)
    g = get_download(client, m["url"])
    assert g.status_code == 200
    assert re.fullmatch(r"новые, 1 шт, \d+\.\d МБ, id 1–3, нет файла: 2", world.audit[0][2])
    assert world.state["media_watermark"] == 3 and world.state["last_media_count"] == 1


# --- сброс пароля на сервере ------------------------------------------------------------------

def test_reset_offsite_password_script(world, monkeypatch, capsys):
    with_copy_password(world)
    monkeypatch.setattr(reset_offsite_password, "SessionLocal", world.session)
    assert reset_offsite_password.main() == 0
    out = capsys.readouterr().out
    assert out.strip() == "Пароль на копию сброшен. Задайте новый в панели: Настройки → Копия на флешку."
    st = world.state
    assert st["password_enc"] is None and st["password_set_at"] is None and st["password_set_by"] is None
    assert [(a[0], a[1], a[3]) for a in world.audit] == [("backup", "Пароль на копию сброшен на сервере", "server")]


def test_reset_then_new_password_without_current(client, world, monkeypatch):
    with_copy_password(world)
    monkeypatch.setattr(reset_offsite_password, "SessionLocal", world.session)
    reset_offsite_password.main()
    j = client.get("/offsite/status", headers=bearer()).json()
    assert j["password"] == {"set": False, "set_at": None, "set_by": None, "needs_reset": False}
    assert set_password(client).status_code == 200                  # текущий пароль уже не нужен


def test_reset_script_reports_db_error(monkeypatch, capsys):
    def broken():
        raise RuntimeError("connection refused")
    monkeypatch.setattr(reset_offsite_password, "SessionLocal", broken)
    assert reset_offsite_password.main() == 1
    cap = capsys.readouterr()
    assert "connection refused" in cap.err and cap.out == ""


# --- проводка ---------------------------------------------------------------------------------

SERVER = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NGINX = os.path.join(os.path.dirname(SERVER), "nginx.conf")


def test_router_is_wired_in_main():
    with open(os.path.join(SERVER, "main.py"), encoding="utf-8") as f:
        text = f.read()
    imports = re.search(r"from routers import \((.*?)\)", text, flags=re.S).group(1)
    assert re.search(r"\boffsite\b", imports)
    assert "app.include_router(offsite.router)" in text
    assert text.index("app.include_router(backups.router)") < text.index("app.include_router(offsite.router)")


def test_routes_are_registered():
    paths = {(m, r.path) for r in offsite.router.routes for m in r.methods}
    assert paths == {("GET", "/offsite/status"), ("POST", "/offsite/password"), ("POST", "/offsite/copy"),
                     ("POST", "/offsite/media"), ("GET", "/offsite/download")}


@pytest.mark.skipif(not os.path.exists(NGINX),
                    reason="nginx.conf не смонтирован в контейнер тестов — проверяется на хосте")
def test_nginx_offsite_location_before_api_and_unbuffered():
    with open(NGINX, encoding="utf-8") as f:
        text = f.read()
    i_off = text.index("location /api/offsite/ {")
    i_api = text.index("location /api/ {")
    assert i_off < i_api
    block = text[i_off:text.index("\n        }\n", i_off)]
    for needle in ("rewrite ^/api/(.*)$ /$1 break;", "proxy_pass $ds_api;", "proxy_buffering off;",
                   "proxy_read_timeout 3600s;", "send_timeout 300s;", "proxy_http_version 1.1;", "proxy_set_header Host $host;",
                   "proxy_set_header X-Real-IP $remote_addr;"):
        assert needle in block, needle
    assert "proxy_read_timeout 300s" not in block       # сборка копии дольше 5 минут не должна давать 504
    assert "X-DS-Accel" not in block        # этот роутер X-Accel-Redirect не отдаёт
