"""Экраны/мини ПК: регистрация, heartbeat, расписание для агента, команды, группы синхронизации."""
import asyncio
import json
import secrets
from datetime import datetime, timezone, timedelta
from typing import Optional

# Вся сеть работает по московскому времени (решение от 2026-07-06):
# по нему выбираются датовые переопределения и живёт расписание агента.
# У Москвы нет сезонного перевода часов, поэтому фиксированное смещение корректно.
MSK = timezone(timedelta(hours=3))

from fastapi import APIRouter, Body, Depends, File, Header, HTTPException, Query, UploadFile
from sqlalchemy import text
from sqlalchemy.orm import Session

from deps import get_db, get_current_admin, require_write, verify_device_token
from dashboard_ws import dashboard_ws_manager
from ws_manager import ws_manager

router = APIRouter()


# ─── Экраны / мини ПК ───────────────────────────────────────────────────────

@router.post("/minipc/register")
def register_minipc(
    name: str = Query(...),
    city: str = Query(""),
    location: str = Query(""),
    group_id: Optional[int] = Query(None),
    admin: dict = Depends(require_write),
    db: Session = Depends(get_db)
):
    """Зарегистрировать новый мини ПК, получить токен"""
    token = secrets.token_urlsafe(32)
    result = db.execute(text(
        "INSERT INTO screens (name, city, location, group_id, token, status) "
        "VALUES (:n, :c, :l, :g, :t, 'offline') RETURNING id"
    ), {"n": name, "c": city, "l": location, "g": group_id, "t": token})
    screen_id = result.fetchone()[0]
    # Авто-добавление в служебную группу «Все экраны» (если экран без группы)
    if group_id is None:
        db.execute(text("""
            UPDATE screens SET group_id = (
                SELECT id FROM sync_groups WHERE name = 'Все экраны' LIMIT 1
            ) WHERE id = :id AND group_id IS NULL
        """), {"id": screen_id})
    # Запись в журнал операций (если таблица существует)
    try:
        db.execute(text("""
            INSERT INTO audit_log (event_type, title, detail, actor, screen_id)
            VALUES ('register', :ti, :d, :a, :sid)
        """), {"ti": f"Зарегистрирован экран «{name}»", "d": "токен выдан",
               "a": admin.get("username", "admin") if isinstance(admin, dict) else "admin",
               "sid": screen_id})
    except Exception:
        pass
    db.commit()
    # Возвращаем id И screen_id — для совместимости
    return {"id": screen_id, "screen_id": screen_id, "token": token, "name": name,
            "message": "Сохраните токен — он не отображается повторно!"}


@router.get("/minipc")
def list_minipcs(admin: dict = Depends(get_current_admin), db: Session = Depends(get_db)):
    rows = db.execute(text("""
        SELECT s.id, s.name, s.city, s.location, s.status, s.playing_file,
               s.last_seen, s.disk_free_gb, s.ip_address, s.agent_version,
               s.power_on_time, s.power_off_time,
               s.volume_day, s.volume_night, s.night_from, s.night_to,
               s.clock_drift_seconds, s.venue_type,
               s.display_connected, s.display_outputs, s.display_changed_at,
               g.name as group_name
        FROM screens s
        LEFT JOIN sync_groups g ON g.id = s.group_id
        ORDER BY s.name
    """)).fetchall()
    return [dict(r._mapping) for r in rows]


@router.get("/minipc/{screen_id}/status")
def minipc_status(screen_id: int, admin: dict = Depends(get_current_admin),
                  db: Session = Depends(get_db)):
    row = db.execute(text("""
        SELECT s.*, g.name as group_name FROM screens s
        LEFT JOIN sync_groups g ON g.id = s.group_id
        WHERE s.id = :id
    """), {"id": screen_id}).fetchone()
    if not row:
        raise HTTPException(404, "Экран не найден")
    return dict(row._mapping)


@router.delete("/minipc/{screen_id}")
def delete_minipc(screen_id: int, admin: dict = Depends(require_write),
                  db: Session = Depends(get_db)):
    db.execute(text("DELETE FROM screens WHERE id = :id"), {"id": screen_id})
    db.commit()
    return {"status": "deleted"}


def _parse_hhmm(value, field):
    """'HH:MM' → строка для TIME-колонки; None/'' → NULL."""
    if value in (None, ""):
        return None
    try:
        datetime.strptime(str(value), "%H:%M")
    except ValueError:
        raise HTTPException(400, f"{field}: ожидается время в формате ЧЧ:ММ")
    return str(value)


@router.patch("/minipc/{screen_id}/settings")
def update_screen_settings(screen_id: int, body: dict = Body(...),
                           admin: dict = Depends(require_write),
                           db: Session = Depends(get_db)):
    """
    Питание и громкость экрана (времена московские):
      power_on_time/power_off_time — окно работы монитора (оба NULL = всегда включён);
      volume_day/volume_night 0-100, night_from/night_to — окно ночной громкости.
    Агент подхватывает изменения при следующем опросе расписания.
    """
    exists = db.execute(text("SELECT id FROM screens WHERE id = :id"), {"id": screen_id}).fetchone()
    if not exists:
        raise HTTPException(404, "Экран не найден")

    sets, params = [], {"id": screen_id}
    if "venue_type" in body:
        vt = str(body["venue_type"])
        if vt not in ("store_alcohol", "store", "mall", "office", "other"):
            raise HTTPException(400, "venue_type: store_alcohol / store / mall / office / other")
        params["venue_type"] = vt
        sets.append("venue_type = :venue_type")
    for f in ("power_on_time", "power_off_time", "night_from", "night_to"):
        if f in body:
            params[f] = _parse_hhmm(body[f], f)
            sets.append(f"{f} = :{f}")
    for f in ("volume_day", "volume_night"):
        if f in body:
            try:
                v = int(body[f])
            except (TypeError, ValueError):
                raise HTTPException(400, f"{f}: ожидается число 0-100")
            if not 0 <= v <= 100:
                raise HTTPException(400, f"{f}: допустимо 0-100")
            params[f] = v
            sets.append(f"{f} = :{f}")

    if not sets:
        raise HTTPException(400, "Нечего обновлять")

    # окно питания задаётся либо целиком, либо никак
    row = db.execute(text(
        f"UPDATE screens SET {', '.join(sets)} WHERE id = :id "
        "RETURNING id, name, power_on_time, power_off_time, "
        "volume_day, volume_night, night_from, night_to, venue_type"
    ), params).fetchone()
    db.commit()
    out = dict(row._mapping)
    for k in ("power_on_time", "power_off_time", "night_from", "night_to"):
        if out.get(k) is not None:
            out[k] = out[k].strftime("%H:%M")
    return out


# ─── Heartbeat от мини ПК ───────────────────────────────────────────────────

@router.post("/heartbeat/{screen_id}")
async def heartbeat(screen_id: int, body: dict = Body(...), db: Session = Depends(get_db),
                     _screen=Depends(verify_device_token)):
    """
    Принять heartbeat от мини ПК.
    Тело: {"status":"online", "playing_file":"...", "disk_free_gb":10.5, "agent_version":"1.0.0"}
    """
    now = datetime.now(timezone.utc)
    # Дрейф часов: агент шлёт своё время (UNIX UTC) — положительное значение
    # означает, что часы мини-ПК отстают от сервера. NULL = агент старой
    # версии, поле ещё не шлёт.
    clock_drift = None
    agent_time = body.get("agent_time")
    if isinstance(agent_time, (int, float)) and agent_time > 0:
        clock_drift = round(now.timestamp() - float(agent_time), 1)
    # Видеовыход: True/False от агента, None = агент старой версии либо ядро
    # не отдаёт статус. NULL не затирает последнее известное значение и не
    # поднимает тревогу — см. миграцию 028.
    display = body.get("display_connected")
    display = bool(display) if isinstance(display, bool) else None
    # ИСПРАВЛЕНО: правильные имена полей из тела запроса
    db.execute(text("""
        UPDATE screens SET
            last_seen    = :ts,
            status       = 'online',
            playing_file = :pf,
            disk_free_gb = :df,
            disk_total_gb = :dt,
            agent_version = :av,
            os_version   = :osv,
            vlc_version  = :vlcv,
            ip_address   = :ip,
            clock_drift_seconds = COALESCE(:drift, clock_drift_seconds),
            display_connected = COALESCE(CAST(:disp AS BOOLEAN), display_connected),
            display_outputs   = COALESCE(CAST(:disp_out AS VARCHAR), display_outputs),
            display_changed_at = CASE
                WHEN CAST(:disp AS BOOLEAN) IS NOT NULL
                     AND display_connected IS DISTINCT FROM CAST(:disp AS BOOLEAN)
                THEN :ts ELSE display_changed_at END
        WHERE id = :id
    """), {
        "drift": clock_drift,
        "disp": display,
        "disp_out": (body.get("display_outputs") or None),
        "ts": now,
        "pf": body.get("playing_file"),   # клиент шлёт "playing_file"
        "df": body.get("disk_free_gb"),
        "dt": body.get("disk_total_gb"),
        "av": body.get("agent_version"),
        "osv": body.get("os_version"),
        "vlcv": body.get("vlc_version"),
        "ip": body.get("ip_address"),
        "id": screen_id
    })

    db.execute(text("""
        INSERT INTO minipc_status
            (screen_id, timestamp, status, playing_file, disk_free_gb, disk_total_gb, agent_version, os_version, vlc_version, ip_address, display_connected)
        VALUES (:sid, :ts, 'online', :pf, :df, :dt, :av, :osv, :vlcv, :ip, CAST(:disp AS BOOLEAN))
    """), {
        "sid": screen_id, "ts": now, "disp": display,
        "pf": body.get("playing_file"),
        "df": body.get("disk_free_gb"),
        "dt": body.get("disk_total_gb"),
        "av": body.get("agent_version"),
        "osv": body.get("os_version"),
        "vlcv": body.get("vlc_version"),
        "ip": body.get("ip_address")
    })
    db.commit()
    # Push обновления дашборду (если есть подключённые администраторы)
    if dashboard_ws_manager.client_count() > 0:
        asyncio.create_task(dashboard_ws_manager.broadcast({
            "type": "screen_heartbeat",
            "screen_id": screen_id,
            "status": "online",
            "playing_file": body.get("playing_file"),
            "timestamp": now.isoformat(),
        }))
    return {"status": "ok", "server_time": now.isoformat()}


@router.post("/agent/restart/{screen_id}")
def report_agent_restart(
    screen_id: int,
    body: dict = Body({}),
    db: Session = Depends(get_db),
    _screen=Depends(verify_device_token)
):
    """
    Агент сообщает о своём перезапуске.
    Тело: {"restart_count": 5, "reason": "unknown", "agent_version": "1.0.0", "os_version": "1.7"}
    Вызывается при старте агента (не при каждом heartbeat).
    """
    try:
        db.execute(text("""
            INSERT INTO agent_restarts (screen_id, restart_count, reason, agent_version, os_version)
            VALUES (:sid, :rc, :r, :av, :osv)
        """), {
            "sid": screen_id,
            "rc":  body.get("restart_count", 1),
            "r":   body.get("reason", "unknown"),
            "av":  body.get("agent_version"),
            "osv": body.get("os_version"),
        })
        db.commit()
    except Exception:
        pass  # Не критично — не прерываем запуск агента
    return {"status": "ok"}


@router.post("/playback/log/{screen_id}")
def playback_log(screen_id: int, body: dict = Body(...), db: Session = Depends(get_db),
                  _screen=Depends(verify_device_token)):
    """
    Агент сообщает о факте показа ролика (вызывается в момент старта воспроизведения).
    Тело: {"filename": "video.mp4"}
    Пишет в play_log; ended_at оценивается по длительности ролика из медиатеки,
    так как агент не шлёт отдельное событие об окончании показа.
    """
    filename = body.get("filename")
    if not filename:
        raise HTTPException(status_code=400, detail="filename обязателен")

    media = db.execute(
        text("SELECT id, duration_seconds FROM media WHERE filename = :f"),
        {"f": filename}
    ).fetchone()

    started = datetime.now(timezone.utc)
    duration = (media.duration_seconds if media and media.duration_seconds else 0) or 0
    ended = started + timedelta(seconds=duration)

    db.execute(text("""
        INSERT INTO play_log (screen_id, media_id, started_at, ended_at, filename)
        VALUES (:sid, :mid, :started, :ended, :fn)
    """), {
        "sid": screen_id,
        "mid": media.id if media else None,
        "started": started,
        "ended": ended,
        "fn": filename,
    })
    if media and media.id:
        # Успешный показ доказывает, что ролик больше не «нерабочий».
        db.execute(text("UPDATE media SET is_broken = FALSE WHERE id = :mid"), {"mid": media.id})
    db.commit()
    return {"status": "ok"}


@router.post("/playback/error/{screen_id}")
def playback_error(screen_id: int, body: dict = Body(...), db: Session = Depends(get_db),
                    _screen=Depends(verify_device_token)):
    """
    Агент сообщает, что ролик не смог воспроизвестись.
    Тело: {"filename": "video.mp4", "reason": "playback did not start"}
    """
    filename = body.get("filename")
    reason = body.get("reason") or ""
    if not filename:
        raise HTTPException(status_code=400, detail="filename обязателен")

    media = db.execute(text("SELECT id FROM media WHERE filename = :f"), {"f": filename}).fetchone()
    media_id = media.id if media else None

    db.execute(text("""
        INSERT INTO playback_errors (screen_id, media_id, filename, reason)
        VALUES (:sid, :mid, :fn, :r)
    """), {"sid": screen_id, "mid": media_id, "fn": filename, "r": reason})

    if media_id:
        db.execute(text("""
            UPDATE media
            SET is_broken = TRUE, last_error = :r, error_count = error_count + 1, last_error_at = NOW()
            WHERE id = :mid
        """), {"r": reason, "mid": media_id})

    db.commit()
    return {"status": "ok"}


# ─── Расписание для мини ПК ─────────────────────────────────────────────────

@router.get("/schedule/minipc/{screen_id}")
def schedule_for_minipc(screen_id: int, db: Session = Depends(get_db),
                         _screen=Depends(verify_device_token)):
    """
    Расписание для мини ПК + список нужных файлов.
    Приоритет источника плейлиста на текущий момент:
      1) Общий эфир сети (network_broadcast.is_on) — перекрывает всё;
      2) Датовое переопределение на сегодня (schedule_overrides) для экрана
         или его группы;
      3) Недельный шаблон (schedule_slots) — как раньше.
    Поле active_playlist подсказывает агенту, что играть прямо сейчас;
    поле schedule (недельный шаблон) сохранено для обратной совместимости.
    """
    today = datetime.now(MSK).date()
    active_playlist = None
    active_source = "template"

    # Тип площадки: реклама алкоголя (ст. 21 38-ФЗ) выдаётся только на
    # экраны «магазин с алколицензией» — фильтр :venue во всех выборках ниже.
    scr = db.execute(text("SELECT group_id, COALESCE(venue_type,'other') AS venue_type "
                          "FROM screens WHERE id = :sid"), {"sid": screen_id}).fetchone()
    venue = scr.venue_type if scr else "other"
    VENUE_FILTER = " AND (m.category IS NULL OR m.category <> 'alcohol' OR :venue = 'store_alcohol') "

    # 1) Общий эфир
    bc = db.execute(text("""
        SELECT b.playlist_id, p.name FROM network_broadcast b
        LEFT JOIN playlists p ON p.id = b.playlist_id
        WHERE b.id = 1 AND b.is_on = TRUE AND b.playlist_id IS NOT NULL
    """)).fetchone()
    if bc:
        active_playlist = {"playlist_id": bc[0], "playlist_name": bc[1]}
        active_source = "broadcast"

    # 2) Датовое переопределение (экран или его группа), если эфир выключен
    if active_playlist is None:
        ov = db.execute(text("""
            SELECT so.playlist_id, p.name, so.is_off
            FROM schedule_overrides so
            LEFT JOIN playlists p ON p.id = so.playlist_id
            WHERE so.on_date = :d
              AND (so.screen_id = :sid
                   OR so.group_id = (SELECT group_id FROM screens WHERE id = :sid))
            ORDER BY so.screen_id NULLS LAST  -- приоритет переопределению на экран
            LIMIT 1
        """), {"d": today, "sid": screen_id}).fetchone()
        if ov:
            if ov[2]:  # is_off
                active_playlist = {"playlist_id": None, "playlist_name": None, "off": True}
            else:
                active_playlist = {"playlist_id": ov[0], "playlist_name": ov[1]}
            active_source = "override"

    # Файлы активного плейлиста (если определён переопределением/эфиром)
    active_items = []
    if active_playlist and active_playlist.get("playlist_id"):
        active_items = [dict(r._mapping) for r in db.execute(text(f"""
            SELECT m.id AS media_id, m.filename, m.filesize, m.md5_hash,
                   pi.repeat_count, pi.position
            FROM playlist_items pi
            JOIN media m ON m.id = pi.media_id
            WHERE pi.playlist_id = :p AND m.status = 'ready' AND m.review_status = 'approved'
              {VENUE_FILTER}
              AND (m.valid_from  IS NULL OR m.valid_from  <= NOW())
              AND (m.valid_until IS NULL OR m.valid_until >= NOW())
            ORDER BY pi.position
        """), {"p": active_playlist["playlist_id"], "venue": venue}).fetchall()]

    # Трёхуровневая резолюция недельного шаблона: на каждый (день, час)
    # берётся слот с наивысшим приоритетом: экран (3) > группа (2) > сеть (1).
    _SLOT_RESOLUTION = """
        WITH candidates AS (
            SELECT day_of_week, hour, playlist_id,
                   CASE WHEN screen_id IS NOT NULL THEN 3
                        WHEN group_id IS NOT NULL THEN 2 ELSE 1 END AS prio
            FROM schedule_slots
            WHERE screen_id = :sid
               OR (screen_id IS NULL AND group_id IS NOT NULL
                   AND group_id = (SELECT group_id FROM screens WHERE id = :sid))
               OR (screen_id IS NULL AND group_id IS NULL)
        ), best AS (
            SELECT DISTINCT ON (day_of_week, COALESCE(hour, -1))
                   day_of_week, hour, playlist_id
            FROM candidates
            ORDER BY day_of_week, COALESCE(hour, -1), prio DESC
        )
    """

    slots = db.execute(text(f"""
        {_SLOT_RESOLUTION}
        SELECT b.day_of_week, b.hour, p.id as playlist_id, p.name as playlist_name,
               json_agg(json_build_object(
                   'media_id', m.id,
                   'filename', m.filename,
                   'filesize', m.filesize,
                   'md5_hash', m.md5_hash,
                   'repeat_count', pi.repeat_count,
                   'position', pi.position
               ) ORDER BY pi.position) as items
        FROM best b
        JOIN playlists p ON p.id = b.playlist_id
        JOIN playlist_items pi ON pi.playlist_id = p.id
        JOIN media m ON m.id = pi.media_id
        WHERE m.status = 'ready' AND m.review_status = 'approved'
          {VENUE_FILTER}
        GROUP BY b.day_of_week, b.hour, p.id, p.name
        ORDER BY b.day_of_week, b.hour
    """), {"sid": screen_id, "venue": venue}).fetchall()

    files = db.execute(text(f"""
        {_SLOT_RESOLUTION}
        SELECT DISTINCT m.id, m.filename, m.filesize, m.md5_hash
        FROM media m
        JOIN playlist_items pi ON pi.media_id = m.id
        JOIN best b ON b.playlist_id = pi.playlist_id
        WHERE m.status = 'ready' AND m.review_status = 'approved'
          {VENUE_FILTER}
    """), {"sid": screen_id, "venue": venue}).fetchall()

    files_list = [{"media_id": f.id, "filename": f.filename,
                   "filesize": f.filesize, "md5_hash": f.md5_hash} for f in files]
    # дополнить файлами активного плейлиста (эфир/переопределение),
    # чтобы агент успел их докачать
    seen = {f["media_id"] for f in files_list}
    for it in active_items:
        if it["media_id"] not in seen:
            files_list.append({"media_id": it["media_id"], "filename": it["filename"],
                               "filesize": it["filesize"], "md5_hash": it["md5_hash"]})
            seen.add(it["media_id"])

    # Часовая последовательность (реклама + заглушки равномерно), если у
    # активного плейлиста включён режим fill_to_hour. Агент проигрывает её
    # по порядку. Файлы заглушек тоже добавляем в список загрузки.
    hourly_sequence = []
    if active_playlist and active_playlist.get("playlist_id"):
        try:
            from admin_panel import build_hourly_sequence
            plrow = db.execute(text(
                "SELECT fill_to_hour, COALESCE(target_seconds,3600) FROM playlists WHERE id = :p"),
                {"p": active_playlist["playlist_id"]}).fetchone()
            if plrow and plrow[0]:
                hourly_sequence = build_hourly_sequence(
                    db, active_playlist["playlist_id"], plrow[1], venue_type=venue)
                # докачать файлы заглушек
                fillers = db.execute(text("""
                    SELECT id, filename, filesize, md5_hash FROM media
                    WHERE is_filler = TRUE AND status='ready' AND review_status='approved'
                """)).fetchall()
                for fr in fillers:
                    if fr[0] not in seen:
                        files_list.append({"media_id": fr[0], "filename": fr[1],
                                           "filesize": fr[2], "md5_hash": fr[3]})
                        seen.add(fr[0])
        except Exception:
            hourly_sequence = []

    # Фолбэк: если по расписанию ничего не выпало (дыра между слотами),
    # агент крутит заглушки вместо чёрного экрана. Файлы заглушек всегда
    # включаются в список загрузки, чтобы лежали на диске заранее.
    fallback_rows = db.execute(text(f"""
        SELECT m.id, m.filename, m.filesize, m.md5_hash FROM media m
        WHERE m.is_filler = TRUE AND m.status = 'ready' AND m.review_status = 'approved'
          {VENUE_FILTER} ORDER BY m.id
    """), {"venue": venue}).fetchall()
    fallback = [fr.filename for fr in fallback_rows]
    for fr in fallback_rows:
        if fr.id not in seen:
            files_list.append({"media_id": fr.id, "filename": fr.filename,
                               "filesize": fr.filesize, "md5_hash": fr.md5_hash})
            seen.add(fr.id)

    # Индивидуальная длительность баннеров-картинок: filename → секунды.
    # Агент передаёт это mpv per-file (image-display-duration), чтобы
    # каждый баннер висел столько, сколько задано при загрузке.
    img_rows = db.execute(text("""
        SELECT filename, COALESCE(duration_seconds, 10) AS d FROM media
        WHERE status = 'ready' AND review_status = 'approved'
          AND (LOWER(filename) LIKE '%.png' OR LOWER(filename) LIKE '%.jpg'
               OR LOWER(filename) LIKE '%.jpeg' OR LOWER(filename) LIKE '%.bmp'
               OR LOWER(filename) LIKE '%.webp' OR LOWER(filename) LIKE '%.gif')
    """)).fetchall()
    image_durations = {r.filename: float(r.d) for r in img_rows}

    # Питание и громкость экрана (времена — московские, см. миграцию 018)
    pw = db.execute(text("""
        SELECT power_on_time, power_off_time, volume_day, volume_night, night_from, night_to
        FROM screens WHERE id = :sid
    """), {"sid": screen_id}).fetchone()
    power = {
        "on": pw.power_on_time.strftime("%H:%M") if pw and pw.power_on_time else None,
        "off": pw.power_off_time.strftime("%H:%M") if pw and pw.power_off_time else None,
    }
    volume = {
        "day": pw.volume_day if pw and pw.volume_day is not None else 100,
        "night": pw.volume_night if pw and pw.volume_night is not None else 100,
        "night_from": pw.night_from.strftime("%H:%M") if pw and pw.night_from else None,
        "night_to": pw.night_to.strftime("%H:%M") if pw and pw.night_to else None,
    }

    # ── Авто-снятие бегущей строки при окончании показа плейлиста ──────────
    # Сервер запоминает, какой плейлист экран играет сейчас (last_playlist_id).
    # Когда он меняется (кончился слот, включили другой плейлист/эфир) —
    # активные бегущие строки снимаются автоматически: задание не «висит»
    # после показа, при котором его запускали. Команда «Стоп» чистит строку
    # отдельно (см. /command/stop). Первый запуск показа (NULL → плейлист)
    # сменой не считается.
    try:
        if active_playlist is not None:
            cur_pid = active_playlist.get("playlist_id")  # None при is_off
        else:
            now_msk = datetime.now(MSK)
            day_rows = [r for r in slots if r.day_of_week == now_msk.weekday()]
            hit = (next((r for r in day_rows if r.hour == now_msk.hour), None)
                   or next((r for r in day_rows if r.hour is None), None))
            cur_pid = hit.playlist_id if hit else None
        prev_pid = db.execute(text(
            "SELECT last_playlist_id FROM screens WHERE id = :sid"),
            {"sid": screen_id}).scalar()
        if prev_pid != cur_pid:
            db.execute(text(
                "UPDATE screens SET last_playlist_id = :p WHERE id = :sid"),
                {"p": cur_pid, "sid": screen_id})
            if prev_pid is not None:
                from routers.ticker import auto_clear_tickers
                auto_clear_tickers(db, "показ плейлиста завершён (смена расписания)")
            db.commit()
    except Exception:
        db.rollback()

    return {
        "screen_id": screen_id,
        "active_playlist": active_playlist,   # что играть прямо сейчас (приоритет)
        "active_source": active_source,       # broadcast | override | template
        "active_items": active_items,
        "hourly_sequence": hourly_sequence,   # реклама + заглушки на час (по порядку)
        "schedule": [dict(r._mapping) for r in slots],
        "files": files_list,
        "fallback": fallback,                 # заглушки на случай дыры в расписании
        "image_durations": image_durations,   # баннер → секунды показа
        "power": power,                       # окно работы монитора (МСК)
        "volume": volume,                     # громкость день/ночь (МСК)
        "generated_at": datetime.now(timezone.utc).isoformat()
    }


# ─── Команды для мини ПК ────────────────────────────────────────────────────

@router.get("/command/poll/{screen_id}")
def poll_commands(screen_id: int, db: Session = Depends(get_db),
                   _screen=Depends(verify_device_token)):
    """
    Мини ПК опрашивает сервер на наличие команд.
    Возвращает: {"command": {...}} или {"command": null}
    """
    row = db.execute(text("""
        SELECT id, type, payload FROM commands
        WHERE screen_id = :sid AND executed_at IS NULL
        ORDER BY created_at LIMIT 1
    """), {"sid": screen_id}).fetchone()
    if not row:
        return {"command": None}
    return {"command": {"id": row.id, "type": row.type, "payload": row.payload}}


@router.post("/command/ack/{command_id}")
def ack_command(command_id: int, db: Session = Depends(get_db),
                 x_token: Optional[str] = Header(default=None, alias="X-Token")):
    """
    Мини ПК подтверждает выполнение команды.
    command_id не связан напрямую с screen_id в пути, поэтому здесь свой
    вариант проверки: находим экран-владелец команды через JOIN и сверяем
    его токен с X-Token (verify_device_token из deps.py не подходит —
    ему нужен screen_id как параметр пути).
    """
    if not x_token:
        raise HTTPException(status_code=401, detail="Device token missing")

    row = db.execute(text("""
        SELECT s.token FROM commands c
        JOIN screens s ON s.id = c.screen_id
        WHERE c.id = :cid
    """), {"cid": command_id}).fetchone()
    if not row or row.token != x_token:
        raise HTTPException(status_code=401, detail="Invalid device token")

    db.execute(text("UPDATE commands SET executed_at = NOW() WHERE id = :id"),
               {"id": command_id})
    db.commit()
    return {"status": "acknowledged"}


# ─── Синхронизация ──────────────────────────────────────────────────────────

@router.get("/groups")
def list_groups(admin: dict = Depends(get_current_admin), db: Session = Depends(get_db)):
    rows = db.execute(text("SELECT * FROM sync_groups ORDER BY name")).fetchall()
    return [dict(r._mapping) for r in rows]


@router.post("/groups")
def create_group(name: str = Query(...), description: str = Query(""),
                 admin: dict = Depends(require_write), db: Session = Depends(get_db)):
    result = db.execute(text(
        "INSERT INTO sync_groups (name, description) VALUES (:n, :d) RETURNING id"
    ), {"n": name, "d": description})
    gid = result.fetchone()[0]
    db.commit()
    return {"id": gid, "name": name}


@router.post("/groups/{group_id}/screens/{screen_id}")
def add_screen_to_group(
    group_id: int,
    screen_id: int,
    admin: dict = Depends(require_write),
    db: Session = Depends(get_db)
):
    """Добавить экран в группу синхронизации"""
    row = db.execute(text("SELECT id FROM sync_groups WHERE id = :gid"), {"gid": group_id}).fetchone()
    if not row:
        raise HTTPException(404, "Группа не найдена")
    db.execute(text("UPDATE screens SET group_id = :gid WHERE id = :sid"),
               {"gid": group_id, "sid": screen_id})
    db.commit()
    return {"status": "ok", "group_id": group_id, "screen_id": screen_id}


@router.delete("/groups/{group_id}/screens/{screen_id}")
def remove_screen_from_group(
    group_id: int,
    screen_id: int,
    admin: dict = Depends(get_current_admin),
    db: Session = Depends(get_db)
):
    """Убрать экран из группы"""
    db.execute(text("UPDATE screens SET group_id = NULL WHERE id = :sid AND group_id = :gid"),
               {"sid": screen_id, "gid": group_id})
    db.commit()
    return {"status": "ok"}


@router.delete("/groups/{group_id}")
def delete_group(
    group_id: int,
    admin: dict = Depends(require_write),
    db: Session = Depends(get_db)
):
    """Удалить группу экранов (экраны остаются, group_id обнуляется)"""
    db.execute(text("UPDATE screens SET group_id = NULL WHERE group_id = :gid"), {"gid": group_id})
    db.execute(text("DELETE FROM sync_groups WHERE id = :gid"), {"gid": group_id})
    db.commit()
    return {"status": "ok"}


@router.post("/command/restart/{screen_id}")
async def command_restart_agent(
    screen_id: int,
    admin: dict = Depends(require_write),
    db: Session = Depends(get_db)
):
    """Отправить команду перезапуска агента на мини ПК (очередь команд + WS-будильник)"""
    import json as _json
    import asyncio as _asyncio
    # Сначала кладём команду в очередь — это источник истины,
    # агент заберёт её при poll даже если push не дойдёт.
    db.execute(text(
        "INSERT INTO commands (screen_id, type, payload) VALUES (:sid, 'restart_agent', CAST(:p AS jsonb))"
    ), {"sid": screen_id, "p": _json.dumps({})})
    db.commit()
    # Push подключённому агенту: new_command → агент немедленно делает poll.
    # (Тип именно new_command — других типов ds_ws_client.py не обрабатывает.)
    connected = ws_manager.connected_screens()
    if screen_id in connected:
        _asyncio.create_task(ws_manager.send(screen_id, {"type": "new_command", "count": 1}))
    return {"status": "ok", "ws_sent": screen_id in connected}


@router.post("/command/resume/{screen_id}")
async def command_resume_screen(
    screen_id: int,
    admin: dict = Depends(require_write),
    db: Session = Depends(get_db)
):
    """Возобновить показ по расписанию: снимает на агенте флаг «остановлено
    пользователем» (в отличие от restart_agent — без sudo/перезапуска, надёжно
    через обычный опрос команд)."""
    import json as _json
    import asyncio as _asyncio
    db.execute(text(
        "INSERT INTO commands (screen_id, type, payload) VALUES (:sid, 'resume', CAST(:p AS jsonb))"
    ), {"sid": screen_id, "p": _json.dumps({})})
    db.commit()
    connected = ws_manager.connected_screens()
    if screen_id in connected:
        _asyncio.create_task(ws_manager.send(screen_id, {"type": "new_command", "count": 1}))
    return {"status": "ok", "ws_sent": screen_id in connected}


@router.post("/command/stop/{screen_id}")
async def command_stop_screen(
    screen_id: int,
    admin: dict = Depends(require_write),
    db: Session = Depends(get_db)
):
    """Отправить команду остановки показа на мини ПК (очередь команд + WS-будильник).
    Разовая остановка: агент возобновит показ по расписанию на следующем цикле."""
    import json as _json
    import asyncio as _asyncio
    db.execute(text(
        "INSERT INTO commands (screen_id, type, payload) VALUES (:sid, 'stop', CAST(:p AS jsonb))"
    ), {"sid": screen_id, "p": _json.dumps({})})
    # Показ остановлен → активные бегущие строки снимаются автоматически
    # (задание не должно «висеть» после окончания показа, при котором его
    # запускали; при новом показе строку запускают заново)
    from routers.ticker import auto_clear_tickers
    auto_clear_tickers(db, "показ остановлен командой «Стоп»")
    db.commit()
    connected = ws_manager.connected_screens()
    if screen_id in connected:
        _asyncio.create_task(ws_manager.send(screen_id, {"type": "new_command", "count": 1}))
    return {"status": "ok", "ws_sent": screen_id in connected}


# ─── Диагностика экрана: агент собирает архив логов по команде ──────────────

import os as _os
DIAG_DIR = _os.path.join(_os.getenv("BACKUP_DIR", "/data/backups"), "diagnostics")
_os.makedirs(DIAG_DIR, exist_ok=True)
DIAG_KEEP_PER_SCREEN = 5


@router.post("/minipc/{screen_id}/collect-diag")
async def collect_diag(screen_id: int, admin: dict = Depends(require_write),
                       db: Session = Depends(get_db)):
    """Поставить агенту команду собрать архив диагностики и прислать на сервер."""
    if not db.execute(text("SELECT 1 FROM screens WHERE id=:id"), {"id": screen_id}).fetchone():
        raise HTTPException(404, "Экран не найден")
    db.execute(text(
        "INSERT INTO commands (screen_id, type, payload) VALUES (:sid, 'collect_diag', '{}'::jsonb)"
    ), {"sid": screen_id})
    db.commit()
    connected = ws_manager.connected_screens()
    if screen_id in connected:
        asyncio.create_task(ws_manager.send(screen_id, {"type": "new_command", "count": 1}))
    return {"status": "ok", "ws_sent": screen_id in connected}


@router.get("/minipc/{screen_id}/diagnostics")
def list_diagnostics(screen_id: int, admin: dict = Depends(get_current_admin),
                     db: Session = Depends(get_db)):
    rows = db.execute(text("""
        SELECT id, filename, size_bytes, created_at FROM screen_diagnostics
        WHERE screen_id = :sid ORDER BY created_at DESC LIMIT 20
    """), {"sid": screen_id}).fetchall()
    return [dict(r._mapping) for r in rows]


@router.post("/diagnostics/upload/{screen_id}")
async def upload_diagnostics(screen_id: int, file: UploadFile = File(...),
                             db: Session = Depends(get_db),
                             _screen=Depends(verify_device_token)):
    """Агент загружает собранный архив (X-Token). Храним последние
    DIAG_KEEP_PER_SCREEN архивов на экран, старые удаляем."""
    safe = "diag_scr{}_{}.tar.gz".format(
        screen_id, datetime.now(MSK).strftime("%Y%m%d_%H%M%S"))
    path = _os.path.join(DIAG_DIR, safe)
    size = 0
    with open(path, "wb") as f:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            f.write(chunk)
            size += len(chunk)
            if size > 50 * 1024 * 1024:   # защита от бесконечного файла
                f.close()
                _os.remove(path)
                raise HTTPException(413, "Архив диагностики больше 50 МБ")
    db.execute(text(
        "INSERT INTO screen_diagnostics (screen_id, filename, size_bytes) "
        "VALUES (:sid, :fn, :sz)"), {"sid": screen_id, "fn": safe, "sz": size})
    old = db.execute(text("""
        SELECT id, filename FROM screen_diagnostics WHERE screen_id = :sid
        ORDER BY created_at DESC OFFSET :keep
    """), {"sid": screen_id, "keep": DIAG_KEEP_PER_SCREEN}).fetchall()
    for r in old:
        try:
            _os.remove(_os.path.join(DIAG_DIR, r.filename))
        except OSError:
            pass
        db.execute(text("DELETE FROM screen_diagnostics WHERE id=:id"), {"id": r.id})
    db.commit()
    return {"status": "ok", "filename": safe, "size_bytes": size}


@router.get("/diagnostics/{diag_id}/download")
def download_diagnostics(diag_id: int, db: Session = Depends(get_db)):
    # Как и у бэкапов/медиа: скачивание по непрозрачному id без заголовка
    # авторизации (панель открывает ссылку в новой вкладке; наружу порт закрыт)
    from fastapi.responses import FileResponse
    row = db.execute(text("SELECT filename FROM screen_diagnostics WHERE id=:id"),
                     {"id": diag_id}).fetchone()
    if not row:
        raise HTTPException(404, "Архив не найден")
    path = _os.path.join(DIAG_DIR, row.filename)
    if not _os.path.exists(path):
        raise HTTPException(404, "Файл архива отсутствует на диске")
    return FileResponse(path, media_type="application/gzip", filename=row.filename)


@router.post("/sync/start/{group_id}")
async def sync_start(
    group_id: int,
    filename: str = Query(...),
    delay_seconds: int = Query(10),
    admin: dict = Depends(require_write),
    db: Session = Depends(get_db)
):
    """Запустить синхронное воспроизведение для всех экранов группы"""
    screens = db.execute(text(
        "SELECT id FROM screens WHERE group_id = :gid"
    ), {"gid": group_id}).fetchall()

    if not screens:
        raise HTTPException(400, "В группе нет экранов")

    play_at = datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)
    # ИСПРАВЛЕНО: передаём UNIX timestamp (float), а не ISO строку
    play_at_ts = play_at.timestamp()

    # ИСПРАВЛЕНО: используем json.dumps вместо str().replace()
    payload = json.dumps({"filename": filename, "play_at": play_at_ts})

    for screen in screens:
        db.execute(text(
            "INSERT INTO commands (screen_id, type, payload) VALUES (:sid, 'sync_play', CAST(:p AS jsonb))"
        ), {"sid": screen.id, "p": payload})
    db.commit()

    # Push-уведомление подключённым агентам
    for screen in screens:
        asyncio.create_task(
            ws_manager.send(screen.id, {"type": "new_command", "count": 1})
        )

    return {
        "status": "sync_scheduled",
        "group_id": group_id,
        "screens_count": len(screens),
        "play_at_iso": play_at.isoformat(),
        "play_at_ts": play_at_ts
    }
