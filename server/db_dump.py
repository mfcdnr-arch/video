"""Дамп базы для резервных копий: ночной (Celery, tasks.nightly_backup), ручной (кнопка в Настройках,
routers/backups.py) и дамп внутри «Копии на флешку» (offsite_store) — все снимаются одной функцией
pg_dump_gz.

Что она гарантирует:
  * пароль БД не попадает в командную строку pg_dump (её видит любой, кто смотрит `ps` / `docker top`):
    в argv — адрес без пароля, сам пароль — в окружении процесса (PGPASSWORD);
  * дамп заливается в PostgreSQL 15 с `psql -v ON_ERROR_STOP=1`: из шапки убрана строка
    `SET transaction_timeout = 0;`, которую пишет pg_dump 17 из образа api (см. ниже);
  * дамп не ждёт чужую блокировку таблицы бесконечно (`--lock-wait-timeout=60s`);
  * любая беда — RuntimeError с коротким русским текстом без пароля БД; недописанный файл удаляется.

Модуль не импортирует deps.py: Celery-воркер берёт его без остального кода сервера.
"""
import gzip
import os
import shutil
import subprocess
import threading
from urllib.parse import unquote, urlsplit, urlunsplit

# pg_dump в образе api новее сервера PostgreSQL 15: он пишет в шапку дампа `SET transaction_timeout = 0;`,
# а PostgreSQL 15 такого параметра не знает («unrecognized configuration parameter»). Инструкция
# восстановления заливает дамп через `psql -v ON_ERROR_STOP=1`, и без правки она встала бы на этой строке.
# Параметр — только «снять лимит по умолчанию», так что строку просто не пишем в дамп. Правим строго
# шапку: пока идут пустые строки, `--`-комментарии, `SET …`, `SELECT pg_catalog.set_config(…)` и команды
# psql (`\restrict …`); на первом настоящем операторе шапка кончается, и дальше всё идёт байт в байт —
# те же слова внутри данных или тела функции это содержимое базы, его трогать нельзя.
_DUMP_UNSUPPORTED_LINE = b"SET transaction_timeout = 0;"
_DUMP_PREAMBLE_PREFIXES = (b"--", b"SET ", b"SELECT pg_catalog.set_config(", b"\\")
_DUMP_LINE_MAX = 64 * 1024              # строка шапки короткая; длиннее — это уже не шапка
_DUMP_CHUNK = 1024 * 1024               # остальной дамп копируем кусками: память не растёт с размером базы
_DUMP_STDERR_KEEP = 64 * 1024           # от stderr pg_dump нужно только начало для текста ошибки

# Сколько pg_dump ждёт блокировку таблицы. Таблицу держит долгая операция (миграция, ALTER TABLE) —
# без предела дамп (а с ним сборка копии и её замок «копия уже собирается», запрос кнопки «Создать
# бэкап», ночная задача Celery) ждал бы без конца; с пределом pg_dump через минуту падает с понятной
# ошибкой, и человек просто повторяет позже.
PG_DUMP_LOCK_WAIT = "60s"


def _remove_quiet(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def _is_dump_preamble_line(line: bytes) -> bool:
    body = line.rstrip(b"\r\n")
    return body == b"" or body.startswith(_DUMP_PREAMBLE_PREFIXES)


def _copy_dump_stream(src, dst) -> None:
    """Переписать дамп из src в dst, выбросив из шапки строку `SET transaction_timeout = 0;`."""
    while True:
        line = src.readline(_DUMP_LINE_MAX)
        if not line:
            return
        cut_mid_line = len(line) >= _DUMP_LINE_MAX and not line.endswith(b"\n")
        if cut_mid_line or not _is_dump_preamble_line(line):
            dst.write(line)             # первый оператор: шапка кончилась
            break
        if line.rstrip(b"\r\n") != _DUMP_UNSUPPORTED_LINE:
            dst.write(line)
    shutil.copyfileobj(src, dst, _DUMP_CHUNK)


def _drain_stderr(pipe, box: list) -> None:
    """Вычитывать stderr pg_dump в фоне: иначе при толстом stderr он встанет на полной трубе, пока мы
    ждём его stdout. Храним только начало, остальное выбрасываем."""
    kept = pipe.read(_DUMP_STDERR_KEEP)
    box.append(kept)
    while pipe.read(_DUMP_CHUNK):
        pass


def pg_dump_target(database_url: str) -> tuple[str, dict]:
    """Адрес базы для pg_dump без пароля и окружение процесса с паролем (PGPASSWORD).

    Командную строку процесса видит любой, кто смотрит список процессов (`ps`, `docker top`), поэтому
    пароль БД в неё не кладём: в argv уходит адрес без пароля, сам пароль — через окружение pg_dump
    (его читает только владелец процесса). Нет пароля в адресе — адрес как есть.
    """
    parts = urlsplit(database_url)
    env = dict(os.environ)
    if parts.password is None:
        return database_url, env
    userinfo, _, hostport = parts.netloc.rpartition("@")     # так же делит адрес и сам urlsplit
    user = userinfo.partition(":")[0]
    env["PGPASSWORD"] = unquote(parts.password)
    return urlunsplit(parts._replace(netloc=f"{user}@{hostport}")), env


def pg_dump_gz(database_url: str, dst_path: str) -> None:
    """Снять дамп базы database_url в dst_path: `pg_dump --lock-wait-timeout=60s <адрес без пароля>`
    (пароль — в PGPASSWORD, см. pg_dump_target), сжатый gzip (уровень 6, как у утилиты gzip), без строки
    `SET transaction_timeout = 0;` в шапке (см. выше). Любая беда — RuntimeError с текстом stderr
    (до 300 символов, пароль БД вычищен); недописанный файл удаляется.
    """
    ok = False
    try:
        url, env = pg_dump_target(database_url)
        try:
            dump = subprocess.Popen(["pg_dump", f"--lock-wait-timeout={PG_DUMP_LOCK_WAIT}", url],
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        except FileNotFoundError:
            raise RuntimeError("pg_dump не найден на сервере")
        err_box: list = []
        drain = threading.Thread(target=_drain_stderr, args=(dump.stderr, err_box), daemon=True)
        drain.start()
        write_error = None
        try:
            with gzip.open(dst_path, "wb", compresslevel=6) as out:
                _copy_dump_stream(dump.stdout, out)
        except BaseException as e:
            dump.kill()                 # поток не дочитан: pg_dump завис бы на полной трубе
            if not isinstance(e, OSError):
                raise
            write_error = e             # нет места, нет папки: отчитаемся тем же RuntimeError
        finally:
            dump.stdout.close()
            dump.wait()
            drain.join()
            dump.stderr.close()
        if write_error is not None:
            reason = write_error.strerror or type(write_error).__name__
            raise RuntimeError(f"Ошибка pg_dump: не удалось записать дамп ({reason})")
        if dump.returncode != 0:
            msg = (err_box[0] if err_box else b"").decode(errors="ignore")
            # пароль БД pg_dump знает (из окружения); если он его где-то процитировал — прячем
            raw_pw = urlsplit(database_url).password
            for secret in ({raw_pw, unquote(raw_pw)} if raw_pw else ()):
                if secret:
                    msg = msg.replace(secret, "***")
            raise RuntimeError("Ошибка pg_dump: " + msg.strip()[:300])
        ok = True
    finally:
        if not ok:
            _remove_quiet(dst_path)
