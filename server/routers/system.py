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
