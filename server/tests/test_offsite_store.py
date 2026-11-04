"""offsite_store: состояние «Копии на флешку», ролики для архивов, журнал, одноразовые ссылки.

Настоящих Postgres и Redis здесь нет: OffsiteDB разбирает текст запроса и отдаёт строки из словаря,
FakeRedis — словарь с теми же вызовами set(ex=) / getdel, что у redis-py. Проверяется то, что
легко сломать незаметно: «водяной знак» роликов не откатывается назад (GREATEST), документы из
медиатеки не попадают в архивы, времена приходят из Python (не NOW() Postgres), ссылка
одноразовая и живёт 120 секунд, пароль проверяется bcrypt-хешем пользователя, а хранилище
commit не делает. Сам SQL и миграцию 035 против живой схемы гоняет стенд (migrate.sh).
"""
import json
import os
import re
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace as Row

import pytest

import offsite_store as store
from deps import pwd_context

N = datetime(2026, 11, 4, 9, 30)
SERVER = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MIGRATION = os.path.join(SERVER, "migrations", "035_offsite_copy.sql")
COMPOSE = os.path.join(os.path.dirname(SERVER), "docker-compose.yml")

# Колонки состояния без id — ровно ключи load_state (и колонки миграции 035).
STATE_KEYS = {
    "password_enc", "password_set_at", "password_set_by",
    "last_copy_at", "last_copy_by", "last_copy_file", "last_copy_bytes",
    "media_watermark", "last_media_at", "last_media_by", "last_media_scope",
    "last_media_count", "last_media_bytes",
}


class _Res:
    def __init__(self, rows):
        self._rows = list(rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows


class OffsiteDB:
    """Сессия-мок: одна строка состояния (None — строки нет), таблица media и пользователи.

    Запросы разбираются по тексту (пробелы схлопнуты); все вызовы копятся в calls, чтобы
    проверять и SQL, и параметры. media_rows фильтруется по after/upto из параметров, как
    это сделал бы Postgres, а проверка «документы не берём» — по тексту запроса.
    """

    def __init__(self, state=None, media=(), users=None):
        self.state = state
        self.media = list(media)
        self.users = users or {}
        self.calls: list[tuple[str, dict]] = []
        self.commits = 0
        self.rollbacks = 0

    def execute(self, query, params=None):
        q = " ".join(str(query).split())
        p = params or {}
        self.calls.append((q, p))
        if q.startswith("SELECT") and "FROM offsite_copy_state" in q:
            return _Res([Row(**self.state)] if self.state is not None else [])
        if "FROM users" in q:
            h = self.users.get(p.get("u"))
            return _Res([Row(password_hash=h)] if p.get("u") in self.users else [])
        if "FROM media" in q:
            return _Res(self._media(q, p))
        return _Res([])

    def _media(self, q, p):
        docs_only = "status = 'document'" in q
        skip_docs = "status <> 'document'" in q
        rows = [m for m in self.media
                if (not docs_only or m["status"] == "document")
                and (not skip_docs or m["status"] != "document")]
        if q.startswith("SELECT COUNT"):
            after = p.get("after", 0)
            new = [m for m in rows if m["id"] > after]
            size = lambda ms: sum(m["filesize"] or 0 for m in ms)  # noqa: E731
            return [Row(new_count=len(new), new_bytes=Decimal(size(new)),
                        all_count=len(rows), all_bytes=Decimal(size(rows)),
                        max_id=max((m["id"] for m in rows), default=0))]
        if "id > :after" in q:
            rows = [m for m in rows if p["after"] < m["id"] <= p["upto"]]
        return [Row(**{k: m[k] for k in ("id", "title", "filename", "filesize", "md5_hash")})
                for m in sorted(rows, key=lambda m: m["id"])]

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def params(self, needle):
        return [p for sql, p in self.calls if needle in sql]

    def sqls(self, needle):
        return [sql for sql, _ in self.calls if needle in sql]


def M(id, filesize=100, status="ready", title=None, filename=None):
    return {"id": id, "title": title or f"Ролик {id}", "filename": filename or f"{id}.mp4",
            "filesize": filesize, "md5_hash": f"{id:032x}", "status": status}


def state_row(**kw):
    """Строка состояния: всё пусто, водяной знак 0 — поверх неё то, что задано."""
    base = {k: None for k in STATE_KEYS}
    base["media_watermark"] = 0
    return {**base, **kw}


# --- миграция 035 и строка в initdb -------------------------------------------------------

def _migration():
    with open(MIGRATION, encoding="utf-8") as f:
        return f.read()


def test_migration_035_is_idempotent_single_row_table():
    sql = _migration()
    assert "CREATE TABLE IF NOT EXISTS offsite_copy_state" in sql
    # Одна строка на всю систему: id всегда 1 (проверка в схеме, а не только в коде).
    assert "id               SMALLINT PRIMARY KEY DEFAULT 1 CHECK (id = 1)" in sql
    assert "INSERT INTO offsite_copy_state (id) VALUES (1) ON CONFLICT (id) DO NOTHING" in sql
    assert "media_watermark  INTEGER NOT NULL DEFAULT 0" in sql
    # Моменты — наивное МСК из Python, поэтому без DEFAULT NOW() у наших колонок.
    code = "\n".join(l for l in sql.splitlines() if not l.lstrip().startswith("--"))
    assert "NOW()" not in code


def test_load_state_keys_match_migration_columns():
    """Ключи load_state = колонки миграции без id: разъехаться незаметно они не смогут."""
    body = _migration().split("CREATE TABLE IF NOT EXISTS offsite_copy_state (", 1)[1].split(");", 1)[0]
    cols = {m.group(1) for m in re.finditer(r"^\s+(\w+)\s+[A-Z]", body, re.M)} - {"id"}
    assert cols == STATE_KEYS
    assert set(store.load_state(OffsiteDB(state=state_row()))) == cols


@pytest.mark.skipif(not os.path.exists(COMPOSE),
                    reason="в контейнере ds_api / тестовом образе docker-compose.yml не виден")
def test_migration_listed_in_compose_initdb_once_after_034():
    with open(COMPOSE, encoding="utf-8") as f:
        text = f.read()
    line = ("      - ./server/migrations/035_offsite_copy.sql:"
            "/docker-entrypoint-initdb.d/35_offsite_copy.sql:ro")
    assert text.count("035_offsite_copy") == 1
    assert line in text.splitlines()
    assert text.index("034_ticker_redesign.sql") < text.index("035_offsite_copy.sql")


# --- состояние ----------------------------------------------------------------------------

def test_load_state_defaults_when_row_missing():
    st = store.load_state(OffsiteDB(state=None))
    assert set(st) == STATE_KEYS
    assert st["media_watermark"] == 0
    assert st["last_copy_at"] is None
    assert all(v is None for k, v in st.items() if k != "media_watermark")


def test_load_state_returns_stored_values():
    row = state_row(password_enc="gAAAA", password_set_by="boss", password_set_at=N,
                    last_copy_at=N, last_copy_by="boss", last_copy_file="copy.tar.gz.enc",
                    last_copy_bytes=123, media_watermark=57, last_media_scope="new",
                    last_media_count=3, last_media_bytes=999)
    st = store.load_state(OffsiteDB(state=row))
    assert st == row
    assert "id" not in st


def test_load_state_null_watermark_is_zero():
    """NOT NULL в схеме, но «нет водяного знака» везде должно читаться как 0, а не None."""
    assert store.load_state(OffsiteDB(state=state_row(media_watermark=None)))["media_watermark"] == 0


def test_save_password_stores_encrypted_value_who_and_msk_time():
    db = OffsiteDB()
    store.save_password(db, "ENC-TOKEN", "boss", N)
    (sql, p), = db.calls
    assert p == {"enc": "ENC-TOKEN", "who": "boss", "now": N}
    assert "offsite_copy_state" in sql and "password_enc" in sql
    # Строки могло не быть (удалили руками) — запись создаёт её, а не молча ничего не делает.
    assert "ON CONFLICT (id) DO UPDATE" in sql
    assert "NOW()" not in sql
    assert db.commits == 0


def test_clear_password_nulls_password_and_its_stamp():
    db = OffsiteDB()
    store.clear_password(db)
    (sql, p), = db.calls
    assert p == {}
    for col in ("password_enc", "password_set_at", "password_set_by"):
        assert f"{col} = NULL" in sql
    assert "WHERE id = 1" in sql
    assert db.commits == 0


def test_mark_copy_done_params():
    db = OffsiteDB()
    store.mark_copy_done(db, "boss", "copy_2026-11-04.tar.gz.enc", 4096, N)
    (sql, p), = db.calls
    assert p == {"who": "boss", "fn": "copy_2026-11-04.tar.gz.enc", "size": 4096, "now": N}
    assert "last_copy_at" in sql and "last_copy_file" in sql and "last_copy_bytes" in sql
    assert "ON CONFLICT (id) DO UPDATE" in sql
    assert "NOW()" not in sql
    # Пароль и водяной знак роликов копия не трогает.
    assert "password_enc" not in sql and "media_watermark" not in sql
    assert db.commits == 0


def test_mark_media_done_never_moves_back():
    db = OffsiteDB()
    store.mark_media_done(db, "boss", "new", 57, 3, 3_000_000, N)
    (sql, p), = db.calls
    assert p == {"who": "boss", "scope": "new", "upto": 57, "cnt": 3, "size": 3_000_000, "now": N}
    # Водяной знак только растёт: повторная выдача «всех» или более старого диапазона его не сбросит.
    assert "GREATEST(offsite_copy_state.media_watermark, EXCLUDED.media_watermark)" in sql
    assert "ON CONFLICT (id) DO UPDATE" in sql
    for col in ("last_media_at", "last_media_by", "last_media_scope", "last_media_count",
                "last_media_bytes"):
        assert col in sql
    assert "NOW()" not in sql
    assert db.commits == 0


# --- ролики -------------------------------------------------------------------------------

MEDIA = [M(1), M(2, status="document"), M(3, filesize=None), M(4), M(5, status="document"), M(6)]


def test_media_rows_excludes_documents_and_bounds():
    db = OffsiteDB(media=MEDIA)
    rows = store.media_rows(db, 1, 4)
    # after — не включается, upto — включается; документы (id 2) не попадают.
    assert [r.id for r in rows] == [3, 4]
    assert isinstance(rows, list)
    (sql, p), = db.calls
    assert p == {"after": 1, "upto": 4}
    assert "status <> 'document'" in sql
    assert "id > :after" in sql and "id <= :upto" in sql
    assert sql.endswith("ORDER BY id")
    assert {"id", "title", "filename", "filesize", "md5_hash"} <= set(vars(rows[0]))


def test_media_rows_null_filesize_is_zero_in_sql():
    db = OffsiteDB(media=MEDIA)
    rows = store.media_rows(db, 0, 6)
    assert [r.id for r in rows] == [1, 3, 4, 6]
    # NULL приводит к нулю уже в запросе — архиватор получает число, а не None.
    assert "COALESCE(filesize, 0) AS filesize" in db.calls[0][0]


def test_media_rows_empty_range():
    assert store.media_rows(OffsiteDB(media=MEDIA), 6, 6) == []


def test_media_totals_shape():
    db = OffsiteDB(media=MEDIA)
    t = store.media_totals(db, 3)
    assert set(t) == {"new_count", "new_bytes", "all_count", "all_bytes", "max_id"}
    # Документов (2, 5) нет нигде; «новые» — строго после водяного знака (3): ролики 4 и 6.
    assert t == {"new_count": 2, "new_bytes": 200, "all_count": 4, "all_bytes": 300, "max_id": 6}
    assert all(type(v) is int for v in t.values())     # Decimal из SUM() наружу не выходит
    (sql, p), = db.calls
    assert p == {"after": 3}
    assert "status <> 'document'" in sql
    assert "COALESCE" in sql


def test_media_totals_empty_library():
    t = store.media_totals(OffsiteDB(media=[M(2, status="document")]), 0)
    assert t == {"new_count": 0, "new_bytes": 0, "all_count": 0, "all_bytes": 0, "max_id": 0}


def test_document_rows_only_documents():
    db = OffsiteDB(media=MEDIA)
    rows = store.document_rows(db)
    assert [r.id for r in rows] == [2, 5]
    (sql, p), = db.calls
    assert "status = 'document'" in sql and "status <> 'document'" not in sql
    assert {"id", "title", "filename", "filesize"} <= set(vars(rows[0]))
    assert sql.endswith("ORDER BY id")


# --- журнал -------------------------------------------------------------------------------

def test_audit_writes_backup_event():
    db = OffsiteDB()
    store.audit(db, "Копия на флешку скачана", "copy.tar.gz.enc, 4 МБ", "boss")
    (sql, p), = db.calls
    assert "INSERT INTO audit_log (event_type, title, detail, actor)" in sql
    assert p == {"type": "backup", "t": "Копия на флешку скачана",
                 "d": "copy.tar.gz.enc, 4 МБ", "a": "boss"}
    assert store.AUDIT_TYPE == "backup"
    assert db.commits == 0


# --- проверка пароля входа -----------------------------------------------------------------

HASH = pwd_context.hash("s3cret-Pass")


def test_check_login_password():
    db = OffsiteDB(users={"boss": HASH})
    assert store.check_login_password(db, "boss", "s3cret-Pass") is True
    assert store.check_login_password(db, "boss", "wrong") is False
    assert store.check_login_password(db, "nobody", "s3cret-Pass") is False
    assert db.params("FROM users")[0] == {"u": "boss"}


def test_check_login_password_empty_password_is_false_without_query():
    db = OffsiteDB(users={"boss": HASH})
    assert store.check_login_password(db, "boss", "") is False
    assert store.check_login_password(db, "boss", None) is False
    assert db.calls == []


@pytest.mark.parametrize("bad_hash", [None, "", "not-a-bcrypt-hash"])
def test_check_login_password_broken_hash_is_false(bad_hash):
    """Пустой или битый хеш в users не должен ронять запрос ошибкой 500 — это просто «не подошёл»."""
    assert store.check_login_password(OffsiteDB(users={"boss": bad_hash}), "boss", "x") is False


# --- одноразовые ссылки (Redis) ----------------------------------------------------------

class FakeRedis:
    """Словарь с теми же вызовами, что у redis-py: set(key, value, ex=) и getdel(key); значения — bytes."""

    def __init__(self):
        self.data = {}
        self.set_calls = []

    def set(self, key, value, ex=None):
        self.set_calls.append((key, value, ex))
        self.data[key] = value.encode() if isinstance(value, str) else value
        return True

    def getdel(self, key):
        return self.data.pop(key, None)


@pytest.fixture
def fake_redis(monkeypatch):
    r = FakeRedis()
    monkeypatch.setattr(store, "_redis", lambda: r)
    return r


def test_ticket_single_use(fake_redis):
    payload = {"kind": "copy", "who": "boss", "файл": "копия.tar.gz.enc", "n": 7}
    t = store.issue_ticket(payload)
    assert store.take_ticket(t) == payload
    assert store.take_ticket(t) is None
    assert fake_redis.data == {}


def test_ticket_ttl_passed_to_redis(fake_redis):
    t = store.issue_ticket({"k": 1})
    key, value, ex = fake_redis.set_calls[0]
    assert ex == 120
    assert key == "offsite:ticket:" + t
    assert json.loads(value) == {"k": 1}
    store.issue_ticket({"k": 2}, ttl=30)
    assert fake_redis.set_calls[1][2] == 30


def test_tickets_are_unique_and_url_safe(fake_redis):
    tokens = {store.issue_ticket({"i": i}) for i in range(50)}
    assert len(tokens) == 50
    assert all(re.fullmatch(r"[A-Za-z0-9_-]{32,}", t) for t in tokens)


def test_unknown_or_empty_ticket_is_none(fake_redis):
    assert store.take_ticket("no-such-token") is None
    assert store.take_ticket("") is None
    assert store.take_ticket(None) is None


def test_corrupt_ticket_value_is_none_and_consumed(fake_redis):
    fake_redis.data["offsite:ticket:abc"] = b"{not json"
    assert store.take_ticket("abc") is None
    assert "offsite:ticket:abc" not in fake_redis.data
    fake_redis.data["offsite:ticket:lst"] = b"[1, 2]"    # не словарь — тоже не наша ссылка
    assert store.take_ticket("lst") is None


def test_redis_client_uses_redis_url(monkeypatch):
    import redis
    seen = []
    monkeypatch.setattr(redis.Redis, "from_url", staticmethod(lambda url, **kw: seen.append(url) or "client"))
    monkeypatch.setenv("REDIS_URL", "redis://example:6380")
    assert store._redis() == "client"
    monkeypatch.delenv("REDIS_URL")
    store._redis()
    assert seen == ["redis://example:6380", "redis://redis:6379"]
