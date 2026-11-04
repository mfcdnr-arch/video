"""Шифрование копии на флешку и хранение пароля на копию (offsite_crypto).

Копию должен суметь расшифровать человек на любом компьютере обычным `openssl`,
поэтому главная проверка — настоящий openssl из образа расшифровывает наш файл
командой из инструкции (OPENSSL_DECRYPT_CMD). Остальное — формат файла и
хранение пароля на копию (Fernet): без БД и без сети.
"""
import os
import shutil
import subprocess

import pytest

from offsite_crypto import (
    OPENSSL_DECRYPT_CMD,
    OPENSSL_ITER,
    encrypt_file_openssl,
    open_secret,
    seal_secret,
)

PASSWORD = "Copy-pass-2026!"
SECRET_KEY = "test-secret-key-for-offsite"

# 3 МиБ + хвост, не кратный 16: проверяем и несколько кусков по 1 МиБ, и добивку PKCS7
BIG = 3 * 1024 * 1024 + 7


def _need_openssl():
    if not shutil.which("openssl"):
        pytest.skip("в окружении нет утилиты openssl — проверять расшифровку нечем")


def _write_random(path, n):
    data = os.urandom(n)
    with open(path, "wb") as f:
        f.write(data)
    return data


def _openssl_decrypt(tmp_path, enc_path, password):
    """Команда из инструкции (OPENSSL_DECRYPT_CMD), но с `-pass env:PW` — пароль не в argv."""
    out = tmp_path / "out.tar.gz"
    # подменяем целые слова команды, а не подстроки: «copy.tar.gz» входит в «copy.tar.gz.enc»
    names = {"copy.tar.gz.enc": str(enc_path), "copy.tar.gz": str(out)}
    argv = [names.get(word, word) for word in OPENSSL_DECRYPT_CMD.split()]
    proc = subprocess.run(argv + ["-pass", "env:PW"],
                          env={**os.environ, "PW": password},
                          capture_output=True)
    return proc.returncode, out


def test_decrypt_command_is_exact():
    assert OPENSSL_ITER == 200_000
    assert OPENSSL_DECRYPT_CMD == (
        "openssl enc -d -aes-256-cbc -pbkdf2 -md sha256 -iter 200000 "
        "-in copy.tar.gz.enc -out copy.tar.gz")


def test_openssl_decrypts_our_file(tmp_path):
    _need_openssl()
    src, enc = tmp_path / "copy.tar.gz", tmp_path / "copy.tar.gz.enc"
    original = _write_random(src, BIG)

    encrypt_file_openssl(str(src), str(enc), PASSWORD)

    code, out = _openssl_decrypt(tmp_path, enc, PASSWORD)
    assert code == 0
    assert out.read_bytes() == original


def test_wrong_password_does_not_decrypt(tmp_path):
    _need_openssl()
    src, enc = tmp_path / "copy.tar.gz", tmp_path / "copy.tar.gz.enc"
    original = _write_random(src, 100_000)

    encrypt_file_openssl(str(src), str(enc), PASSWORD)

    code, out = _openssl_decrypt(tmp_path, enc, "Wrong-pass-2026!")
    # с чужим паролем openssl либо падает на добивке, либо (1 шанс из 256) отдаёт мусор
    assert code != 0 or not out.exists() or out.read_bytes() != original


def test_header_and_size(tmp_path):
    src, enc = tmp_path / "copy.tar.gz", tmp_path / "copy.tar.gz.enc"
    for n in (0, 1, 15, 16, 17, BIG):
        _write_random(src, n)
        written = encrypt_file_openssl(str(src), str(enc), PASSWORD)
        data = enc.read_bytes()
        assert data.startswith(b"Salted__")
        # «Salted__» + 8 байт соли, затем шифротекст с добивкой PKCS7 (всегда +1..16 байт)
        assert len(data) == 16 + (n // 16 + 1) * 16
        assert written == len(data)


def test_salt_is_random_by_default(tmp_path):
    src = tmp_path / "copy.tar.gz"
    _write_random(src, 1000)
    a, b = tmp_path / "a.enc", tmp_path / "b.enc"
    encrypt_file_openssl(str(src), str(a), PASSWORD)
    encrypt_file_openssl(str(src), str(b), PASSWORD)
    assert a.read_bytes()[8:16] != b.read_bytes()[8:16]


def test_fixed_salt_is_deterministic(tmp_path):
    src = tmp_path / "copy.tar.gz"
    _write_random(src, BIG)
    a, b = tmp_path / "a.enc", tmp_path / "b.enc"
    salt = b"01234567"
    encrypt_file_openssl(str(src), str(a), PASSWORD, salt=salt)
    encrypt_file_openssl(str(src), str(b), PASSWORD, salt=salt)
    assert a.read_bytes() == b.read_bytes()
    assert a.read_bytes()[8:16] == salt


def test_bad_salt_length_is_rejected(tmp_path):
    src = tmp_path / "copy.tar.gz"
    _write_random(src, 10)
    with pytest.raises(ValueError):
        encrypt_file_openssl(str(src), str(tmp_path / "x.enc"), PASSWORD, salt=b"short")


def test_seal_open_roundtrip():
    token = seal_secret("Abc-123-xyz", SECRET_KEY)
    assert token != "Abc-123-xyz" and "Abc-123-xyz" not in token
    assert open_secret(token, SECRET_KEY) == "Abc-123-xyz"


def test_open_with_other_key_is_none():
    token = seal_secret("Abc-123-xyz", SECRET_KEY)
    assert open_secret(token, "another-secret-key") is None


def test_open_garbage_is_none():
    assert open_secret("not-a-token", SECRET_KEY) is None
    assert open_secret("", SECRET_KEY) is None
