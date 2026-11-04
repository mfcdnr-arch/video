"""Сборка ежедневной копии на флешку (offsite_store.build_copy).

Настоящих Postgres и pg_dump здесь нет: БД — мок с контрольными числами и строками «Документов»,
pg_dump подменён (пишет gzip с «-- dump»). Всё остальное настоящее: временная папка проекта,
файловые папки, шифрование и — главное — расшифровка настоящим `openssl` командой из инструкции.
Проверяется то, что легко сломать незаметно: во внешнем tar лежат ровно три открытых файла, внутри
запертой части есть всё нужное для восстановления (в том числе .env байт в байт, права 0600), в
программу не попадает мусор и симлинки, в инструкции нет секретов, а при любой ошибке сборки на диске
не остаётся ни одного файла — ни открытого, ни запертого.
"""
import gzip
import hashlib
import os
import re
import shutil
import stat
import subprocess
import tarfile
import time
from datetime import datetime
from types import SimpleNamespace as Row
from types import SimpleNamespace as Paths

import pytest

import offsite_store as store
from offsite_crypto import OPENSSL_DECRYPT_CMD
from offsite_rules import copy_filename

NOW = datetime(2026, 11, 4, 9, 30, 15)
PASSWORD = "Copy-pass-2026!"
HOST = "10.0.119.100"
ENV_BYTES = (b"POSTGRES_DB=display_system\nPOSTGRES_USER=display_user\n"
             b"DB_PASSWORD=db-secret-0123456789abcdef\nMINIO_USER=ds_minio\n"
             b"MINIO_PASSWORD=minio-secret-0123456789abcdef\n"
             b"SECRET_KEY=jwt-secret-0123456789abcdef0123456789abcdef\n")
COUNTS = {"screens": 12, "media": 340, "media_docs": 3, "playlists": 8,
          "users": 9, "advertisers": 4, "advertiser_docs": 6}

INSTRUCTION = "ВОССТАНОВЛЕНИЕ.txt"
OUTER_NAMES = [INSTRUCTION, "copy.tar.gz.enc", "copy.tar.gz.enc.sha256"]


class BuildDB:
    """Сессия-мок: контрольные числа одним запросом и строки «Документов» медиатеки."""

    def __init__(self, docs=(), counts=None):
        self.docs = list(docs)
        self.counts = counts or COUNTS
        self.calls = []

    def execute(self, query, params=None):
        q = " ".join(str(query).split())
        self.calls.append(q)
        if "count(*)" in q:
            return _Res([Row(**self.counts)])
        if "FROM media" in q and "status = 'document'" in q:
            return _Res([Row(**d) for d in self.docs])
        return _Res([])


class _Res:
    def __init__(self, rows):
        self._rows = rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows


def _doc(id, filename, title=None, filesize=10):
    return {"id": id, "title": title or f"Документ {id}", "filename": filename, "filesize": filesize}


def _write(path, data=b"x", mode=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    if mode is not None:
        path.chmod(mode)
    return path


def _fake_pg_dump(dst_path):
    with gzip.open(dst_path, "wb") as f:
        f.write(b"-- dump")


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Игрушечный проект + файловые папки + подменённый pg_dump. Возвращает пути."""
    project = tmp_path / "project"
    _write(project / ".env", ENV_BYTES, 0o644)      # в источнике права шире — в копии должно стать 0600
    _write(project / "docker-compose.yml", b"services: {}\n")
    _write(project / "install_server.sh", b"#!/bin/bash\necho install\n", 0o755)
    _write(project / "server" / "main.py", b"print('main')\n")
    _write(project / "server" / "migrations" / "001_init_schema.sql", b"-- 001\n")
    _write(project / "server" / "migrations" / "034_ticker_redesign.sql", b"-- 034\n")
    _write(project / "server" / "migrations" / "035_offsite_copy.sql", b"-- 035\n")
    _write(project / "README.md", "Проект — привет\n".encode("utf-8"))
    # то, чего в копии быть не должно
    _write(project / ".git" / "config", b"[core]\n")
    _write(project / ".claude" / "settings.json", b"{}")
    _write(project / ".superpowers" / "note.md", b"x")
    _write(project / "server" / "__pycache__" / "main.cpython-311.pyc", b"\0")
    _write(project / "backups" / "old.sql.gz", b"old")
    _write(project / "install_server.log", b"log")
    _write(project / "ds_server_V2_x.tar.gz", b"archive")
    _write(project / ".dev_admin_pass", b"dev-pass")
    _write(project / ".DS_Store", b"mac")
    # симлинки: на файл внутри проекта и на папку снаружи — ни тот ни другой в копию идти не должен
    outside = tmp_path / "outside"
    _write(outside / "secret.txt", b"outside-secret")
    os.symlink(project / "server" / "main.py", project / "link_to_main.py")
    os.symlink(outside, project / "link_to_outside")

    backup = tmp_path / "backups_vol"
    _write(backup / "documents" / "advertiser_doc_1.pdf", b"%PDF-advertiser")
    _write(backup / "backup_20261103_033000.sql.gz", b"old nightly dump, not part of the copy")
    media = tmp_path / "media_vol"
    _write(media / "contract.pdf", b"%PDF-contract")
    _write(media / "evil.pdf", b"%PDF-evil")
    _write(media / "clip.mp4", b"video clip, not part of the daily copy")
    agent = tmp_path / "agent_vol"
    _write(agent / "ds_agent.py", b"# agent v1\n")
    out = tmp_path / "offsite_tmp"

    monkeypatch.setattr(store, "PROJECT_DIR", str(project))
    monkeypatch.setattr(store, "BACKUP_DIR", str(backup))
    monkeypatch.setattr(store, "MEDIA_PATH", str(media))
    monkeypatch.setattr(store, "AGENT_UPDATE_PATH", str(agent))
    monkeypatch.setattr(store, "OFFSITE_TMP", str(out))
    monkeypatch.setattr(store, "_pg_dump_gz", _fake_pg_dump)
    monkeypatch.setenv("POSTGRES_USER", "display_user")
    monkeypatch.setenv("POSTGRES_DB", "display_system")
    return Paths(project=project, backup=backup, media=media, agent=agent, out=out, tmp=tmp_path)


def default_docs():
    return [
        _doc(1, "contract.pdf", "Договор"),
        _doc(2, "act.pdf", "Акт"),                      # файла нет на диске
        _doc(3, "../outside/evil.pdf", "Хитрый путь"),  # в копию — только evil.pdf
    ]


def build(env, docs=None, who="admin", host=HOST, password=PASSWORD, now=NOW):
    db = BuildDB(default_docs() if docs is None else docs)
    return store.build_copy(db, who=who, host=host, password=password, now=now)


def outer_members(built):
    with tarfile.open(built.path) as tf:
        return {m.name: tf.extractfile(m).read() for m in tf.getmembers()}


def decrypt(tmp_path, enc_bytes, password=PASSWORD):
    """Команда из инструкции (OPENSSL_DECRYPT_CMD), но пароль через env — как в test_offsite_crypto."""
    if not shutil.which("openssl"):
        pytest.skip("в окружении нет утилиты openssl — проверять расшифровку нечем")
    enc = tmp_path / "copy.tar.gz.enc"
    out = tmp_path / "copy.tar.gz"
    enc.write_bytes(enc_bytes)
    names = {"copy.tar.gz.enc": str(enc), "copy.tar.gz": str(out)}
    argv = [names.get(w, w) for w in OPENSSL_DECRYPT_CMD.split()]
    proc = subprocess.run(argv + ["-pass", "env:PW"], env={**os.environ, "PW": password},
                          capture_output=True)
    assert proc.returncode == 0, proc.stderr
    return out


def inner(env, built):
    """Расшифрованная запертая часть: tarfile (открыт, закрыть должен вызывающий) и его члены."""
    enc = outer_members(built)["copy.tar.gz.enc"]
    path = decrypt(env.tmp, enc)
    return tarfile.open(path, "r:gz")


# --- внешний tar ------------------------------------------------------------------------

def test_outer_tar_has_three_open_files(env):
    built = build(env)
    assert isinstance(built, store.BuiltCopy)
    assert built.filename == copy_filename(NOW) == "ds_copy_2026-11-04_0930.tar"
    assert os.path.basename(built.path) == built.filename
    assert os.path.dirname(built.path) == built.workdir
    assert built.size == os.path.getsize(built.path)
    # папка сборки лежит в OFFSITE_TMP, а в ней — только внешний tar (промежуточные файлы убраны)
    assert os.path.dirname(built.workdir) == str(env.out)
    assert os.listdir(built.workdir) == [built.filename]
    with tarfile.open(built.path) as tf:
        names = tf.getnames()
    assert names == OUTER_NAMES


def test_sha256_file_matches(env):
    built = build(env)
    m = outer_members(built)
    enc = m["copy.tar.gz.enc"]
    line = m["copy.tar.gz.enc.sha256"].decode("ascii")
    # ровно как у sha256sum: «<хеш>  <имя>\n» (два пробела), чтобы `sha256sum -c` проглотил
    assert line == f"{hashlib.sha256(enc).hexdigest()}  copy.tar.gz.enc\n"
    assert enc.startswith(b"Salted__")


def test_instruction_has_no_secrets(env):
    built = build(env)
    members = outer_members(built)
    text = members[INSTRUCTION].decode("utf-8")
    for secret in (PASSWORD, "db-secret-0123456789abcdef", "minio-secret-0123456789abcdef",
                   "jwt-secret-0123456789abcdef0123456789abcdef"):
        assert secret not in text
    # пароли нет и нигде во внешнем tar, кроме запертой части (там его и быть не должно: оно зашифровано)
    with open(built.path, "rb") as f:
        raw = f.read()
    for secret in (PASSWORD.encode(), b"db-secret-0123456789abcdef", b"jwt-secret-"):
        assert secret not in raw
    assert HOST in text
    assert OPENSSL_DECRYPT_CMD in text
    assert built.filename in text
    assert "035" in text
    assert "{{" not in text and "}}" not in text, "остались неподставленные поля шаблона"


def test_instruction_covers_restore_steps(env, monkeypatch):
    """Человек, который системы не знает, найдёт в инструкции каждый шаг и готовые команды."""
    monkeypatch.setenv("POSTGRES_USER", "usr_x")
    monkeypatch.setenv("POSTGRES_DB", "db_y")
    text = outer_members(build(env))[INSTRUCTION].decode("utf-8")
    must = [
        "exFAT", "NTFS", "FAT32",                                   # формат флешки
        "ds_media_", "ВСЕ",                                         # все архивы роликов
        "sha256sum -c copy.tar.gz.enc.sha256",
        "bash install_server.sh",
        "docker compose stop api celery",
        'DROP DATABASE db_y', 'CREATE DATABASE db_y OWNER usr_x',   # значения из окружения
        "docker exec ds_postgres psql -U usr_x -d postgres",
        "docker exec -i ds_postgres psql -U usr_x -d db_y -q",
        "docker compose start api celery",
        "docker cp", "ds_api:/data/media/", "ds_api:/data/backups/documents/",
        "ds_api:/data/agent_updates/",
        "docker exec ds_api curl -s http://localhost:8000/health",
        "MANIFEST.txt",
        store._COUNTS_SQL,                                          # те же числа, что в манифесте
        "адрес",                                                  # тот же сетевой адрес
        "без файлов",                                               # старые бэкапы/диагностика
    ]
    for needle in must:
        assert needle in text, needle
    # пронумерованные шаги, читаемые человеком
    assert len(re.findall(r"^ШАГ \d", text, flags=re.M)) >= 5
    assert not any(ord(c) > 0xFFFF for c in text)
    assert "\r" not in text


def test_instruction_defaults_for_db_names(env, monkeypatch):
    monkeypatch.delenv("POSTGRES_USER", raising=False)
    monkeypatch.delenv("POSTGRES_DB", raising=False)
    text = outer_members(build(env))[INSTRUCTION].decode("utf-8")
    assert "display_user" in text and "display_system" in text


def test_instruction_host_is_sanitized(env):
    """Host приходит от клиента: в инструкцию попадает только безопасный адрес (safe_host)."""
    text = outer_members(build(env, host="evil.example\"; rm -rf /;:8080"))[INSTRUCTION].decode("utf-8")
    assert "rm -rf /" not in text and '"; rm' not in text      # (в самой инструкции есть «rm -rf ~/ds_restore»)
    assert "evil.example" in text


# --- запертая часть ---------------------------------------------------------------------

def test_inner_after_openssl(env):
    built = build(env)
    with inner(env, built) as tf:
        names = tf.getnames()
        # .env байт в байт и с правами 0600 (в источнике было 0644)
        m = tf.getmember("program/.env")
        assert tf.extractfile(m).read() == ENV_BYTES
        assert stat.S_IMODE(m.mode) == 0o600
        assert tf.extractfile("program/server/main.py").read() == b"print('main')\n"
        assert tf.extractfile("program/README.md").read() == "Проект — привет\n".encode("utf-8")
        # скрипт остаётся исполняемым
        assert stat.S_IMODE(tf.getmember("program/install_server.sh").mode) & 0o111
        # свежий дамп с именем по времени сборки
        dump = "db/backup_20261104_093015.sql.gz"
        assert gzip.decompress(tf.extractfile(dump).read()) == b"-- dump"
        assert tf.extractfile("documents/advertiser_doc_1.pdf").read() == b"%PDF-advertiser"
        assert tf.extractfile("media_documents/contract.pdf").read() == b"%PDF-contract"
        assert tf.extractfile("agent_updates/ds_agent.py").read() == b"# agent v1\n"
        assert "MANIFEST.txt" in names
        # ночной дамп с сервера и обычные ролики в копию не входят
        assert not any("backup_20261103" in n for n in names)
        assert not any(n.endswith("clip.mp4") for n in names)
        # каждый раздел есть как папка, даже пустой: `docker cp <папка>/.` не упадёт
        for d in ("program", "db", "documents", "media_documents", "agent_updates"):
            assert tf.getmember(d).isdir(), d
        # владельцев хоста в копию не тащим
        assert all(m.uid == 0 and m.gid == 0 for m in tf.getmembers())


def test_program_exclusions_applied(env):
    with inner(env, build(env)) as tf:
        names = tf.getnames()
    bad = [n for n in names if n.startswith("program/") and re.search(
        r"(^|/)(\.git|\.claude|\.superpowers|backups|__pycache__)(/|$)|install_server\.log$|"
        r"ds_server_V2_x\.tar\.gz$|\.dev_admin_pass$|\.DS_Store$", n)]
    assert bad == []
    assert "program/.env" in names


def test_symlinks_are_not_followed(env):
    with inner(env, build(env)) as tf:
        names = tf.getnames()
        assert not any("link_to" in n for n in names)
        assert not any("secret.txt" in n for n in names)
        assert all(not m.issym() and not m.islnk() for m in tf.getmembers())


def test_media_documents_use_basename_only(env):
    """Имя из БД не должно вывести за пределы раздела: только basename, без «..» и ведущего «/»."""
    docs = [_doc(1, "contract.pdf"), _doc(2, "../outside/evil.pdf"), _doc(3, "/etc/passwd"),
            _doc(4, "sub\\dir\\contract.pdf"), _doc(5, ".."), _doc(6, "")]
    with inner(env, build(env, docs)) as tf:
        names = tf.getnames()
    md = sorted(n for n in names if n.startswith("media_documents/") and n != "media_documents/")
    assert md == ["media_documents/contract.pdf", "media_documents/evil.pdf"]
    assert all(".." not in n.split("/") and not n.startswith("/") for n in names)


def test_media_documents_missing_listed_in_manifest(env):
    with inner(env, build(env)) as tf:
        manifest = tf.extractfile("MANIFEST.txt").read().decode("utf-8")
        names = tf.getnames()
    assert "media_documents/act.pdf" not in names
    missing = [ln for ln in manifest.splitlines() if "нет файла" in ln]
    assert len(missing) == 1
    assert "act.pdf" in missing[0] and "Акт" in missing[0]


# --- манифест ---------------------------------------------------------------------------

def _file_lines(manifest):
    """Строки списка файлов: <sha256>  <размер>  <путь>."""
    rx = re.compile(r"^([0-9a-f]{64})  (\d+)  (.+)$")
    return {m.group(3): (m.group(1), int(m.group(2)))
            for m in map(rx.match, manifest.splitlines()) if m}


def test_manifest_counts_and_hashes(env):
    built = build(env, who="admin")
    with inner(env, built) as tf:
        manifest = tf.extractfile("MANIFEST.txt").read().decode("utf-8")
        files = _file_lines(manifest)
        # когда, кем, с какого адреса, миграция
        assert "2026-11-04 09:30" in manifest
        assert "admin" in manifest
        assert HOST in manifest
        assert re.search(r"миграция.*\b035\b", manifest, flags=re.I)
        # контрольные числа — каждое со своей подписью
        for key, label in (("screens", "screens"), ("media", "media"), ("media_docs", "media_docs"),
                           ("playlists", "playlists"), ("users", "users"), ("advertisers", "advertisers"),
                           ("advertiser_docs", "advertiser_docs")):
            assert re.search(rf"^\s*{label}\s*[:=]\s*{COUNTS[key]}\b", manifest, flags=re.M), label
        # sha256 .env в манифесте верный
        assert files["program/.env"] == (hashlib.sha256(ENV_BYTES).hexdigest(), len(ENV_BYTES))
        # ... и список полный: каждый файл копии (кроме самого манифеста) есть в нём с верным хешем
        members = [m for m in tf.getmembers() if m.isfile() and m.name != "MANIFEST.txt"]
        assert sorted(files) == sorted(m.name for m in members)
        for m in members:
            data = tf.extractfile(m).read()
            assert files[m.name] == (hashlib.sha256(data).hexdigest(), len(data)), m.name
        # сам манифест в своём списке не значится (его хеш внутри него не посчитать)
        assert "MANIFEST.txt" not in files


def test_manifest_migration_is_the_highest_number(env):
    # 035 лежит в проекте как самая большая; появится 036 — манифест назовёт её
    _write(env.project / "server" / "migrations" / "036_next.sql", b"-- 036\n")
    with inner(env, build(env)) as tf:
        manifest = tf.extractfile("MANIFEST.txt").read().decode("utf-8")
    assert re.search(r"миграция.*\b036\b", manifest, flags=re.I)


def test_manifest_has_no_secrets(env):
    with inner(env, build(env)) as tf:
        manifest = tf.extractfile("MANIFEST.txt").read().decode("utf-8")
    for secret in (PASSWORD, "db-secret", "minio-secret", "jwt-secret"):
        assert secret not in manifest


# --- ошибки и чистка --------------------------------------------------------------------

def _tmp_is_empty(env):
    return (not env.out.exists()) or os.listdir(env.out) == []


def test_not_mounted_raises(env):
    (env.project / ".env").unlink()
    with pytest.raises(store.ProjectNotMounted):
        build(env)
    assert _tmp_is_empty(env)


def test_not_mounted_without_compose_raises(env):
    (env.project / "docker-compose.yml").unlink()
    with pytest.raises(store.ProjectNotMounted):
        build(env)
    assert _tmp_is_empty(env)


def test_project_dir_missing_raises(env, monkeypatch):
    monkeypatch.setattr(store, "PROJECT_DIR", str(env.tmp / "нет_такой_папки"))
    with pytest.raises(store.ProjectNotMounted):
        build(env)


def test_pg_dump_failure_cleans_up(env, monkeypatch):
    def boom(dst_path):
        with open(dst_path, "wb") as f:     # дамп успел начаться — недописанный файл тоже должен уйти
            f.write(b"\x1f\x8b")
        raise RuntimeError("Ошибка pg_dump: connection refused")
    monkeypatch.setattr(store, "_pg_dump_gz", boom)
    with pytest.raises(RuntimeError, match="pg_dump"):
        build(env)
    assert _tmp_is_empty(env)


@pytest.mark.parametrize("target,exc", [
    ("encrypt_file_openssl", OSError("No space left on device")),
    ("write_tar", OSError("No space left on device")),
    ("document_rows", RuntimeError("БД недоступна")),
])
def test_any_failure_cleans_up(env, monkeypatch, target, exc):
    """Сбой на любом этапе (шифрование, запись внешнего tar, чтение БД) не оставляет ничего на диске."""
    def boom(*a, **kw):
        raise exc
    monkeypatch.setattr(store, target, boom)
    with pytest.raises(type(exc)):
        build(env)
    assert _tmp_is_empty(env)


def test_file_vanishing_midway_cleans_up(env, monkeypatch):
    """Файл пропал между обходом папки и чтением: ошибка наружу, на диске чисто."""
    real_open = open

    def flaky(path, *a, **kw):
        if str(path).endswith("server/main.py"):
            raise FileNotFoundError(path)
        return real_open(path, *a, **kw)
    # подменяем open только в модуле сборки: у pytest и остальных модулей свой, настоящий
    monkeypatch.setattr(store, "open", flaky, raising=False)
    with pytest.raises(FileNotFoundError):
        build(env)
    assert _tmp_is_empty(env)


def test_password_never_in_exception_text(env, monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("шифрование не удалось")
    monkeypatch.setattr(store, "encrypt_file_openssl", boom)
    with pytest.raises(RuntimeError) as ei:
        build(env)
    assert PASSWORD not in str(ei.value)


def test_empty_password_rejected_before_anything_is_written(env):
    with pytest.raises(ValueError):
        build(env, password="")
    assert _tmp_is_empty(env)


def test_workdir_is_private(env):
    """В папке сборки на миг лежит расшифрованный архив с .env — закрываем её от других пользователей."""
    built = build(env)
    assert stat.S_IMODE(os.stat(built.workdir).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(env.out).st_mode) == 0o700


def test_two_builds_do_not_collide(env):
    a, b = build(env), build(env)
    assert a.workdir != b.workdir
    assert os.path.exists(a.path) and os.path.exists(b.path)


# --- чистка забытых папок ----------------------------------------------------------------

def test_cleanup_stale(env):
    old = env.out / "old"
    fresh = env.out / "fresh"
    for d in (old, fresh):
        _write(d / "ds_copy.tar", b"x")
    t = time.time() - 20 * 60
    os.utime(old, (t, t))
    assert store.cleanup_stale() == 1
    assert not old.exists()
    assert fresh.exists()


def test_cleanup_stale_custom_age_and_missing_dir(env):
    assert store.cleanup_stale() == 0       # OFFSITE_TMP ещё не создан — не ошибка
    d = env.out / "x"
    _write(d / "f", b"x")
    t = time.time() - 120
    os.utime(d, (t, t))
    assert store.cleanup_stale(older_than_s=60) == 1
    assert not d.exists()


def test_cleanup_stale_ignores_files_and_symlinks(env):
    """Чистим только папки сборки; чужой файл и симлинк на внешнюю папку не трогаем."""
    env.out.mkdir(parents=True)
    victim = env.tmp / "victim"
    _write(victim / "keep.txt", b"keep")
    os.symlink(victim, env.out / "link")
    stray = _write(env.out / "stray.txt", b"s")
    t = time.time() - 3600
    os.utime(stray, (t, t))
    os.utime(env.out / "link", (t, t), follow_symlinks=False)
    assert store.cleanup_stale() == 0
    assert (victim / "keep.txt").exists()
    assert stray.exists()


# --- чистка не трогает идущую сборку ------------------------------------------------------

def test_build_leaves_no_marker_after_success(env):
    """Маркер «сборка идёт» нужен только пока копия собирается; в готовой папке остаётся один внешний tar."""
    built = build(env)
    assert store.BUILD_MARKER not in os.listdir(built.workdir)


def test_cleanup_stale_skips_running_build(env, monkeypatch):
    """Дамп большой базы может идти дольше 15 минут. Пока сборка не закончилась (маркер .building),
    чистка её папку не трогает, даже если сама папка выглядит старой — иначе копия развалилась бы."""
    seen = {}

    def slow_dump(dst_path):
        _fake_pg_dump(dst_path)
        (workdir,) = [p for p in env.out.iterdir() if p.is_dir()]
        t = time.time() - 20 * 60
        os.utime(workdir, (t, t))                    # «прошло 20 минут»
        seen["marker"] = (workdir / store.BUILD_MARKER).exists()
        seen["removed"] = store.cleanup_stale()
        seen["alive"] = workdir.is_dir()
    monkeypatch.setattr(store, "_pg_dump_gz", slow_dump)
    built = build(env)
    assert seen == {"marker": True, "removed": 0, "alive": True}
    assert os.path.exists(built.path)               # сборка дошла до конца


def test_cleanup_stale_removes_crashed_build_after_six_hours(env):
    """Процесс убили посреди сборки: маркер остался. Через 6 часов это уже не «идёт», а мусор."""
    crashed = env.out / "crashed"
    running = env.out / "running"
    for d in (crashed, running):
        _write(d / store.BUILD_MARKER, b"")
        _write(d / "dump.sql.gz", b"x")
    for d, hours in ((crashed, 7), (running, 5)):
        t = time.time() - hours * 3600
        os.utime(d / store.BUILD_MARKER, (t, t))
        os.utime(d, (t, t))
    assert store.cleanup_stale() == 1
    assert not crashed.exists()
    assert running.exists()


def test_cleanup_stale_still_removes_old_dir_without_marker(env):
    """Готовую, но так и не скачанную копию (маркера уже нет) стираем через 15 минут, как раньше."""
    done = env.out / "done"
    _write(done / "ds_copy.tar", b"x")
    t = time.time() - 20 * 60
    os.utime(done, (t, t))
    assert store.cleanup_stale() == 1
    assert not done.exists()


def test_failed_build_removes_marker_with_workdir(env, monkeypatch):
    def boom(dst_path):
        raise RuntimeError("Ошибка pg_dump: нет связи")
    monkeypatch.setattr(store, "_pg_dump_gz", boom)
    with pytest.raises(RuntimeError):
        build(env)
    assert _tmp_is_empty(env)


# --- «проект подключён» для статуса ---------------------------------------------------------

def test_project_mounted_flag(env):
    assert store.project_mounted() is True
    (env.project / ".env").unlink()
    assert store.project_mounted() is False


# --- монтирование проекта в compose ---------------------------------------------------------

COMPOSE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                       "docker-compose.yml")


@pytest.mark.skipif(not os.path.exists(COMPOSE),
                    reason="docker-compose.yml не смонтирован в контейнер тестов — проверяется на хосте")
def test_compose_mounts_project_read_only_into_api_only():
    """Копия берёт программу и .env из /srv/project: монтирование есть у api и только на чтение."""
    with open(COMPOSE, encoding="utf-8") as f:
        text = f.read()
    api = text.split("\n  api:\n", 1)[1].split("\n  celery:\n", 1)[0]
    celery = text.split("\n  celery:\n", 1)[1].split("\n  nginx:\n", 1)[0]
    assert "      - ./:/srv/project:ro\n" in api
    assert "/srv/project" not in celery        # копию собирает только api: лишних мест с .env не плодим


# --- настоящий _pg_dump_gz (с поддельной утилитой pg_dump) --------------------------------

def _fake_bin(tmp_path, script):
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    p = bindir / "pg_dump"
    p.write_text(script)
    p.chmod(0o755)
    return str(bindir)


def test_pg_dump_gz_ok(tmp_path, monkeypatch):
    if not shutil.which("gzip"):
        pytest.skip("нет gzip")
    bindir = _fake_bin(tmp_path, "#!/bin/sh\necho '-- dump of' \"$1\"\n")
    monkeypatch.setenv("PATH", bindir + os.pathsep + os.environ["PATH"])
    monkeypatch.setattr(store, "DATABASE_URL", "postgresql://u:pw@h:5432/d")
    dst = tmp_path / "out.sql.gz"
    store._pg_dump_gz(str(dst))
    assert gzip.decompress(dst.read_bytes()) == b"-- dump of postgresql://u:pw@h:5432/d\n"


def test_pg_dump_gz_failure_text_is_short_and_has_no_db_password(tmp_path, monkeypatch):
    if not shutil.which("gzip"):
        pytest.skip("нет gzip")
    # 900 нулей после пароля: текст ошибки длиннее 300 символов, пароль стоит в начале
    bindir = _fake_bin(tmp_path, "#!/bin/sh\nprintf 'pg_dump: error s3cr3tpw %0900d\\n' 0 >&2\nexit 1\n")
    monkeypatch.setenv("PATH", bindir + os.pathsep + os.environ["PATH"])
    monkeypatch.setattr(store, "DATABASE_URL", "postgresql://u:s3cr3tpw@h:5432/d")
    with pytest.raises(RuntimeError) as ei:
        store._pg_dump_gz(str(tmp_path / "out.sql.gz"))
    msg = str(ei.value)
    assert "pg_dump" in msg
    assert "s3cr3tpw" not in msg
    assert len(msg) <= 300 + 60
    assert not (tmp_path / "out.sql.gz").exists()   # недописанный дамп убран


def test_pg_dump_gz_missing_binary(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path / "пусто"))
    with pytest.raises(RuntimeError, match="не найден"):
        store._pg_dump_gz(str(tmp_path / "out.sql.gz"))
