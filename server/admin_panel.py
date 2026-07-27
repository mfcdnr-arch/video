from fastapi import APIRouter
from sqlalchemy import text

router = APIRouter(prefix="/admin", tags=["admin"])

@router.get("/health")
def admin_panel_health():
    return {"status": "ok", "module": "admin_panel"}


def build_hourly_detail(db, playlist_id: int, target_seconds: int = 3600,
                        venue_type: str = "other") -> dict:
    """
    Развёрнутый расчёт часового блока: реклама плейлиста + заглушки
    (is_filler=TRUE), распределённые равномерно в оставшееся время — после
    каждого ролика вставляется доля свободного времени, пропорциональная
    длине часа (см. migrations/009_fillers_hourly.sql).

    Возвращает словарь с последовательностью и разбивкой по времени —
    его использует и выдача агенту (через build_hourly_sequence), и панель
    (/playlists/{id}/fill_info: предупреждения о нехватке заглушек).
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

    filler_rows = db.execute(text("""
        SELECT filename, COALESCE(duration_seconds, 0) AS duration
        FROM media WHERE is_filler = TRUE AND status = 'ready' AND review_status = 'approved'
          AND (category IS NULL OR category <> 'alcohol' OR :venue = 'store_alcohol')
        ORDER BY id
    """), {"venue": venue_type}).fetchall()
    fillers = [(r.filename, float(r.duration or 0)) for r in filler_rows]

    ads_total = sum(d for _, d in ad_sequence)
    detail = {
        "sequence": [fn for fn, _ in ad_sequence],
        "main_seconds": ads_total,
        "main_plays": len(ad_sequence),
        "filler_seconds": 0.0,
        "filler_plays": 0,
        "fillers_available": len(fillers),
        "fillers_available_seconds": sum(d for _, d in fillers),
        "target_seconds": float(target_seconds),
    }

    if not ad_sequence:
        return detail

    remaining = float(target_seconds) - ads_total
    if remaining <= 0 or not fillers:
        return detail

    # Целевая длительность — МАКСИМУМ блока: набираем заглушки по кругу, пока
    # следующая влезает в остаток (блок никогда не превышает цель), затем
    # раскладываем их равномерно по промежуткам между основными роликами.
    picks = []
    picked_secs = 0.0
    idx = 0
    while fillers:
        f_fn, f_dur = fillers[idx % len(fillers)]
        eff = f_dur if f_dur > 0 else 1.0
        if picked_secs + eff > remaining:
            break
        picks.append(f_fn)
        picked_secs += f_dur
        idx += 1

    n = len(ad_sequence)
    k = len(picks)
    result = []
    for i, (fn, _) in enumerate(ad_sequence):
        result.append(fn)
        # промежуток i получает свою долю набранных заглушек, порядок ротации
        # сохраняется (без соседних повторов одной и той же заглушки)
        result.extend(picks[(i * k) // n:((i + 1) * k) // n])

    detail["sequence"] = result
    detail["filler_seconds"] = picked_secs
    detail["filler_plays"] = len(picks)
    return detail


def build_hourly_sequence(db, playlist_id: int, target_seconds: int = 3600,
                          venue_type: str = "other"):
    """
    Плоский список имён файлов на target_seconds (обычно час) —
    agent/ds_agent.py:current_playlist_files просто оборачивает его в list(),
    без обращения к полям словаря. Вся логика — в build_hourly_detail().
    """
    return build_hourly_detail(db, playlist_id, target_seconds, venue_type)["sequence"]
