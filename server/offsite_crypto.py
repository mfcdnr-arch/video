"""Шифрование «Копии на флешку» и хранение пароля на копию.

Копия — обычный tar.gz, зашифрованный так же, как делает `openssl enc`: файл
начинается с «Salted__» и 8 байт соли, дальше шифротекст AES-256-CBC. Ключ и IV
берутся из пароля через PBKDF2-HMAC-SHA256 (48 байт: 32 на ключ, 16 на IV).
Формат выбран затем, чтобы расшифровать копию мог любой человек на любом
компьютере одной командой openssl (OPENSSL_DECRYPT_CMD) — без нашего софта и без
доступа к серверу. Шифруем сами через cryptography, а не запускаем openssl:
не зависим от утилиты в образе, пароль не уходит в аргументы процесса, а
результат побайтно тот же, что выдал бы openssl.

Пароль на копию сервер хранит не открытым текстом, а в зашифрованном виде
(Fernet, ключ выводится из SECRET_KEY): так «Скачать сейчас» и ежедневная копия
работают без ввода пароля, но утечка таблицы настроек сам пароль не раскрывает.
Пароль не попадает ни в логи, ни в тексты исключений.

Чистая логика: без БД и без сети.
"""
import base64
import hashlib
import os
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes, padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

# Число итераций PBKDF2 — то же, что в команде расшифровки: расходятся значения —
# openssl не расшифрует. Держим одну константу и подставляем её в команду.
OPENSSL_ITER = 200_000

# Команда из инструкции для человека на флешке. Пароль она спросит сама (или его
# передают флагом -pass); имена файлов — как лежат на флешке.
OPENSSL_DECRYPT_CMD = (
    "openssl enc -d -aes-256-cbc -pbkdf2 -md sha256 -iter %d "
    "-in copy.tar.gz.enc -out copy.tar.gz" % OPENSSL_ITER)

_MAGIC = b"Salted__"
_SALT_LEN = 8
_CHUNK = 1024 * 1024        # читаем и пишем кусками по 1 МиБ: копия может весить гигабайты


def encrypt_file_openssl(src_path: str, dst_path: str, password: str, *,
                         salt: Optional[bytes] = None) -> int:
    """Зашифровать src_path в dst_path в формате `openssl enc -pbkdf2`; вернуть размер результата.

    salt нужен только тестам (одинаковая соль даёт одинаковый файл); в работе — случайная.
    """
    if salt is None:
        salt = os.urandom(_SALT_LEN)
    if len(salt) != _SALT_LEN:
        raise ValueError("соль должна быть ровно %d байт" % _SALT_LEN)

    # 48 байт: первые 32 — ключ AES-256, последние 16 — IV (так делает openssl)
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=48, salt=salt, iterations=OPENSSL_ITER)
    material = kdf.derive(password.encode("utf-8"))
    key, iv = material[:32], material[32:]

    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    padder = padding.PKCS7(128).padder()

    written = 0
    with open(src_path, "rb") as src, open(dst_path, "wb") as dst:
        header = _MAGIC + salt
        dst.write(header)
        written += len(header)
        while True:
            chunk = src.read(_CHUNK)
            if not chunk:
                break
            out = encryptor.update(padder.update(chunk))
            dst.write(out)
            written += len(out)
        # добивка PKCS7 даёт последний блок даже для файла, кратного 16 байтам
        tail = encryptor.update(padder.finalize()) + encryptor.finalize()
        dst.write(tail)
        written += len(tail)
    return written


def _fernet(secret_key: str) -> Fernet:
    # Ключ Fernet — 32 байта в urlsafe-base64. Выводим его из SECRET_KEY с префиксом
    # назначения, чтобы тот же SECRET_KEY, применённый для другого, не давал тот же ключ.
    digest = hashlib.sha256(b"ds-offsite-copy:" + secret_key.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def seal_secret(plain: str, secret_key: str) -> str:
    """Спрятать пароль на копию для хранения в настройках."""
    return _fernet(secret_key).encrypt(plain.encode("utf-8")).decode("ascii")


def open_secret(token: str, secret_key: str) -> Optional[str]:
    """Достать пароль на копию; None — если SECRET_KEY другой или в настройках мусор."""
    try:
        return _fernet(secret_key).decrypt(token.encode("ascii", "replace")).decode("utf-8")
    except InvalidToken:
        return None
