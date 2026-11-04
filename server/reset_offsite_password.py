#!/usr/bin/env python3
"""
reset_offsite_password.py — забыть пароль на копию («Копия на флешку»), чтобы задать новый в панели.

Пароль на копию нельзя сменить из панели, не зная старого (иначе любой, кто попал в панель под
суперадмином, подсунул бы свой пароль и унёс копию, которую сумеет открыть). Если старый забыт, его
сбрасывают здесь, на самом сервере: доступ к серверу и так означает полное доверие. Старые копии
по-прежнему открываются старым паролем — сброс их не трогает.

Запускается ВНУТРИ контейнера ds_api (там есть SQLAlchemy и DATABASE_URL), по образцу reset_admin_password.py:
    docker compose exec -T api python /app/reset_offsite_password.py

Сброс пишется в журнал действий (кто — «server»). Код возврата: 0 — готово, 1 — не получилось
(например, база недоступна или миграция 035 ещё не применена: тогда `bash migrate.sh`).
"""
import sys

import offsite_store as store
from deps import SessionLocal

MESSAGE = "Пароль на копию сброшен. Задайте новый в панели: Настройки → Копия на флешку."


def main() -> int:
    try:
        db = SessionLocal()
        try:
            store.clear_password(db)
            store.audit(db, "Пароль на копию сброшен на сервере",
                        "командой reset_offsite_password.py; новый пароль задаётся в панели", "server")
            db.commit()
        finally:
            db.close()
    except Exception as e:
        sys.stderr.write(f"Не удалось сбросить пароль на копию: {type(e).__name__}: {str(e)[:300]}\n")
        return 1
    print(MESSAGE)
    return 0


if __name__ == "__main__":
    sys.exit(main())
