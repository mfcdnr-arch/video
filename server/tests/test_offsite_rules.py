"""Правила «Копии на флешку» (offsite_rules): пароль на копию, напоминание, состав и имена файлов.

Чистая логика без БД и без os.environ: проверяем только значения, которые
панель и инструкция на флешке показывают человеку (тексты, имена файлов),
и границы правил (пароль, «сегодня», что не берём в копию программы).
"""
from datetime import datetime

import pytest

from offsite_rules import (
    COPY_PASSWORD_MIN,
    copy_filename,
    copy_password_problem,
    is_program_excluded,
    media_filename,
    reminder_state,
    safe_host,
)

TOO_SHORT = "Пароль на копию — не короче 10 символов"
NOT_ASCII = ("Только латинские буквы, цифры и знаки: "
             "такой пароль можно набрать в консоли любой машины")


# --- пароль на копию -----------------------------------------------------------

def test_password_min_constant():
    assert COPY_PASSWORD_MIN == 10


def test_password_too_short():
    assert copy_password_problem("Abc-12345") == TOO_SHORT


def test_password_empty_is_too_short():
    assert copy_password_problem("") == TOO_SHORT


def test_password_exactly_min_is_ok():
    assert len("Abcdef-123") == COPY_PASSWORD_MIN
    assert copy_password_problem("Abcdef-123") is None


def test_password_cyrillic_refused():
    assert copy_password_problem("пароль-на-копию").startswith("Только латинские")
    assert copy_password_problem("пароль-на-копию") == NOT_ASCII


def test_password_space_refused():
    # пробел и табуляция — не печатные ASCII-знаки: в консоли их легко не заметить
    assert copy_password_problem("Copy pass 2026!") == NOT_ASCII
    assert copy_password_problem("Copy-pass\t2026") == NOT_ASCII


def test_password_control_and_non_ascii_symbols_refused():
    assert copy_password_problem("Copy-pass-2026\n") == NOT_ASCII
    assert copy_password_problem("Copy-pass-2026é") == NOT_ASCII


def test_password_ok():
    assert copy_password_problem("Copy-pass-2026!") is None


# --- напоминание «сделайте копию» -----------------------------------------------

def test_reminder_ok_today_no_media():
    assert reminder_state(datetime(2026, 11, 3, 9, 15), 0, datetime(2026, 11, 3, 18, 0)) == "ok"


def test_reminder_warn_yesterday():
    # 23:59 вчера и 00:01 сегодня — разные календарные дни, хоть прошло две минуты
    assert reminder_state(datetime(2026, 11, 2, 23, 59), 0, datetime(2026, 11, 3, 0, 1)) == "warn"


def test_reminder_warn_new_media():
    assert reminder_state(datetime(2026, 11, 3, 9, 15), 3, datetime(2026, 11, 3, 18, 0)) == "warn"


def test_reminder_warn_never():
    assert reminder_state(None, 0, datetime(2026, 11, 3, 9, 0)) == "warn"


# --- что не кладём в копию программы -----------------------------------------------

@pytest.mark.parametrize("p,ex", [
    (".env", False),                          # секреты нужны для восстановления
    ("server/main.py", False),
    (".git", True),
    (".git/config", True),
    (".claude/settings.json", True),
    ("server/__pycache__/x.pyc", True),
    ("install_server.log", True),
    ("ui_tests/node_modules/a.js", True),
    ("ds_server_V2_x.tar.gz", True),          # архив в корне — старые выпуски
    ("docs/a.zip", False),                    # а в подпапке это обычный файл проекта
    (".dev_admin_pass", True),
    ("backups/b.sql.gz", True),
    (".codex/x", True),
    (".superpowers/sdd/plan.md", True),
    ("docs/.DS_Store", True),
    ("a.tar", True),
    ("a.tgz", True),
    ("a.zip", True),
    ("server/static/app.log", True),
    ("server/routers/backups.py", False),     # «backups» — только целый компонент пути
    ("server/catalog.py", False),
])
def test_program_excluded(p, ex):
    assert is_program_excluded(p) is ex


# --- имена файлов на флешке -----------------------------------------------------------

def test_copy_filename():
    assert copy_filename(datetime(2026, 11, 3, 9, 15)) == "ds_copy_2026-11-03_0915.tar"


def test_copy_filename_pads_zeros():
    assert copy_filename(datetime(2026, 1, 2, 3, 4)) == "ds_copy_2026-01-02_0304.tar"


def test_media_filename_new_all():
    now = datetime(2026, 11, 3, 9, 15)
    # «новые» — id от следующего за уже скопированным до последнего на момент запуска
    assert media_filename("new", now, 142, 160) == "ds_media_new_2026-11-03_0915_id143-160.tar"
    assert media_filename("all", now, 142, 160) == "ds_media_all_2026-11-03_0915.tar"


def test_media_filename_unknown_scope_raises():
    with pytest.raises(ValueError):
        media_filename("some", datetime(2026, 11, 3, 9, 15), 0, 1)


# --- адрес сервера в инструкции --------------------------------------------------------

def test_safe_host():
    assert safe_host("10.0.119.100:80") == "10.0.119.100"
    assert safe_host("<script>") == "script"
    assert safe_host(None) == "адрес сервера"


def test_safe_host_empty_and_garbage():
    assert safe_host("") == "адрес сервера"
    assert safe_host("<>/\\") == "адрес сервера"
    assert safe_host("[::1]:8000") == "адрес сервера"


def test_safe_host_keeps_name_with_dash_and_dots():
    assert safe_host("ds-server.example.ru") == "ds-server.example.ru"
    assert safe_host("ds-server.example.ru:8080") == "ds-server.example.ru"
