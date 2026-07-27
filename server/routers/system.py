"""Системные эндпоинты: health-check, метрики сервера, сводка для дашборда."""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from deps import get_db, get_current_admin

router = APIRouter()


@router.get("/health")
def health():
    return {"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()}


def compute_server_metrics() -> dict:
    """Метрики сервера через psutil. Используется и REST-эндпоинтом, и WS-пушем дашборда."""
    try:
        import psutil as _pu, time as _t
        _cpu = _pu.cpu_percent(interval=None)
        _mem = _pu.virtual_memory()
        _disks = []
        for _p in _pu.disk_partitions(all=False):
            try:
                _u = _pu.disk_usage(_p.mountpoint)
                _disks.append({"mount": _p.mountpoint,
                               "total_gb": round(_u.total/(1024**3), 1),
                               "used_gb":  round(_u.used /(1024**3), 1),
                               "free_gb":  round(_u.free /(1024**3), 1),
                               "pct":      _u.percent})
            except PermissionError:
                pass
        return {
            "cpu_pct":      _cpu,
            "ram_total":    round(_mem.total/(1024**3), 1),
            "ram_used":     round(_mem.used /(1024**3), 1),
            "ram_pct":      _mem.percent,
            "disks":        _disks,
            "uptime_hours": round((_t.time()-_pu.boot_time())/3600, 1),
        }
    except Exception:
        return {"error": "psutil unavailable"}


@router.get("/server/metrics")
def server_metrics(current_admin=Depends(get_current_admin)):
    return compute_server_metrics()


def _check(cid, title, status, detail, action=None):
    return {"id": cid, "title": title, "status": status,
            "detail": detail, "action": action}


@router.get("/system/selfcheck")
def system_selfcheck(db: Session = Depends(get_db), current_admin=Depends(get_current_admin)):
    """Самодиагностика сервера: каждый пункт — статус ok/warn/fail и подсказка
    «что делать». Проверяет только то, что видно ИЗНУТРИ стека; случай «панель
    вообще не открывается» ловит selfheal.sh на хосте (runbook §1)."""
    import os, shutil, socket, urllib.request

    checks = []

    # --- PostgreSQL (если БД лежит, до этой строки запрос не дойдёт — но
    # замер времени отклика полезен и для «живой» БД)
    try:
        t0 = datetime.now(timezone.utc)
        db.execute(text("SELECT 1"))
        ms = (datetime.now(timezone.utc) - t0).total_seconds() * 1000
        st = "ok" if ms < 500 else "warn"
        checks.append(_check("db", "База данных (PostgreSQL)", st,
                             f"отклик {ms:.0f} мс",
                             None if st == "ok" else "БД отвечает медленно: проверьте нагрузку хоста (Дашборд → Метрики) и `docker stats ds_postgres`"))
    except Exception as e:
        checks.append(_check("db", "База данных (PostgreSQL)", "fail", str(e)[:200],
                             "`docker compose restart postgres`, затем логи: `docker compose logs --tail=50 postgres` (runbook §2)"))

    # --- Миграции: файлы в migrations/ vs учёт migrate.sh
    try:
        files = sorted(f for f in os.listdir("/app/migrations") if f.endswith(".sql"))
        applied = {r[0] for r in db.execute(text(
            "SELECT filename FROM schema_migrations")).fetchall()}
        pending = [f for f in files if f not in applied]
        if not pending:
            checks.append(_check("migrations", "Миграции БД", "ok",
                                 f"применены все {len(files)}"))
        else:
            checks.append(_check("migrations", "Миграции БД", "warn",
                                 "не отмечены применёнными: " + ", ".join(pending[:5]),
                                 "на сервере выполнить `bash migrate.sh` (идемпотентно, безопасно повторять)"))
    except Exception as e:
        checks.append(_check("migrations", "Миграции БД", "warn", str(e)[:200],
                             "таблицы учёта нет — выполните `bash migrate.sh` на сервере"))

    # --- Redis
    try:
        import redis as _redis
        r = _redis.Redis.from_url(os.getenv("REDIS_URL", "redis://redis:6379"),
                                  socket_timeout=2, socket_connect_timeout=2)
        r.ping()
        checks.append(_check("redis", "Redis (кеш/очереди)", "ok", "отвечает"))
    except Exception as e:
        checks.append(_check("redis", "Redis (кеш/очереди)", "fail", str(e)[:200],
                             "`docker compose restart redis` (runbook §4); без Redis не работают Celery-задачи (бэкапы, уведомления)"))

    # --- Celery-воркер (бэкапы, уведомления MAX, архив журнала показов)
    try:
        from tasks import celery_app
        pong = celery_app.control.inspect(timeout=1.5).ping()
        if pong:
            checks.append(_check("celery", "Celery (фоновые задачи)", "ok",
                                 "воркер отвечает: " + ", ".join(pong.keys())))
        else:
            checks.append(_check("celery", "Celery (фоновые задачи)", "fail",
                                 "воркер не отвечает на ping",
                                 "`docker compose restart celery`, логи: `docker compose logs --tail=50 celery`; без него не идут авто-бэкапы и уведомления MAX"))
    except Exception as e:
        checks.append(_check("celery", "Celery (фоновые задачи)", "fail", str(e)[:200],
                             "`docker compose restart celery`; без него не идут авто-бэкапы и уведомления MAX"))

    # --- MinIO (бэкап медиатеки): health-эндпоинт без авторизации
    try:
        url = "http://" + os.getenv("MINIO_ENDPOINT", "minio:9000") + "/minio/health/live"
        with urllib.request.urlopen(url, timeout=3) as resp:
            if resp.status == 200:
                checks.append(_check("minio", "MinIO (бэкап медиа)", "ok", "живой"))
            else:
                raise RuntimeError(f"HTTP {resp.status}")
    except Exception as e:
        checks.append(_check("minio", "MinIO (бэкап медиа)", "fail", str(e)[:200],
                             "`docker compose restart minio`; если в логах «CPU does not support x86-64-v2» — см. 01_УСТАНОВКА_СЕРВЕР (пиновка старого релиза)"))

    # --- NTP: короткий SNTP-запрос контейнеру ntp (UDP 123).
    # Заодно меряем СМЕЩЕНИЕ ЧАСОВ ХОСТА: chronyd отдаёт скомпенсированное
    # истинное время, а часы контейнера = часы хоста. Разница = ошибка часов
    # сервера (история 27.07.2026: хост жил на −3 часа, службы времени нет).
    try:
        import struct, time as _time
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(2)
        t0 = _time.time()
        s.sendto(b"\x1b" + 47 * b"\0", ("ntp", 123))
        data, _ = s.recvfrom(48)
        t1 = _time.time()
        s.close()
        li = (data[0] >> 6) & 0x3          # 3 = источник не синхронизирован
        secs, frac = struct.unpack("!II", data[40:48])
        server_time = secs - 2208988800 + frac / 2**32
        offset = server_time - (t0 + t1) / 2
        if li == 3:
            checks.append(_check("ntp", "NTP (время для мини-ПК)", "ok",
                                 "отвечает (без внешнего эталона — изолированная сеть)"))
        elif abs(offset) < 3:
            checks.append(_check("ntp", "NTP (время для мини-ПК)", "ok",
                                 f"отвечает; часы сервера точны ({offset:+.1f} с)"))
        else:
            st = "warn" if abs(offset) < 60 else "fail"
            checks.append(_check("ntp", "NTP (время для мини-ПК)", st,
                                 f"часы сервера расходятся с эталоном на {offset:+.0f} с",
                                 "поправьте часы ХОСТА и настройте службу времени (chrony) — "
                                 "см. 01_УСТАНОВКА_СЕРВЕР, раздел «Часы хоста»; быстрое лечение: "
                                 "`sudo date -s \"now {} seconds\"`".format(int(-offset))))
    except Exception as e:
        checks.append(_check("ntp", "NTP (время для мини-ПК)", "fail", str(e)[:200],
                             "`docker compose up -d --build ntp` (именно --build: известный баг pid-файла chronyd чинится пересборкой); без NTP часы мини-ПК уплывают → расписание и отчёты врут"))

    # --- Диск: каталоги данных
    for cid, env, default, name in (
            ("disk_media", "MEDIA_PATH", "/data/media", "Диск: медиатека"),
            ("disk_backup", "BACKUP_DIR", "/data/backups", "Диск: бэкапы")):
        path = os.getenv(env, default)
        try:
            u = shutil.disk_usage(path)
            pct = u.used / u.total * 100
            st = "ok" if pct < 85 else ("warn" if pct < 95 else "fail")
            checks.append(_check(cid, name, st,
                                 f"занято {pct:.0f}% (свободно {u.free/1024**3:.1f} ГБ)",
                                 None if st == "ok" else "освободите место: удалите старые бэкапы/неиспользуемые ролики или расширьте диск"))
        except Exception as e:
            checks.append(_check(cid, name, "fail", str(e)[:200],
                                 f"каталог {path} недоступен — проверьте тома Docker"))

    # --- Свежесть бэкапа БД (celery делает раз в 24 ч)
    try:
        age_h = db.execute(text(
            "SELECT EXTRACT(EPOCH FROM (NOW() - MAX(created_at)))/3600 FROM backups")).scalar()
        if age_h is None:
            checks.append(_check("backup_age", "Свежесть бэкапа БД", "warn",
                                 "бэкапов ещё не было",
                                 "нажмите «Создать бэкап сейчас» ниже и проверьте Celery"))
        else:
            st = "ok" if age_h < 26 else "warn"
            checks.append(_check("backup_age", "Свежесть бэкапа БД", st,
                                 f"последний {age_h:.0f} ч назад",
                                 None if st == "ok" else "авто-бэкап отстаёт: проверьте Celery (выше) и нажмите «Создать бэкап сейчас»"))
    except Exception as e:
        checks.append(_check("backup_age", "Свежесть бэкапа БД", "warn", str(e)[:200], None))

    # --- Эфир: офлайн-экраны и битые ролики (краткая сводка, детали на Дашборде)
    try:
        row = db.execute(text("""
            SELECT COUNT(*) FILTER (WHERE last_seen IS NULL
                                     OR last_seen <= NOW() - INTERVAL '5 minutes') AS offline,
                   COUNT(*) AS total FROM screens""")).fetchone()
        broken = db.execute(text(
            "SELECT COUNT(*) FROM media WHERE is_broken = TRUE")).scalar()
        if row.total and row.offline:
            checks.append(_check("screens", "Экраны", "warn",
                                 f"офлайн {row.offline} из {row.total}",
                                 "см. Дашборд и runbook §6 (агент не выходит на связь)"))
        else:
            checks.append(_check("screens", "Экраны", "ok",
                                 f"онлайн {row.total - row.offline} из {row.total}" if row.total else "экранов пока нет"))
        if broken:
            checks.append(_check("media", "Ролики", "warn",
                                 f"битых роликов: {broken}",
                                 "см. Дашборд → «Нерабочие ролики»: перезалейте файл или снимите с эфира"))
    except Exception:
        pass

    worst = "ok"
    if any(c["status"] == "warn" for c in checks):
        worst = "warn"
    if any(c["status"] == "fail" for c in checks):
        worst = "fail"
    return {"status": worst,
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "checks": checks}


@router.get("/dashboard")
def dashboard(db: Session = Depends(get_db), current_admin=Depends(get_current_admin)):
    """Сводка для стартового экрана панели."""
    summary = db.execute(text("""
        SELECT
            COUNT(*) as total,
            COUNT(*) FILTER (WHERE last_seen > NOW() - INTERVAL '5 minutes') as online,
            COUNT(*) FILTER (WHERE status = 'offline' OR last_seen IS NULL
                              OR last_seen <= NOW() - INTERVAL '5 minutes') as offline
        FROM screens
    """)).fetchone()

    adv_count = db.execute(text("SELECT COUNT(*) FROM advertisers")).scalar()
    media_count = db.execute(text("SELECT COUNT(*) FROM media")).scalar()

    # Ролики, срок действия которых истекает в ближайшие 3 дня, — они тихо
    # пропадут из эфира; владелец должен успеть продлить или заменить.
    expiring = db.execute(text("""
        SELECT m.id, m.title, m.valid_until, a.name AS advertiser
        FROM media m
        LEFT JOIN advertisers a ON a.id = m.advertiser_id
        WHERE m.status = 'ready' AND m.review_status = 'approved'
          AND m.valid_until IS NOT NULL
          AND m.valid_until > NOW()
          AND m.valid_until <= NOW() + INTERVAL '3 days'
        ORDER BY m.valid_until
    """)).fetchall()

    bc = db.execute(text("""
        SELECT b.is_on, p.name AS playlist_name
        FROM network_broadcast b
        LEFT JOIN playlists p ON p.id = b.playlist_id
        WHERE b.id = 1
    """)).fetchone()

    now_playing = db.execute(text("""
        SELECT s.name, s.status, s.playing_file,
               m.title AS media_title, a.name AS advertiser
        FROM screens s
        LEFT JOIN media m ON m.filename = s.playing_file
        LEFT JOIN advertisers a ON a.id = m.advertiser_id
        ORDER BY s.name
    """)).fetchall()

    disks = db.execute(text("""
        SELECT name, status, disk_free_gb, disk_total_gb
        FROM screens
        ORDER BY name
    """)).fetchall()

    feed = db.execute(text("""
        SELECT event_type, title, detail, created_at
        FROM audit_log
        ORDER BY created_at DESC
        LIMIT 10
    """)).fetchall()

    broken_media = db.execute(text("""
        SELECT m.title, m.filename, a.name AS advertiser, m.error_count
        FROM media m
        LEFT JOIN advertisers a ON a.id = m.advertiser_id
        WHERE m.is_broken = TRUE
        ORDER BY m.last_error_at DESC NULLS LAST
    """)).fetchall()

    return {
        "screens_total": summary.total,
        "screens_online": summary.online,
        "screens_offline": summary.offline,
        "advertisers": adv_count,
        "media_count": media_count,
        "broadcast_on": bool(bc.is_on) if bc else False,
        "broadcast_playlist": bc.playlist_name if bc else None,
        "now_playing": [dict(r._mapping) for r in now_playing],
        "disks": [dict(r._mapping) for r in disks],
        "feed": [dict(r._mapping) for r in feed],
        "broken_media": [dict(r._mapping) for r in broken_media],
        "expiring_media": [dict(r._mapping) for r in expiring],
    }
