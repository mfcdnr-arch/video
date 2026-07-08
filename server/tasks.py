"""
Celery-задачи. Воркер и beat запускаются одним процессом
(см. docker-compose: celery -A tasks.celery_app worker -B).

nightly_backup — ночной дамп БД + ротация. Та же механика, что ручная
кнопка в панели (routers/backups.py): pg_dump | gzip → BACKUP_DIR,
запись в таблицы backups и audit_log (actor=celery). Старые копии
удаляются, хранится последних BACKUP_KEEP штук.
"""
import logging
import os
import subprocess
from datetime import datetime, timezone

from celery import Celery
from celery.schedules import crontab

log = logging.getLogger(__name__)

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")

celery_app = Celery(
    "tasks",
    broker=REDIS_URL,
    backend=REDIS_URL,
)

# Расписание beat живёт по Москве — вся сеть в одном часовом поясе.
celery_app.conf.update(
    timezone="Europe/Moscow",
    enable_utc=True,
    beat_schedule={
        "nightly-backup": {
            "task": "nightly_backup",
            "schedule": crontab(hour=3, minute=30),   # 03:30 МСК ежедневно
        },
        "nightly-media-backup": {
            "task": "nightly_media_backup",
            "schedule": crontab(hour=2, minute=30),   # 02:30 МСК ежедневно
        },
        "monthly-playlog-archive": {
            "task": "archive_play_log",
            "schedule": crontab(day_of_month=1, hour=4, minute=30),  # 1-е число, 04:30 МСК
        },
        "check-notifications": {
            "task": "check_notifications",
            "schedule": 60.0,   # каждую минуту
        },
    },
)

BACKUP_KEEP = int(os.getenv("BACKUP_KEEP", "14"))  # сколько последних копий хранить


@celery_app.task(name="ping")
def ping():
    return "pong"


@celery_app.task(name="nightly_backup")
def nightly_backup():
    """Дамп БД + ротация старых копий. Возвращает имя файла или текст ошибки."""
    from sqlalchemy import create_engine, text

    # DATABASE_URL приходит из окружения контейнера (docker-compose),
    # deps.py не импортируем — воркер живёт без остального кода сервера.
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        log.error("[backup] DATABASE_URL не задан в окружении")
        return "error: DATABASE_URL is not set"

    backup_dir = os.getenv("BACKUP_DIR", "/data/backups")
    os.makedirs(backup_dir, exist_ok=True)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    filename = f"backup_{timestamp}.sql.gz"
    filepath = os.path.join(backup_dir, filename)

    with open(filepath, "wb") as f:
        dump_proc = subprocess.Popen(["pg_dump", database_url],
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        gzip_proc = subprocess.Popen(["gzip"], stdin=dump_proc.stdout, stdout=f)
        dump_proc.stdout.close()
        _, dump_err = dump_proc.communicate()
        gzip_proc.communicate()
    if dump_proc.returncode != 0:
        if os.path.exists(filepath):
            os.remove(filepath)
        err = dump_err.decode(errors="ignore")[:300]
        log.error(f"[backup] pg_dump упал: {err}")
        return f"error: {err}"

    size = os.path.getsize(filepath)
    engine = create_engine(database_url, pool_pre_ping=True)
    removed = 0
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO backups (filename, size_bytes, created_by)
            VALUES (:fn, :sz, 'celery')
        """), {"fn": filename, "sz": size})
        conn.execute(text("""
            INSERT INTO audit_log (event_type, title, detail, actor)
            VALUES ('backup', 'Автоматическая резервная копия', :detail, 'celery')
        """), {"detail": filename})

        # Ротация: удаляем всё, что старше последних BACKUP_KEEP копий
        old = conn.execute(text("""
            SELECT id, filename FROM backups
            ORDER BY created_at DESC OFFSET :keep
        """), {"keep": BACKUP_KEEP}).fetchall()
        for row in old:
            old_path = os.path.join(backup_dir, row.filename)
            if os.path.exists(old_path):
                try:
                    os.remove(old_path)
                except OSError:
                    pass
            conn.execute(text("DELETE FROM backups WHERE id = :id"), {"id": row.id})
            removed += 1

    log.info(f"[backup] Создан {filename} ({size} байт), удалено старых: {removed}")
    return filename


@celery_app.task(name="nightly_media_backup")
def nightly_media_backup():
    """
    Зеркалирование медиафайлов в MinIO (S3-хранилище из docker-compose,
    до этого простаивавшее). Докачиваются новые и изменённые по размеру
    файлы; удалённые локально файлы в бакете НЕ удаляются — это защита
    от распространения случайного удаления в резервную копию.
    Скрытые каталоги (например .thumbs — миниатюры регенерируются сами)
    не зеркалируются.
    """
    from minio import Minio

    endpoint = os.getenv("MINIO_ENDPOINT", "minio:9000")
    access_key = os.getenv("MINIO_USER")
    secret_key = os.getenv("MINIO_PASSWORD")
    bucket = os.getenv("MINIO_MEDIA_BUCKET", "ds-media-backup")
    media_path = os.getenv("MEDIA_PATH", "/data/media")

    if not access_key or not secret_key:
        log.error("[media-backup] MINIO_USER/MINIO_PASSWORD не заданы в окружении")
        return "error: MinIO credentials are not set"

    client = Minio(endpoint, access_key=access_key, secret_key=secret_key, secure=False)
    if not client.bucket_exists(bucket):
        client.make_bucket(bucket)

    existing = {}  # имя объекта -> размер в бакете
    for obj in client.list_objects(bucket, recursive=True):
        existing[obj.object_name] = obj.size

    uploaded, skipped, errors = 0, 0, 0
    for name in sorted(os.listdir(media_path)):
        full = os.path.join(media_path, name)
        if name.startswith(".") or not os.path.isfile(full):
            continue
        size = os.path.getsize(full)
        if existing.get(name) == size:
            skipped += 1
            continue
        try:
            client.fput_object(bucket, name, full)
            uploaded += 1
        except Exception as e:
            log.warning(f"[media-backup] Не удалось загрузить {name}: {e}")
            errors += 1

    # отметка в журнале операций
    database_url = os.getenv("DATABASE_URL")
    if database_url:
        try:
            from sqlalchemy import create_engine, text
            engine = create_engine(database_url, pool_pre_ping=True)
            with engine.begin() as conn:
                conn.execute(text("""
                    INSERT INTO audit_log (event_type, title, detail, actor)
                    VALUES ('backup', 'Резервная копия медиафайлов (MinIO)', :d, 'celery')
                """), {"d": f"загружено {uploaded}, без изменений {skipped}, ошибок {errors}"})
        except Exception as e:
            log.warning(f"[media-backup] Не удалось записать в audit_log: {e}")

    log.info(f"[media-backup] Готово: загружено {uploaded}, без изменений {skipped}, ошибок {errors}")
    return {"uploaded": uploaded, "skipped": skipped, "errors": errors}


@celery_app.task(name="check_notifications")
def check_notifications():
    """Раз в минуту: проверить условия и отправить новые уведомления в MAX."""
    from ds_notify import run_notifications
    try:
        return run_notifications()
    except Exception as e:
        log.error(f"[notify] Ошибка проверки уведомлений: {e}")
        return {"error": str(e)}


@celery_app.task(name="archive_play_log")
def archive_play_log():
    """
    Ежемесячное сворачивание журнала показов: сырые строки play_log старше
    ARCHIVE_AFTER_DAYS (по умолчанию 365) суммируются в дневные агрегаты
    play_log_daily (день × экран × ролик: показы и секунды), переносятся в
    play_log_archive и удаляются из play_log. Отчёты по свежим периодам
    продолжают читать play_log и не замечают изменений.
    """
    from sqlalchemy import create_engine, text

    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        log.error("[playlog-archive] DATABASE_URL не задан")
        return "error: DATABASE_URL is not set"

    keep_days = int(os.getenv("ARCHIVE_AFTER_DAYS", "365"))
    engine = create_engine(database_url, pool_pre_ping=True)
    with engine.begin() as conn:
        # 1) агрегаты (при повторном запуске за тот же день — суммируются)
        conn.execute(text("""
            INSERT INTO play_log_daily (on_date, screen_id, media_id, filename, plays, seconds)
            SELECT started_at::date, screen_id, media_id, filename,
                   COUNT(*),
                   COALESCE(SUM(EXTRACT(EPOCH FROM (ended_at - started_at))), 0)
            FROM play_log
            WHERE started_at < NOW() - (:days || ' days')::interval
            GROUP BY started_at::date, screen_id, media_id, filename
            ON CONFLICT (on_date, screen_id, media_id, filename)
            DO UPDATE SET plays = play_log_daily.plays + EXCLUDED.plays,
                          seconds = play_log_daily.seconds + EXCLUDED.seconds
        """), {"days": keep_days})
        # 2) сырьё в архив
        moved = conn.execute(text("""
            INSERT INTO play_log_archive (id, screen_id, media_id, started_at, ended_at, filename)
            SELECT id, screen_id, media_id, started_at, ended_at, filename
            FROM play_log
            WHERE started_at < NOW() - (:days || ' days')::interval
        """), {"days": keep_days}).rowcount
        # 3) удаляем из рабочей таблицы
        conn.execute(text("""
            DELETE FROM play_log
            WHERE started_at < NOW() - (:days || ' days')::interval
        """), {"days": keep_days})

    log.info(f"[playlog-archive] В архив перенесено строк: {moved}")
    return {"archived": moved}
