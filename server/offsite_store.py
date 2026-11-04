"""«Копия на флешку» на сервере: состояние в БД, ролики для архивов, журнал, одноразовые ссылки.

Здесь только SQL и Redis. Шифрование копии — offsite_crypto, правила и сборка архивов —
offsite_rules / offsite_tar (чистая логика), а HTTP-слой (роутер) склеивает всё это.

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
import json
import os
import secrets
from datetime import datetime

from sqlalchemy import text

from deps import pwd_context

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
    """Клиент Redis. Отдельная функция, чтобы тесты подменяли её словарём-заглушкой."""
    import redis
    return redis.Redis.from_url(os.getenv("REDIS_URL", "redis://redis:6379"))


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
