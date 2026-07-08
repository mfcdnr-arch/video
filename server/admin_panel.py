from fastapi import APIRouter
from sqlalchemy import text

router = APIRouter(prefix="/admin", tags=["admin"])

@router.get("/health")
def admin_panel_health():
    return {"status": "ok", "module": "admin_panel"}


def build_hourly_sequence(db, playlist_id: int, target_seconds: int = 3600,
                          venue_type: str = "other"):
    """
    Порядок показа на target_seconds (обычно час): реклама плейлиста + заглушки
    (is_filler=TRUE), распределённые равномерно в оставшееся время — после
    каждого ролика вставляется доля свободного времени, пропорциональная
    длине часа (см. migrations/009_fillers_hourly.sql).

    Возвращает ПЛОСКИЙ список имён файлов — agent/ds_agent.py:current_playlist_files
    просто оборачивает его в list(), без обращения к полям словаря.
    """
    ad_rows = db.execute(text("""
        SELECT m.filename, COALESCE(m.duration_seconds, 0) AS duration, pi.repeat_count
        FROM playlist_items pi
        JOIN media m ON m.id = pi.media_id
        WHERE pi.playlist_id = :pid AND m.status = 'ready' AND m.review_status = 'approved'
          AND (m.category IS NULL OR m.category <> 'alcohol' OR :venue = 'store_alcohol')
          AND (m.valid_from IS NULL OR m.valid_from <= NOW())
          AND (m.valid_until IS NULL OR m.valid_until >= NOW())
        ORDER BY pi.position
    """), {"pid": playlist_id, "venue": venue_type}).fetchall()

    ad_sequence = []
    for r in ad_rows:
        for _ in range(max(1, r.repeat_count or 1)):
            ad_sequence.append((r.filename, float(r.duration or 0)))

    if not ad_sequence:
        return []

    filler_rows = db.execute(text("""
        SELECT filename, COALESCE(duration_seconds, 0) AS duration
        FROM media WHERE is_filler = TRUE AND status = 'ready' AND review_status = 'approved'
          AND (category IS NULL OR category <> 'alcohol' OR :venue = 'store_alcohol')
        ORDER BY id
    """), {"venue": venue_type}).fetchall()

    ads_total = sum(d for _, d in ad_sequence)
    remaining = float(target_seconds) - ads_total

    if remaining <= 0 or not filler_rows:
        return [fn for fn, _ in ad_sequence]

    gap_per_ad = remaining / len(ad_sequence)
    fillers = [(r.filename, float(r.duration or 0)) for r in filler_rows]

    result = []
    filler_idx = 0
    for fn, _ in ad_sequence:
        result.append(fn)
        gap_budget = gap_per_ad
        while gap_budget > 0 and fillers:
            f_fn, f_dur = fillers[filler_idx % len(fillers)]
            result.append(f_fn)
            gap_budget -= f_dur if f_dur > 0 else 1
            filler_idx += 1

    return result
