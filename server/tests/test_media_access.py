"""Файлы медиатеки — только по входу (правило — media_access.py).

Ролики, баннеры, сканы договоров и миниатюры отдавались без входа, а номера
идут подряд: перебором выкачивалась вся медиатека, включая ролики на
модерации и отклонённые. Теперь:
  * GET /files/download/{id} — вошедший в панель (JWT или cookie ds_media) —
    любой файл; экран по X-Token — только готовый одобренный, прочее для него
    «не найдено»; за nginx ответ — X-Accel-Redirect (файл с Range отдаёт nginx);
  * GET /media/{id}/thumbnail и GET /files/auth (проверка nginx для /media/<имя>)
    — только вошедший в панель;
  * POST /session/media ставит httpOnly-cookie ds_media до конца входа,
    DELETE — снимает. Cookie подписана своим ключом: токен входа в cookie не
    годится, cookie как токен входа — тоже.
Без входа — 401 на любой номер (раньше 404), рекламодатель и заблокированный —
403.

Запросы идут настоящим HTTP-клиентом (TestClient) с настоящими JWT и cookie;
подменены только сессия БД и каталог медиа.
"""
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from jose import jwt

from deps import (ACCESS_TOKEN_EXPIRE_MINUTES, ALGORITHM, SECRET_KEY,
                  create_access_token, get_db, is_path_allowed_for_advertiser)
from media_access import MEDIA_COOKIE, device_may_read, make_media_cookie, media_cookie_user
from routers import auth as auth_mod
from routers import media as media_mod

# имя → (роль, заблокирован)
USERS = {
    "boss": ("superadmin", False),
    "admin": ("admin", False),
    "audit": ("auditor", False),
    "mod": ("moderator", False),
    "client": ("advertiser", False),
    "gone": ("admin", True),
}
DEVICE_TOKEN = "screen-token-abc"

# id → (имя файла, status, review_status); у 5 файла на диске нет
MEDIA = {
    1: ("1700000000_ab12cd_clip.mp4", "ready", "approved"),
    2: ("1700000001_ab12cd_pending.mp4", "ready", "pending"),
    3: ("1700000002_ab12cd_rejected.mp4", "ready", "rejected"),
    4: ("1700000003_ab12cd_договор.pdf", "document", "approved"),
    5: ("1700000004_ab12cd_lost.mp4", "ready", "approved"),
}
BODY = {1: b"CLIP-0123456789", 2: b"PENDING-CLIP", 3: b"REJECTED-CLIP", 4: b"%PDF-contract"}
THUMB = b"\xff\xd8\xff-thumb"


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


class MediaDB:
    """Сессия-мок: пользователи USERS, ролики MEDIA, экран с токеном DEVICE_TOKEN."""

    def execute(self, query, params=None):
        q = " ".join(str(query).split())
        p = params or {}
        if "FROM users" in q:
            name = p.get("username") or p.get("u")
            if name not in USERS:
                return _Res([])
            role, blocked = USERS[name]
            return _Res([_Row(username=name, role=role, is_active=True,
                              is_blocked=blocked, advertiser_id=None)])
        if "FROM screens" in q:
            return _Res([_Row(id=7)] if p.get("t") == DEVICE_TOKEN else [])
        if "FROM media" in q:
            m = MEDIA.get(p.get("id"))
            return _Res([_Row(filename=m[0], status=m[1], review_status=m[2])] if m else [])
        return _Res([])


@pytest.fixture
def client(tmp_path, monkeypatch):
    media = tmp_path / "media"
    thumbs = media / ".thumbs"
    thumbs.mkdir(parents=True)
    for mid, body in BODY.items():
        (media / MEDIA[mid][0]).write_bytes(body)
        (thumbs / f"{mid}.jpg").write_bytes(THUMB)
    monkeypatch.setattr(media_mod, "MEDIA_PATH", str(media))
    monkeypatch.setattr(media_mod, "THUMBS_DIR", str(thumbs))

    app = FastAPI()
    app.include_router(auth_mod.router)
    app.include_router(media_mod.router)
    app.dependency_overrides[get_db] = lambda: MediaDB()
    return TestClient(app)


def bearer(username, minutes=None):
    delta = timedelta(minutes=minutes) if minutes is not None else None
    return {"Authorization": "Bearer " + create_access_token({"sub": username}, delta)}


def cookie(value):
    return {"Cookie": f"{MEDIA_COOKIE}={value}"}


def in_hour():
    return datetime.now(timezone.utc) + timedelta(hours=1)


DOWNLOAD = "/files/download/{}"
THUMBNAIL = "/media/{}/thumbnail"
NGINX_CHECK = "/files/auth"
ALL_BODIES = list(BODY.values()) + [THUMB]


def leaked(r):
    return any(b in r.content for b in ALL_BODIES)


# --- без входа: 401, и номер не угадать -----------------------------------------

@pytest.mark.parametrize("url", [DOWNLOAD, THUMBNAIL])
@pytest.mark.parametrize("mid", [1, 2, 4, 2147483000])
def test_without_login_is_refused(client, url, mid):
    r = client.get(url.format(mid))
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == "Bearer"
    assert not leaked(r)


def test_nginx_check_without_login_is_refused(client):
    assert client.get(NGINX_CHECK).status_code == 401


@pytest.mark.parametrize("headers", [
    {"Authorization": "Bearer not-a-jwt"},
    {"Authorization": "Bearer " + jwt.encode({"sub": "admin"}, "чужой-ключ", algorithm=ALGORITHM)},
    bearer("admin", minutes=-1),                 # вход истёк
    bearer("nobody"),                            # такого пользователя нет
], ids=["мусор", "чужой ключ", "истёк", "нет пользователя"])
@pytest.mark.parametrize("url", [DOWNLOAD.format(1), THUMBNAIL.format(1), NGINX_CHECK])
def test_forged_or_expired_login_is_refused(client, url, headers):
    r = client.get(url, headers=headers)
    assert r.status_code == 401
    assert not leaked(r)


# --- по входу в панель ----------------------------------------------------------

@pytest.mark.parametrize("user", ["boss", "admin", "audit", "mod"])
@pytest.mark.parametrize("mid", [1, 2, 3, 4])
def test_panel_downloads_any_file(client, user, mid):
    # модератору нужен и ролик на модерации, админу — и скан договора
    r = client.get(DOWNLOAD.format(mid), headers=bearer(user))
    assert r.status_code == 200
    assert r.content == BODY[mid]
    assert r.headers["content-disposition"].startswith("attachment;")
    assert "x-accel-redirect" not in r.headers      # без nginx — файл из API


@pytest.mark.parametrize("user", ["boss", "admin", "audit", "mod"])
def test_panel_sees_thumbnails_and_passes_nginx_check(client, user):
    r = client.get(THUMBNAIL.format(2), headers=bearer(user))
    assert r.status_code == 200 and r.content == THUMB
    assert r.headers["cache-control"].startswith("private")
    assert client.get(NGINX_CHECK, headers=bearer(user)).status_code == 204


@pytest.mark.parametrize("user", ["client", "gone"], ids=["рекламодатель", "заблокирован"])
@pytest.mark.parametrize("url", [DOWNLOAD.format(1), THUMBNAIL.format(1), NGINX_CHECK])
def test_advertiser_and_blocked_are_forbidden(client, url, user):
    r = client.get(url, headers=bearer(user))
    assert r.status_code == 403
    assert not leaked(r)


def test_unknown_id_after_login_is_404(client):
    assert client.get(DOWNLOAD.format(2147483000), headers=bearer("admin")).status_code == 404
    assert client.get(THUMBNAIL.format(2147483000), headers=bearer("admin")).status_code == 404


def test_file_missing_on_disk_is_404(client):
    r = client.get(DOWNLOAD.format(5), headers=bearer("admin"))
    assert r.status_code == 404
    assert "на диске" in r.json()["detail"]


def test_head_for_diagnostics(client):
    # curl -I из runbook: HEAD не 405
    assert client.head(DOWNLOAD.format(1), headers={"X-Token": DEVICE_TOKEN}).status_code == 200
    assert client.head(DOWNLOAD.format(1)).status_code == 401


# --- экран по X-Token -----------------------------------------------------------

def test_screen_downloads_approved_file(client):
    r = client.get(DOWNLOAD.format(1), headers={"X-Token": DEVICE_TOKEN})
    assert r.status_code == 200
    assert r.content == BODY[1]


def test_screen_token_as_bearer_still_works(client):
    # запасной вариант агента, как у deps.verify_any_device_token
    r = client.get(DOWNLOAD.format(1), headers={"Authorization": "Bearer " + DEVICE_TOKEN})
    assert r.status_code == 200


@pytest.mark.parametrize("mid", [2, 3, 4], ids=["на модерации", "отклонён", "документ"])
def test_screen_gets_only_what_may_go_on_air(client, mid):
    r = client.get(DOWNLOAD.format(mid), headers={"X-Token": DEVICE_TOKEN})
    assert r.status_code == 404
    assert not leaked(r)


@pytest.mark.parametrize("url", [THUMBNAIL.format(1), NGINX_CHECK])
def test_screen_token_does_not_open_panel_files(client, url):
    assert client.get(url, headers={"X-Token": DEVICE_TOKEN}).status_code == 401


def test_unknown_screen_token_is_refused(client):
    r = client.get(DOWNLOAD.format(1), headers={"X-Token": "other-screen-token"})
    assert r.status_code == 401
    assert not leaked(r)


def test_device_may_read_rule():
    row = lambda status, review: _Row(status=status, review_status=review)
    assert device_may_read(row("ready", "approved"))
    assert not device_may_read(row("ready", "pending"))
    assert not device_may_read(row("ready", "rejected"))
    assert not device_may_read(row("document", "approved"))
    assert not device_may_read(row("processing", "approved"))


# --- cookie ds_media ------------------------------------------------------------

def test_media_session_requires_login(client):
    assert client.post("/session/media").status_code == 401
    assert client.post("/session/media", headers={"X-Token": DEVICE_TOKEN}).status_code == 401
    assert client.post("/session/media", headers=bearer("client")).status_code == 403


def test_media_session_cookie_opens_files(client):
    r = client.post("/session/media", headers=bearer("admin"))
    assert r.status_code == 200
    sc = r.headers["set-cookie"]
    assert sc.startswith(MEDIA_COOKIE + "=")
    low = sc.lower()
    assert "httponly" in low and "samesite=strict" in low and "path=/" in low
    assert "secure" not in low          # пилот по HTTP: с Secure браузер cookie не пошлёт
    # дальше — только cookie из банки клиента, без заголовка Authorization
    assert client.get(DOWNLOAD.format(2)).content == BODY[2]
    assert client.get(THUMBNAIL.format(1)).content == THUMB
    assert client.get(NGINX_CHECK).status_code == 204


def test_media_cookie_lives_no_longer_than_login(client):
    r = client.post("/session/media", headers=bearer("admin", minutes=10))
    assert 590 <= r.json()["expires_in"] <= 600
    max_age = int(r.headers["set-cookie"].lower().split("max-age=")[1].split(";")[0])
    assert 590 <= max_age <= 600
    r = client.post("/session/media", headers=bearer("admin"))
    assert r.json()["expires_in"] <= ACCESS_TOKEN_EXPIRE_MINUTES * 60


def test_media_session_drop_clears_cookie(client):
    client.post("/session/media", headers=bearer("admin"))
    assert client.get(NGINX_CHECK).status_code == 204
    r = client.delete("/session/media")
    assert r.status_code == 200
    assert 'max-age=0' in r.headers["set-cookie"].lower()
    assert client.get(NGINX_CHECK).status_code == 401


@pytest.mark.parametrize("url", [DOWNLOAD.format(1), THUMBNAIL.format(1), NGINX_CHECK])
def test_expired_media_cookie_is_refused(client, url):
    past = datetime.now(timezone.utc) - timedelta(seconds=1)
    r = client.get(url, headers=cookie(make_media_cookie("admin", past)))
    assert r.status_code == 401
    assert not leaked(r)


@pytest.mark.parametrize("value", [
    create_access_token({"sub": "admin"}),                                        # токен входа
    jwt.encode({"sub": "admin", "scope": "other", "exp": in_hour()},
               SECRET_KEY, algorithm=ALGORITHM),
    "мусор",
], ids=["токен входа", "чужая подпись", "мусор"])
@pytest.mark.parametrize("url", [DOWNLOAD.format(1), NGINX_CHECK])
def test_foreign_cookie_is_refused(client, url, value):
    assert client.get(url, headers=cookie(quote(value))).status_code == 401


def test_media_cookie_is_not_a_login_token(client):
    value = make_media_cookie("admin", in_hour())
    assert client.get("/me", headers={"Authorization": "Bearer " + value}).status_code == 401
    assert client.post("/session/media", headers={"Authorization": "Bearer " + value}).status_code == 401
    # и как Bearer к файлам: не вход и не токен экрана
    assert client.get(DOWNLOAD.format(1), headers={"Authorization": "Bearer " + value}).status_code == 401


@pytest.mark.parametrize("user,code", [("gone", 403), ("client", 403), ("nobody", 401)],
                         ids=["заблокирован", "рекламодатель", "удалён"])
def test_media_cookie_user_is_checked_on_every_request(client, user, code):
    # cookie выдана раньше, а пользователя с тех пор заблокировали / удалили
    r = client.get(DOWNLOAD.format(1), headers=cookie(make_media_cookie(user, in_hour())))
    assert r.status_code == code
    assert not leaked(r)


def test_media_cookie_roundtrip():
    assert media_cookie_user(make_media_cookie("audit", in_hour())) == "audit"
    assert media_cookie_user(None) is None
    assert media_cookie_user("") is None


# --- за nginx: X-Accel-Redirect -------------------------------------------------

ACCEL = {"X-DS-Accel": "1"}


@pytest.mark.parametrize("who", [{"X-Token": DEVICE_TOKEN}, bearer("admin")], ids=["экран", "панель"])
def test_behind_nginx_file_goes_via_x_accel(client, who):
    r = client.get(DOWNLOAD.format(1), headers={**ACCEL, **who, "Range": "bytes=5-"})
    assert r.status_code == 200
    assert r.content == b""                      # тело и Range отдаёт nginx
    assert r.headers["x-accel-redirect"] == "/_media_files/" + MEDIA[1][0]
    assert r.headers["content-type"] == "video/mp4"
    assert r.headers["content-disposition"] == f'attachment; filename="{MEDIA[1][0]}"'


def test_x_accel_quotes_non_ascii_name(client):
    r = client.get(DOWNLOAD.format(4), headers={**ACCEL, **bearer("admin")})
    assert r.headers["x-accel-redirect"] == "/_media_files/" + quote(MEDIA[4][0])
    assert r.headers["content-disposition"] == "attachment; filename*=utf-8''" + quote(MEDIA[4][0])


@pytest.mark.parametrize("start", [len(BODY[1]), len(BODY[1]) + 100])
def test_range_past_end_gets_whole_file(client, start):
    # обрывок у агента длиннее файла: вместо 416 на каждой синхронизации — файл целиком
    r = client.get(DOWNLOAD.format(1),
                   headers={**ACCEL, "X-Token": DEVICE_TOKEN, "Range": f"bytes={start}-"})
    assert r.status_code == 200
    assert r.content == BODY[1]
    assert "x-accel-redirect" not in r.headers


def test_refusal_behind_nginx_has_no_x_accel(client):
    r = client.get(DOWNLOAD.format(2), headers={**ACCEL, "X-Token": DEVICE_TOKEN})
    assert r.status_code == 404 and "x-accel-redirect" not in r.headers
    r = client.get(DOWNLOAD.format(1), headers=ACCEL)
    assert r.status_code == 401 and "x-accel-redirect" not in r.headers


# Рекламодателя к файлам не пускает ещё и middleware (белый список путей)
@pytest.mark.parametrize("path", ["/files/download/1", "/media/1/thumbnail", "/files/auth",
                                  "/session/media"])
def test_advertiser_paths_closed(path):
    assert is_path_allowed_for_advertiser(path) is False
