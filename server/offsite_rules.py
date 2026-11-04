"""Правила «Копии на флешку»: пароль на копию, напоминание, состав копии и имена файлов.

Чистая логика на stdlib, без БД, без fastapi и без os.environ. Все моменты —
наивное московское время (МСК), как и во всём проекте: `now` приходит параметром
(из `now_msk` в роутере), а не берётся здесь из часов.

Копию делает администратор: качает зашифрованный архив сервера и tar-архивы
медиа на флешку и уносит её с собой. Поэтому всё, что видит человек на флешке
(имена файлов, тексты отказов), собрано здесь в одном месте и проверено тестами.
"""
import re
from datetime import datetime

COPY_PASSWORD_MIN = 10                          # короче — подобрать слишком легко

# Пароль набирают в консоли `openssl` на любой машине, а не в нашей панели: там нет
# нашей раскладки и нет вставки кириллицы, пробел в конце легко потерять. Поэтому
# разрешены только печатные ASCII-знаки без пробела (коды 0x21–0x7E).
_PASSWORD_ALLOWED = re.compile(r"[\x21-\x7e]+")

_PASSWORD_TOO_SHORT = f"Пароль на копию — не короче {COPY_PASSWORD_MIN} символов"
_PASSWORD_NOT_ASCII = ("Только латинские буквы, цифры и знаки: "
                       "такой пароль можно набрать в консоли любой машины")

# Любой компонент пути с таким именем — служебное или мусор разработки: к работе
# сервера не относится, а в копию только раздувает архив (.git) или тащит чужое.
_EXCLUDED_DIRS = frozenset({
    ".git", ".claude", ".codex", ".superpowers", "backups", "__pycache__", "node_modules",
})
_EXCLUDED_NAMES = frozenset({".dev_admin_pass", ".DS_Store"})   # пароль стенда разработки и мусор macOS

# Старые выпуски-архивы в корне проекта: .tar с любым сжатием (.tar.gz, .tar.xz,
# .tar.bz2, .tar.zst…), а также .tgz и .zip. Перечислять сжатия поимённо нельзя —
# новый формат тихо попал бы в копию и раздул её на гигабайты, поэтому ищем «.tar»
# в конце имени или перед следующей точкой. «guitar.py» и «x.tarball» не подходят.
_ROOT_ARCHIVE = re.compile(r"\.tar(\.|$)|\.(tgz|zip)$", re.IGNORECASE)

DEFAULT_HOST = "адрес сервера"                  # подставляем в инструкцию, если своего адреса не узнали


def copy_password_problem(pw: str) -> str | None:
    """Почему пароль на копию не подходит (текст для пользователя); None — подходит.

    Сначала длина, потом набор знаков: короткий пароль надо удлинять в любом случае,
    а про раскладку скажем, когда длина уже в порядке.
    """
    if len(pw) < COPY_PASSWORD_MIN:
        return _PASSWORD_TOO_SHORT
    if not _PASSWORD_ALLOWED.fullmatch(pw):
        return _PASSWORD_NOT_ASCII
    return None


def reminder_state(last_copy_at: datetime | None, new_media_count: int, now: datetime) -> str:
    """Напоминание в панели: "ok" — копия сегодня и новых роликов с неё нет, иначе "warn".

    Сравниваются календарные даты, а не «прошло меньше суток»: копия в 23:59 вчера
    уже не сегодняшняя в 00:01. Любое новое медиа после копии — тоже повод "warn".
    """
    if last_copy_at is None:
        return "warn"
    if last_copy_at.date() == now.date() and new_media_count == 0:
        return "ok"
    return "warn"


def is_program_excluded(relpath: str) -> bool:
    """Не класть ли файл (путь от корня проекта, разделитель «/») в копию программы.

    Исключаем служебные папки и мусор, но НЕ `.env`: в нём пароли базы и MinIO и
    SECRET_KEY, без них восстановленный сервер не прочитает ни базу, ни пароль на
    копию, — копия зашифрована, так что класть секреты в неё безопасно.
    Архивы-выпуски (`.tar*`, `.tgz`, `.zip`) пропускаем только в корне: в подпапках
    это может быть обычный файл проекта (например, пример для документации).
    """
    parts = [p for p in relpath.split("/") if p and p != "."]
    if not parts:
        return False
    if any(p in _EXCLUDED_DIRS for p in parts):
        return True
    name = parts[-1]
    if name in _EXCLUDED_NAMES or name.endswith(".log"):
        return True
    return len(parts) == 1 and _ROOT_ARCHIVE.search(name) is not None


def copy_filename(now: datetime) -> str:
    """Имя файла копии сервера: ds_copy_2026-11-03_0915.tar (по времени создания)."""
    return f"ds_copy_{now:%Y-%m-%d_%H%M}.tar"


def media_filename(scope: str, now: datetime, after_id: int, upto_id: int) -> str:
    """Имя tar-архива медиа.

    "new" — только новые ролики: в имя входит диапазон id (`after_id + 1` … `upto_id`),
    чтобы по флешке было видно, какой кусок библиотеки в файле и с чего начинать
    следующий. "all" — вся библиотека, диапазон не нужен.
    """
    stamp = f"{now:%Y-%m-%d_%H%M}"
    if scope == "new":
        return f"ds_media_new_{stamp}_id{after_id + 1}-{upto_id}.tar"
    if scope == "all":
        return f"ds_media_all_{stamp}.tar"
    raise ValueError(f"Неизвестный объём медиа: {scope!r}")


def safe_host(host: str | None) -> str:
    """Адрес сервера для инструкции на флешке: только [A-Za-z0-9.-], без порта.

    Заголовок Host приходит от клиента, а адрес попадает в текст инструкции, поэтому
    всё лишнее (скобки, слэши, кавычки) отбрасываем. Порт отрезаем: в тексте
    инструкции нужен только адрес. Не осталось ничего (пусто, IPv6 в скобках) —
    «адрес сервера» как понятная подсказка человеку.
    """
    if not host:
        return DEFAULT_HOST
    cleaned = re.sub(r"[^A-Za-z0-9.-]", "", host.split(":", 1)[0])
    return cleaned or DEFAULT_HOST
