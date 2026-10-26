from fastapi import APIRouter
from sqlalchemy import text

router = APIRouter(prefix="/admin", tags=["admin"])

@router.get("/health")
def admin_panel_health():
    return {"status": "ok", "module": "admin_panel"}


def ad_sequence(db, playlist_id: int, venue_type: str = "other") -> list[tuple[str, float]]:
    """
    Реклама плейлиста в порядке показа: [(имя файла, длительность)], каждый
    ролик повторён repeat_count раз подряд. В выдачу попадают только готовые
    одобренные ролики, действующие по датам и допустимые для типа точки.
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

    seq = []
    for r in ad_rows:
        for _ in range(max(1, r.repeat_count or 1)):
            seq.append((r.filename, float(r.duration or 0)))
    return seq


def filler_list(db, venue_type: str = "other") -> list[tuple[str, float]]:
    """Заглушки (is_filler=TRUE), допустимые для типа точки: [(имя файла, длительность)]."""
    filler_rows = db.execute(text("""
        SELECT filename, COALESCE(duration_seconds, 0) AS duration
        FROM media WHERE is_filler = TRUE AND status = 'ready' AND review_status = 'approved'
          AND (category IS NULL OR category <> 'alcohol' OR :venue = 'store_alcohol')
        ORDER BY id
    """), {"venue": venue_type}).fetchall()
    return [(r.filename, float(r.duration or 0)) for r in filler_rows]


def distribute_fillers(ads: list[tuple[str, float]], fillers: list[tuple[str, float]],
                       target_seconds: float) -> tuple[list[str], float, int]:
    """
    Раскладывает заглушки по промежуткам между роликами ads, чтобы блок не
    превысил target_seconds. Возвращает (последовательность имён файлов,
    секунд заглушек, штук заглушек). Порядок рекламы не меняется; если
    рекламы нет, заглушек нет или места не осталось — вернётся голая реклама.
    """
    names = [fn for fn, _ in ads]
    if not ads:
        return [], 0.0, 0

    remaining = float(target_seconds) - sum(d for _, d in ads)
    if remaining <= 0 or not fillers:
        return names, 0.0, 0
    # Заглушки без длительности (0/NULL) время не набирают — цикл ниже никогда
    # не дошёл бы до границы и рос бы бесконечно (так было и до выноса функции).
    if all(d <= 0 for _, d in fillers):
        return names, 0.0, 0

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

    n = len(names)
    k = len(picks)
    result = []
    for i, fn in enumerate(names):
        result.append(fn)
        # промежуток i получает свою долю набранных заглушек, порядок ротации
        # сохраняется (без соседних повторов одной и той же заглушки)
        result.extend(picks[(i * k) // n:((i + 1) * k) // n])
    return result, picked_secs, k


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
    Запросы — ad_sequence()/filler_list(), раскладка — distribute_fillers().
    """
    ads = ad_sequence(db, playlist_id, venue_type)
    fillers = filler_list(db, venue_type)

    sequence, filler_seconds, filler_plays = distribute_fillers(ads, fillers, target_seconds)
    return {
        "sequence": sequence,
        "main_seconds": sum(d for _, d in ads),
        "main_plays": len(ads),
        "filler_seconds": filler_seconds,
        "filler_plays": filler_plays,
        "fillers_available": len(fillers),
        "fillers_available_seconds": sum(d for _, d in fillers),
        "target_seconds": float(target_seconds),
    }


def build_hourly_sequence(db, playlist_id: int, target_seconds: int = 3600,
                          venue_type: str = "other"):
    """
    Плоский список имён файлов на target_seconds (обычно час) —
    agent/ds_agent.py:current_playlist_files просто оборачивает его в list(),
    без обращения к полям словаря. Вся логика — в build_hourly_detail().
    """
    return build_hourly_detail(db, playlist_id, target_seconds, venue_type)["sequence"]
