#!/usr/bin/env python3
"""
reset_admin_password.py — задать администратору admin СЛУЧАЙНЫЙ пароль и
включить обязательную смену при следующем входе. Печатает новый пароль в stdout.

Запускается ВНУТРИ контейнера ds_api (там есть SQLAlchemy, passlib и DATABASE_URL):
    docker compose exec -T api python /app/reset_admin_password.py

Использование:
  - install_server.sh вызывает его при ПЕРВОЙ установке, чтобы не оставлять
    предсказуемый пароль admin123;
  - также годится как аварийное восстановление доступа (сброс пароля admin).

Код возврата: 0 — успех (пароль напечатан), 1 — пользователь admin не найден.
"""
import os
import secrets
import sys

from sqlalchemy import create_engine, text
from passlib.context import CryptContext

# Читаемый случайный пароль (~16 символов, без похожих 0/O/1/l и спецсимволов,
# чтобы легко продиктовать/ввести один раз до обязательной смены).
_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789"
password = "".join(secrets.choice(_ALPHABET) for _ in range(16))

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
password_hash = pwd_context.hash(password)

db_url = os.environ.get("DATABASE_URL")
if not db_url:
    sys.stderr.write("DATABASE_URL не задан\n")
    sys.exit(1)

engine = create_engine(db_url)
with engine.begin() as conn:
    res = conn.execute(
        text("UPDATE users SET password_hash = :h, must_change_password = true "
             "WHERE username = 'admin'"),
        {"h": password_hash},
    )
    if res.rowcount == 0:
        sys.stderr.write("Пользователь admin не найден\n")
        sys.exit(1)

# ТОЛЬКО пароль в stdout — его перехватывает install_server.sh.
print(password)
