"""«Копия на флешку» на сервере: состояние в БД, ролики для архивов, журнал, одноразовые ссылки,
сборка ежедневной копии.

SQL и Redis — здесь же; шифрование копии — offsite_crypto, правила и потоковый tar —
offsite_rules / offsite_tar (чистая логика), а HTTP-слой (роутер) склеивает всё это.
Сборка ежедневной копии (build_copy) — внизу модуля: она берёт файлы с диска и дамп базы, а не только SQL.

Состояние — одна строка offsite_copy_state (id = 1): зашифрованный пароль на копию, кто и когда
скачал копию и ролики, и «водяной знак» роликов — наибольший id, который уже ушёл на флешку
(следующая выдача «новых» берёт всё, что больше). Записи делаются через INSERT … ON CONFLICT, а не
голый UPDATE: если строку когда-нибудь удалят руками, пароль и отметка о скачивании не потеряются
молча, а создадут её заново (load_state при этом тоже живёт без строки).

Все отметки времени — наивное МСК. Пишем их из Python (now приходит параметром, вызывающий берёт
now_msk), а не NOW() Postgres: контейнер БД живёт в UTC, и «скачано в 09:30» поехало бы на три часа.

Ролики со status='document' — это «Документы» медиатеки (сканы договоров), а не эфирные ролики:
в архивы роликов они не входят, их отдают отдельным списком document_rows.

Функции не делают commit — транзакцией управляет вызывающий (роутер).
"""
import hashlib
import io
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import tarfile
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterator
from urllib.parse import unquote, urlsplit

from sqlalchemy import text

from deps import DATABASE_URL, MEDIA_PATH, pwd_context
from offsite_crypto import OPENSSL_DECRYPT_CMD, encrypt_file_openssl
from offsite_rules import copy_filename, is_program_excluded, safe_host
from offsite_tar import TarItem, write_tar

AUDIT_TYPE = "backup"           # копия на флешку — того же рода событие, что и бэкап БД
TICKET_PREFIX = "offsite:ticket:"
TICKET_TTL = 120                # секунд: ссылка нужна только чтобы браузер начал скачивание

# Колонки состояния без id — ровно ключи load_state и колонки миграции 035.
_STATE_COLUMNS = (
    "password_enc", "password_set_at", "password_set_by",
    "last_copy_at", "last_copy_by", "last_copy_file", "last_copy_bytes",
    "media_watermark", "last_media_at", "last_media_by", "last_media_scope",
    "last_media_count", "last_media_bytes",
)


# --- состояние ----------------------------------------------------------------------------

def load_state(db) -> dict:
    """Состояние копии: {колонка: значение} без id. Нет строки — все None, а media_watermark = 0
    (то есть «ролики ещё ни разу не скачивали»)."""
    r = db.execute(text(f"SELECT {', '.join(_STATE_COLUMNS)} FROM offsite_copy_state WHERE id = 1")).fetchone()
    state = {c: (getattr(r, c) if r is not None else None) for c in _STATE_COLUMNS}
    state["media_watermark"] = int(state["media_watermark"] or 0)
    return state


def save_password(db, enc: str, who: str, now: datetime) -> None:
    """Запомнить пароль на копию. `enc` — уже зашифрованный (offsite_crypto), открытый пароль сюда не попадает."""
    db.execute(text("""
        INSERT INTO offsite_copy_state (id, password_enc, password_set_at, password_set_by)
        VALUES (1, :enc, :now, :who)
        ON CONFLICT (id) DO UPDATE SET
            password_enc = EXCLUDED.password_enc,
            password_set_at = EXCLUDED.password_set_at,
            password_set_by = EXCLUDED.password_set_by
    """), {"enc": enc, "who": who, "now": now})


def clear_password(db) -> None:
    """Забыть пароль на копию. Отметки «кто и когда задал» обнуляются вместе с ним: пароля нет — задавать нечего."""
    db.execute(text("""
        UPDATE offsite_copy_state
        SET password_enc = NULL, password_set_at = NULL, password_set_by = NULL
        WHERE id = 1
    """))


def mark_copy_done(db, who: str, filename: str, size: int, now: datetime) -> None:
    """Отметить, что копию сервера скачали: кто, когда, под каким именем и сколько байт."""
    db.execute(text("""
        INSERT INTO offsite_copy_state (id, last_copy_at, last_copy_by, last_copy_file, last_copy_bytes)
        VALUES (1, :now, :who, :fn, :size)
        ON CONFLICT (id) DO UPDATE SET
            last_copy_at = EXCLUDED.last_copy_at,
            last_copy_by = EXCLUDED.last_copy_by,
            last_copy_file = EXCLUDED.last_copy_file,
            last_copy_bytes = EXCLUDED.last_copy_bytes
    """), {"who": who, "fn": filename, "size": size, "now": now})


def mark_media_done(db, who: str, scope: str, upto_id: int, count: int, size: int, now: datetime) -> None:
    """Отметить, что ролики скачали: кто, когда, какой набор (scope: «new» / «all»), сколько штук и байт.

    Водяной знак двигается только вперёд (GREATEST): повторная выдача «всех» или более старого
    диапазона не заставит следующие «новые» снова тянуть уже скачанное.
    """
    db.execute(text("""
        INSERT INTO offsite_copy_state (id, media_watermark, last_media_at, last_media_by,
                                        last_media_scope, last_media_count, last_media_bytes)
        VALUES (1, :upto, :now, :who, :scope, :cnt, :size)
        ON CONFLICT (id) DO UPDATE SET
            media_watermark = GREATEST(offsite_copy_state.media_watermark, EXCLUDED.media_watermark),
            last_media_at = EXCLUDED.last_media_at,
            last_media_by = EXCLUDED.last_media_by,
            last_media_scope = EXCLUDED.last_media_scope,
            last_media_count = EXCLUDED.last_media_count,
            last_media_bytes = EXCLUDED.last_media_bytes
    """), {"who": who, "scope": scope, "upto": upto_id, "cnt": count, "size": size, "now": now})


# --- ролики -------------------------------------------------------------------------------

def media_rows(db, after_id: int, upto_id: int) -> list:
    """Ролики для архива: after_id < id <= upto_id, по возрастанию id. Строки: id, title, filename,
    filesize (NULL → 0), md5_hash. «Документы» не берём. Верхняя граница нужна затем, чтобы ролик,
    загруженный во время скачивания, не попал в архив и не оказался «уже скачанным», хотя его там нет."""
    return list(db.execute(text("""
        SELECT id, title, filename, COALESCE(filesize, 0) AS filesize, md5_hash
        FROM media
        WHERE status <> 'document' AND id > :after AND id <= :upto
        ORDER BY id
    """), {"after": after_id, "upto": upto_id}).fetchall())


def media_totals(db, after_id: int) -> dict:
    """Сколько роликов и байт: «новые» (id > after_id) и «все», плюс наибольший id (будущий водяной знак).
    Без «Документов»; пустая медиатека — нули. Числа — int (SUM в Postgres даёт Decimal)."""
    r = db.execute(text("""
        SELECT COUNT(*) FILTER (WHERE id > :after)                             AS new_count,
               COALESCE(SUM(COALESCE(filesize, 0)) FILTER (WHERE id > :after), 0) AS new_bytes,
               COUNT(*)                                                         AS all_count,
               COALESCE(SUM(COALESCE(filesize, 0)), 0)                          AS all_bytes,
               COALESCE(MAX(id), 0)                                             AS max_id
        FROM media
        WHERE status <> 'document'
    """), {"after": after_id}).fetchone()
    return {"new_count": int(r.new_count or 0), "new_bytes": int(r.new_bytes or 0),
            "all_count": int(r.all_count or 0), "all_bytes": int(r.all_bytes or 0),
            "max_id": int(r.max_id or 0)}


def document_rows(db) -> list:
    """«Документы» медиатеки (сканы договоров): id, title, filename, filesize — они идут в копию сервера, не в ролики."""
    return list(db.execute(text("""
        SELECT id, title, filename, COALESCE(filesize, 0) AS filesize
        FROM media
        WHERE status = 'document'
        ORDER BY id
    """)).fetchall())


# --- журнал и проверка пароля -------------------------------------------------------------

def audit(db, title: str, detail: str, who: str) -> None:
    """Запись в журнал действий (тип «backup»)."""
    db.execute(text("""
        INSERT INTO audit_log (event_type, title, detail, actor)
        VALUES (:type, :t, :d, :a)
    """), {"type": AUDIT_TYPE, "t": title, "d": detail, "a": who})


def check_login_password(db, username: str, password: str) -> bool:
    """Верен ли пароль входа пользователя (повторное подтверждение перед скачиванием копии).

    Нет пользователя, пустой пароль, пустой или нераспознаваемый хеш — просто False, а не ошибка.
    Заблокирован ли пользователь, не смотрим: вызывающий уже прошёл get_current_admin.
    """
    if not password:
        return False
    row = db.execute(text("SELECT password_hash FROM users WHERE username = :u"), {"u": username}).fetchone()
    if row is None or not row.password_hash:
        return False
    try:
        return bool(pwd_context.verify(password, row.password_hash))
    except ValueError:      # passlib: хеш не похож ни на одну известную схему
        return False


# --- одноразовые ссылки -------------------------------------------------------------------

def _redis():
    """Клиент Redis. Отдельная функция, чтобы тесты подменяли её словарём-заглушкой.

    Таймаут 2 секунды (как в routers/system.py): без него запрос к недоступному Redis висит минутами,
    а выдача ссылки — это обычный POST из панели, которому надо быстро ответить «Redis недоступен».
    """
    import redis
    return redis.Redis.from_url(os.getenv("REDIS_URL", "redis://redis:6379"),
                                socket_timeout=2, socket_connect_timeout=2)


def issue_ticket(payload: dict, ttl: int = TICKET_TTL) -> str:
    """Выдать одноразовую ссылку на скачивание: случайный токен, под ним в Redis лежит payload (JSON)
    и сам исчезает через ttl секунд. Браузер скачивает файл обычной ссылкой без заголовка
    Authorization, поэтому «входом» служит сам токен — его нельзя угадать, использовать дважды
    и держать долго. payload должен быть JSON-совместимым (даты — строками)."""
    token = secrets.token_urlsafe(32)
    _redis().set(TICKET_PREFIX + token, json.dumps(payload, ensure_ascii=False), ex=ttl)
    return token


def take_ticket(token: str) -> dict | None:
    """Забрать payload по токену и сразу удалить его (GETDEL — атомарно: два одновременных запроса
    не получат один payload дважды). Неизвестный, уже использованный, просроченный или испорченный
    токен — None."""
    if not token:
        return None
    raw = _redis().getdel(TICKET_PREFIX + token)
    if raw is None:
        return None
    try:
        payload = json.loads(raw)
    except ValueError:      # в т.ч. UnicodeDecodeError и JSONDecodeError
        return None
    return payload if isinstance(payload, dict) else None


# --- сборка ежедневной копии --------------------------------------------------------------
#
# Копия собирается во временной папке контейнера и отдаётся браузеру одним файлом (выдачу и удаление
# папки после неё делает роутер). На сервере она не хранится: в ней открытые пароли из .env, и копиться
# им незачем. Поэтому любая ошибка сборки стирает папку целиком, а забытые (ссылку так и не открыли)
# стирает cleanup_stale.

# Временные папки сборки. Не в /data: туда смонтированы тома с живыми данными, а здесь на минуту
# лежит расшифрованный архив со всеми паролями сервера — ему место в закрытой папке контейнера.
OFFSITE_TMP = "/tmp/ds_offsite"
# Корень проекта сервера: в docker-compose.yml у api он смонтирован только на чтение (./:/srv/project:ro).
# Оттуда берутся и программа, и сам файл .env байт в байт — значения из окружения не пересобираем.
PROJECT_DIR = os.getenv("OFFSITE_PROJECT_DIR", "/srv/project")
BACKUP_DIR = os.getenv("BACKUP_DIR", "/data/backups")
AGENT_UPDATE_PATH = os.getenv("AGENT_UPDATE_PATH", "/data/agent_updates")
RESTORE_TEMPLATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "offsite_restore.txt")

INSTRUCTION_NAME = "ВОССТАНОВЛЕНИЕ.txt"
INNER_NAME = "copy.tar.gz"                  # имена совпадают с OPENSSL_DECRYPT_CMD и с инструкцией
ENC_NAME = INNER_NAME + ".enc"
SHA_NAME = ENC_NAME + ".sha256"
# Маркер «сборка ещё идёт»: пустой файл в папке сборки. Дамп большой базы и шифрование могут идти
# дольше 15 минут, и без маркера cleanup_stale (его зовёт соседний запрос) стёр бы папку у собирающейся
# копии. Забытый маркер (процесс убили посреди сборки) через BUILD_MAX_AGE_S перестаёт защищать.
BUILD_MARKER = ".building"
BUILD_MAX_AGE_S = 6 * 3600
# Разделы копии: каждый кладём папкой даже пустым, чтобы `docker cp <папка>/.` в инструкции не падал.
_SECTIONS = ("program", "db", "documents", "media_documents", "agent_updates")

_MSK = timezone(timedelta(hours=3))
_CHUNK = 1 << 20

# Контрольные числа манифеста: (ключ, подпись, запрос). Ключ — он же имя столбца в _COUNTS_SQL,
# и тот же запрос стоит в инструкции (шаг «Проверка»): человек сверяет числа с манифестом глазами.
COUNT_FIELDS = (
    ("screens", "экраны", "SELECT count(*) FROM screens"),
    ("media", "ролики, без «Документов»", "SELECT count(*) FROM media WHERE status <> 'document'"),
    ("media_docs", "«Документы» медиатеки", "SELECT count(*) FROM media WHERE status = 'document'"),
    ("playlists", "плейлисты", "SELECT count(*) FROM playlists"),
    ("users", "пользователи", "SELECT count(*) FROM users"),
    ("advertisers", "рекламодатели", "SELECT count(*) FROM advertisers"),
    ("advertiser_docs", "документы рекламодателей", "SELECT count(*) FROM advertiser_documents"),
)
# Один запрос, а не семь: числа снимаются из одного снимка базы и согласованы между собой.
_COUNTS_SQL = "SELECT " + ", ".join(f"({sql}) AS {key}" for key, _, sql in COUNT_FIELDS)


class ProjectNotMounted(Exception):
    """Корень проекта не виден из контейнера api (нет .env или docker-compose.yml): сервер не обновлён."""


@dataclass
class BuiltCopy:
    """Готовая копия: внешний tar лежит в workdir (его вызывающий удаляет после отдачи)."""
    path: str
    filename: str
    size: int
    workdir: str


def _msk_ts(now: datetime) -> int:
    """Секунды unix для наивного МСК — время файлов в tar (иначе `ls -l` на новой машине соврёт на 3 часа)."""
    return int(now.replace(tzinfo=_MSK).timestamp())


def _one_line(value) -> str:
    """Текст в одну строку: имя из БД или логин не должны сломать вёрстку манифеста и инструкции."""
    return " ".join(str(value if value is not None else "").split())


def _remove_quiet(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def _pg_dump_gz(dst_path: str) -> None:
    """Снять дамп БД в dst_path: `pg_dump "$DATABASE_URL" | gzip`. Любая беда — RuntimeError с текстом
    stderr (до 300 символов, пароль БД из адреса вычищен); недописанный файл удаляется.

    Модульная функция, а не вложенная: тесты подменяют её, чтобы не нужен был настоящий Postgres.
    """
    ok = False
    try:
        with open(dst_path, "wb") as out:
            try:
                dump = subprocess.Popen(["pg_dump", DATABASE_URL],
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            except FileNotFoundError:
                raise RuntimeError("pg_dump не найден на сервере")
            try:
                gz = subprocess.Popen(["gzip"], stdin=dump.stdout, stdout=out)
            except FileNotFoundError:
                dump.kill()
                dump.wait()
                raise RuntimeError("gzip не найден на сервере")
            dump.stdout.close()                 # чтобы gzip увидел конец потока, когда pg_dump закончит
            _, err = dump.communicate()
            gz.wait()
        if dump.returncode != 0 or gz.returncode != 0:
            msg = err.decode(errors="ignore")
            # пароль БД стоит в адресе подключения; если pg_dump его где-то процитировал — прячем
            raw_pw = urlsplit(DATABASE_URL).password
            for secret in ({raw_pw, unquote(raw_pw)} if raw_pw else ()):
                if secret:
                    msg = msg.replace(secret, "***")
            raise RuntimeError("Ошибка pg_dump: " + msg.strip()[:300])
        ok = True
    finally:
        if not ok:
            _remove_quiet(dst_path)


def _check_project() -> None:
    for name in (".env", "docker-compose.yml"):
        if not os.path.isfile(os.path.join(PROJECT_DIR, name)):
            raise ProjectNotMounted(
                f"Папка проекта не подключена к серверу (нет {name} в {PROJECT_DIR}): "
                "обновите сервер, запустив install_server.sh на самом сервере")


def project_mounted() -> bool:
    """Виден ли корень проекта из контейнера (то же правило, что перед сборкой): для статуса в панели,
    чтобы «сервер не обновлён» было видно сразу, а не после нажатия «Скачать»."""
    try:
        _check_project()
    except ProjectNotMounted:
        return False
    return True


def _last_migration() -> str:
    """Наибольший номер NNN_ среди миграций проекта (как написан в имени файла), чтобы манифест и
    инструкция назвали версию базы. Нет миграций — «не определён»."""
    best = None
    try:
        names = os.listdir(os.path.join(PROJECT_DIR, "server", "migrations"))
    except OSError:
        names = []
    for name in names:
        m = re.match(r"(\d+)_", name)
        if m and (best is None or int(m.group(1)) > int(best)):
            best = m.group(1)
    return best or "не определён"


def _control_numbers(db) -> list[tuple[str, str, int]]:
    row = db.execute(text(_COUNTS_SQL)).fetchone()
    return [(key, label, int(getattr(row, key))) for key, label, _ in COUNT_FIELDS]


def _raise(err: OSError) -> None:
    raise err


def _iter_files(root: str, skip=None) -> Iterator[tuple[str, str, os.stat_result]]:
    """Обычные файлы папки root по порядку: (путь от root через «/», полный путь, lstat).

    Симлинки не берём и не идём по ним: ссылка из проекта наружу утащила бы в копию чужие файлы,
    а при восстановлении оказалась бы битой. Не обычные файлы (сокеты, каналы) тоже пропускаем.
    skip(путь) отсекает целые папки и файлы (правила is_program_excluded). Нет папки — пусто; папка,
    которую нельзя прочитать, — ошибка (копия молча без части файлов хуже, чем отказ).
    """
    if not os.path.isdir(root):
        return
    for cur, dirs, files in os.walk(root, topdown=True, onerror=_raise, followlinks=False):
        rel_dir = os.path.relpath(cur, root).replace(os.sep, "/")
        prefix = "" if rel_dir == "." else rel_dir + "/"
        dirs[:] = sorted(d for d in dirs if not (skip and skip(prefix + d)))
        for name in sorted(files):
            rel = prefix + name
            if skip and skip(rel):
                continue
            full = os.path.join(cur, name)
            try:
                st = os.lstat(full)
            except OSError:         # пропал между листингом и чтением — как будто и не было
                continue
            if stat.S_ISREG(st.st_mode):
                yield rel, full, st


class _HashingReader:
    """Обёртка над файлом для tarfile.addfile: считает sha256 тех байт, что реально ушли в архив."""

    def __init__(self, f):
        self._f = f
        self.sha = hashlib.sha256()

    def read(self, n: int = -1) -> bytes:
        buf = self._f.read(n)
        self.sha.update(buf)
        return buf


class _CopyArchive:
    """Внутренний copy.tar.gz: добавляет файлы и запоминает sha256 и размер каждого для описи.

    Файлы кладутся без владельцев хоста (uid/gid 0, имён нет): на новой машине их всё равно
    переназначит тот, кто распаковывает.
    """

    def __init__(self, path: str, now: datetime):
        self._mtime = _msk_ts(now)
        self._tf = tarfile.open(path, "w:gz", format=tarfile.PAX_FORMAT, encoding="utf-8", compresslevel=6)
        self.listing: list[tuple[str, int, str]] = []       # (sha256, размер, путь в архиве)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._tf.close()
        return False

    def _info(self, arcname: str, mode: int, mtime: int) -> tarfile.TarInfo:
        ti = tarfile.TarInfo(arcname)
        ti.mode = mode
        ti.mtime = mtime
        ti.uid = ti.gid = 0
        ti.uname = ti.gname = ""
        return ti

    def add_dir(self, arcname: str) -> None:
        ti = self._info(arcname, 0o755, self._mtime)
        ti.type = tarfile.DIRTYPE
        self._tf.addfile(ti)

    def add_file(self, arcname: str, path: str, st: os.stat_result, mode: int) -> None:
        ti = self._info(arcname, mode, int(st.st_mtime))
        ti.size = st.st_size
        with open(path, "rb") as f:
            reader = _HashingReader(f)
            # addfile читает ровно ti.size байт; файл короче заявленного — OSError, и сборка отменится
            self._tf.addfile(ti, reader)
        self.listing.append((reader.sha.hexdigest(), st.st_size, arcname))

    def add_bytes(self, arcname: str, data: bytes) -> None:
        """Небольшой текст из памяти (манифест). В опись не попадает: свой хеш в себя не вписать."""
        ti = self._info(arcname, 0o644, self._mtime)
        ti.size = len(data)
        self._tf.addfile(ti, io.BytesIO(data))


def safe_basename(filename) -> str | None:
    """Имя файла из БД → безопасное имя в папке раздела (media_documents/ в копии, media/ в архиве
    роликов): только последняя часть пути.

    Имя в БД — данные пользователя: «../x» или «/etc/passwd» не должны вывести за раздел. Обратные
    слэши тоже считаем разделителями (имя могли загрузить с Windows). None — имени нет вовсе.
    """
    name = str(filename or "").replace("\\", "/").rsplit("/", 1)[-1]
    if not name.strip() or name in (".", "..") or "\0" in name:
        return None
    return name


def _add_media_documents(ar: _CopyArchive, rows) -> list[str]:
    """Файлы «Документов» медиатеки в раздел media_documents/. Возвращает строки для манифеста про те
    строки БД, файла для которых на диске нет (или имя непригодно): их копия не восстановит."""
    missing, seen = [], set()
    for r in rows:
        base = safe_basename(r.filename)
        if base in seen:
            continue                    # тот же файл уже в копии (две строки БД на одно имя)
        st = None
        if base is not None:
            try:
                st = os.lstat(os.path.join(MEDIA_PATH, base))
            except OSError:
                st = None
        if st is None or not stat.S_ISREG(st.st_mode):
            shown = base or _one_line(r.filename) or "(пустое имя)"
            missing.append(f"  media_documents/{shown} - нет файла (id {r.id}, «{_one_line(r.title)}»)")
            continue
        seen.add(base)
        # 0644: эти файлы nginx отдаёт из /data/media, они должны читаться всеми внутри контейнера
        ar.add_file("media_documents/" + base, os.path.join(MEDIA_PATH, base), st, 0o644)
    return missing


def _manifest_text(*, now, who, host, migration, counts, listing, missing) -> str:
    lines = [
        "ОПИСЬ КОПИИ СЕРВЕРА DIGITAL SIGNAGE",
        "",
        f"Создана:        {now:%Y-%m-%d %H:%M:%S} (МСК)",
        f"Кем:            {_one_line(who)}",
        f"Адрес сервера:  {_one_line(host)}",
        f"Миграция БД:    {migration} (последняя в этой копии)",
        "",
        "Контрольные числа (число строк в таблицах базы сразу после снятия дампа).",
        "После восстановления те же числа печатает команда из шага «Проверка» инструкции:",
    ]
    lines += [f"  {key}: {value}    ({label})" for key, label, value in counts]
    lines += ["", "Не попали в копию (в базе строка есть, файла на диске нет):"]
    lines += missing or ["  ничего"]
    lines += ["", "Файлы копии: sha256, размер в байтах, путь (сам MANIFEST.txt в списке не значится):"]
    lines += [f"{sha}  {size}  {arcname}" for sha, size, arcname in listing]
    return "\n".join(lines) + "\n"


def _render_instruction(values: dict) -> str:
    """Шаблон инструкции с подставленными полями {{имя}}. Один проход: значение, похожее на поле
    (логин «{{host}}»), второй раз не раскрывается. Неизвестное поле остаётся как есть."""
    with open(RESTORE_TEMPLATE, encoding="utf-8") as f:
        template = f.read()
    return re.sub(r"\{\{(\w+)\}\}", lambda m: values.get(m.group(1), m.group(0)), template)


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            buf = f.read(_CHUNK)
            if not buf:
                return h.hexdigest()
            h.update(buf)


def build_copy(db, *, who: str, host: str, password: str, now: datetime) -> BuiltCopy:
    """Собрать ежедневную копию в OFFSITE_TMP/<uuid>/ и вернуть внешний tar ds_copy_*.tar.

    Внутри внешнего tar три открытых файла: инструкция, запертая паролем часть и её sha256. Запертая
    часть (формат openssl) после расшифровки — copy.tar.gz: program/ (с настоящим .env), свежий дамп,
    документы рекламодателей, «Документы» медиатеки, файлы OTA и MANIFEST.txt.

    Любая ошибка (pg_dump, диск, исчезнувший файл, БД) стирает папку сборки целиком: недособранная
    копия не должна лежать на диске. Пароль в тексты исключений не попадает. Транзакцией БД управляет
    вызывающий; здесь только чтение.
    """
    if not password:
        raise ValueError("Пароль на копию не задан")
    _check_project()
    filename = copy_filename(now)

    os.makedirs(OFFSITE_TMP, mode=0o700, exist_ok=True)
    os.chmod(OFFSITE_TMP, 0o700)
    workdir = os.path.join(OFFSITE_TMP, uuid.uuid4().hex)
    os.mkdir(workdir, 0o700)
    os.chmod(workdir, 0o700)            # на миг здесь лежит расшифрованный архив с .env: чужим закрыто
    try:
        open(os.path.join(workdir, BUILD_MARKER), "wb").close()
        # 1. дамп — первым: контрольные числа снимаем сразу за ним, чтобы они совпали с дампом
        dump_path = os.path.join(workdir, "dump.sql.gz")
        _pg_dump_gz(dump_path)
        counts = _control_numbers(db)
        doc_rows = document_rows(db)
        migration = _last_migration()

        # 2. внутренний copy.tar.gz
        inner_path = os.path.join(workdir, INNER_NAME)
        with _CopyArchive(inner_path, now) as ar:
            for section in _SECTIONS:
                ar.add_dir(section)
            for rel, full, st in _iter_files(PROJECT_DIR, skip=is_program_excluded):
                # .env — только владельцу, что бы ни стояло в источнике; остальным — их права
                mode = 0o600 if rel == ".env" else (st.st_mode & 0o777)
                ar.add_file("program/" + rel, full, st, mode)
            ar.add_file(f"db/backup_{now:%Y%m%d_%H%M%S}.sql.gz", dump_path, os.lstat(dump_path), 0o644)
            for rel, full, st in _iter_files(os.path.join(BACKUP_DIR, "documents")):
                ar.add_file("documents/" + rel, full, st, 0o644)
            missing = _add_media_documents(ar, doc_rows)
            for rel, full, st in _iter_files(AGENT_UPDATE_PATH):
                ar.add_file("agent_updates/" + rel, full, st, 0o644)
            ar.add_bytes("MANIFEST.txt", _manifest_text(
                now=now, who=who, host=safe_host(host), migration=migration, counts=counts,
                listing=ar.listing, missing=missing).encode("utf-8"))
        os.remove(dump_path)

        # 3. запираем и сразу убираем открытый архив
        enc_path = os.path.join(workdir, ENC_NAME)
        encrypt_file_openssl(inner_path, enc_path, password)
        os.remove(inner_path)

        # 4. внешний tar: инструкция + запертая часть + её контрольная сумма
        instruction = _render_instruction({
            "created_at": f"{now:%Y-%m-%d %H:%M}",
            "created_by": _one_line(who),
            "host": safe_host(host),
            "copy_file": filename,
            "decrypt_cmd": OPENSSL_DECRYPT_CMD,
            "migration": migration,
            "pg_user": os.getenv("POSTGRES_USER") or "display_user",
            "pg_db": os.getenv("POSTGRES_DB") or "display_system",
            "counts_sql": _COUNTS_SQL,
        }).encode("utf-8")
        sha_line = f"{_sha256_file(enc_path)}  {ENC_NAME}\n".encode("ascii")
        ts = _msk_ts(now)
        items = [
            TarItem(INSTRUCTION_NAME, len(instruction), data=instruction, mtime=ts),
            TarItem(ENC_NAME, os.path.getsize(enc_path), path=enc_path, mtime=ts),
            TarItem(SHA_NAME, len(sha_line), data=sha_line, mtime=ts),
        ]
        out_path = os.path.join(workdir, filename)
        size = write_tar(items, out_path)
        os.remove(enc_path)             # остаётся только внешний tar
        os.remove(os.path.join(workdir, BUILD_MARKER))      # собрано: дальше это обычная готовая папка
        return BuiltCopy(path=out_path, filename=filename, size=size, workdir=workdir)
    except BaseException:
        shutil.rmtree(workdir, ignore_errors=True)
        raise


def cleanup_stale(older_than_s: int = 900) -> int:
    """Удалить папки сборки в OFFSITE_TMP старше older_than_s секунд (по умолчанию 15 минут); вернуть
    сколько удалено. Нужна для копий, которые собрали, а ссылку так и не открыли. Трогает только
    настоящие папки: файл или симлинк в OFFSITE_TMP — чужое, мы его не создавали.

    Папку с маркером BUILD_MARKER (сборка ещё идёт) не трогает, пока маркеру нет BUILD_MAX_AGE_S:
    сборка большой базы дольше 15 минут — не мусор. Старше — процесс умер, папку стираем.
    """
    try:
        names = os.listdir(OFFSITE_TMP)
    except OSError:
        return 0
    cutoff = time.time() - older_than_s
    removed = 0
    for name in names:
        path = os.path.join(OFFSITE_TMP, name)
        try:
            if os.path.islink(path) or not os.path.isdir(path) or os.stat(path).st_mtime > cutoff:
                continue
            marker = os.path.join(path, BUILD_MARKER)
            if os.path.isfile(marker) and os.stat(marker).st_mtime > time.time() - BUILD_MAX_AGE_S:
                continue
        except OSError:
            continue
        shutil.rmtree(path, ignore_errors=True)
        if not os.path.exists(path):
            removed += 1
    return removed


def cleanup_all() -> int:
    """Стереть всё содержимое OFFSITE_TMP (саму папку оставить); вернуть сколько записей удалено.

    Зовётся при старте API: процесс один, сборка не переживает его перезапуск, поэтому всё, что осталось
    в OFFSITE_TMP, — недособранная или забытая копия (внутри расшифрованный архив с .env). В отличие
    от cleanup_stale возраст не смотрит. Симлинк и файл просто удаляются, по ссылке наружу не идём.
    """
    try:
        names = os.listdir(OFFSITE_TMP)
    except OSError:
        return 0
    removed = 0
    for name in names:
        path = os.path.join(OFFSITE_TMP, name)
        try:
            if os.path.islink(path) or not os.path.isdir(path):
                os.remove(path)
            else:
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            continue
        if not os.path.lexists(path):
            removed += 1
    return removed
