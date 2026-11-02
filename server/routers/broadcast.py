"""Общий эфир сети — один плейлист на все экраны, перекрывает индивидуальные расписания.

Эфир идёт «до отмены» или «до ЧЧ:00 сегодня» (целый час МСК, до 24); по
наступлении срока он выключается сам (Celery-задача expire_broadcast), экраны
возвращаются к расписанию. Правила — broadcast_rules, SQL — broadcast_store.
"""
import re
from datetime import date, datetime

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from sqlalchemy.orm import Session

import broadcast_store
from broadcast_rules import STALE_WINDOW_TEXT, parse_until_hour
from deps import get_db, get_current_admin, require_write
from placements_store import now_msk

router = APIRouter()


_MAX_INT4 = 2 ** 31 - 1                       # playlists.id — SERIAL
_ON_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


def _playlist_id(value) -> int | None:
    """Положительное целое id плейлиста из JSON (int или строка ASCII-цифр) в пределах int4.

    bool, дроби, «²», арабские и полноширинные цифры, а также числа за пределами
    int4 (Postgres ответил бы 500) — не id: None, то есть 400 «Не выбран плейлист».
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        text = value.strip()
        # длина — до int(): строка из тысяч цифр упирается в лимит Python на преобразование
        value = int(text) if text.isascii() and text.isdigit() and len(text) <= 12 else None
    return value if isinstance(value, int) and 0 < value <= _MAX_INT4 else None


def _parse_on_date(value) -> date:
    """`on_date` — МСК-дата, на которую открыто окно включения, строго ГГГГ-ММ-ДД; мусор — 400."""
    if isinstance(value, str) and _ON_DATE.fullmatch(value):
        try:
            return date.fromisoformat(value)
        except ValueError:
            pass
    raise HTTPException(400, "Дата окна — строка вида ГГГГ-ММ-ДД")


def parse_on_request(body, query_playlist_id, now: datetime) -> tuple[int, datetime | None]:
    """Разобрать запрос на включение эфира → (playlist_id, until_at).

    Основной вид — JSON {"playlist_id": N, "until_hour": H | null, "on_date": "ГГГГ-ММ-ДД"}.
    Для обратной совместимости работает и старый вызов без тела: ?playlist_id=N —
    «до отмены». id плейлиста из тела главнее запроса. Отсутствующий или null
    `until_hour` — «до отмены»; всё, что не целый час 1…24 позже текущего, — 400
    с понятным текстом.
    `on_date` необязателен: это МСК-дата, на которую открыто окно в панели. Если он
    есть, а `until_hour` задан и дата уже не сегодняшняя — 400 (окно устарело);
    без него — прежнее поведение (старые клиенты, smoke). Весь разбор тела — здесь.
    """
    if body is not None and not isinstance(body, dict):
        raise HTTPException(400, "Ожидается JSON-объект {playlist_id, until_hour}")
    body = body or {}
    raw_id = body["playlist_id"] if body.get("playlist_id") is not None else query_playlist_id
    playlist_id = _playlist_id(raw_id)
    if playlist_id is None:
        raise HTTPException(400, "Не выбран плейлист")
    if body.get("on_date") is not None:
        day = _parse_on_date(body["on_date"])
        if day != now.date() and body.get("until_hour") is not None:
            # Час «до 24:00», выбранный вчера в 23:xx, после полуночи означал бы эфир на сутки вперёд.
            # «До отмены» от даты не зависит.
            raise HTTPException(400, STALE_WINDOW_TEXT)
    try:
        until_at = parse_until_hour(body.get("until_hour"), now)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return playlist_id, until_at


def broadcast_json(state: dict) -> dict:
    """Состояние эфира для ответа API: моменты времени — ISO-строки (наивное МСК)."""
    return {**state,
            "until_at": state["until_at"].isoformat() if state["until_at"] else None,
            "enabled_at": state["enabled_at"].isoformat() if state["enabled_at"] else None}


@router.get("/broadcast")
def get_broadcast(db: Session = Depends(get_db), current_admin=Depends(get_current_admin)):
    return broadcast_json(broadcast_store.get_broadcast(db, now_msk()))


@router.post("/broadcast/on")
def broadcast_on(body: dict | None = Body(None), playlist_id: int | None = Query(None),
                 db: Session = Depends(get_db), current_admin=Depends(require_write)):
    now = now_msk()
    pid, until_at = parse_on_request(body, playlist_id, now)
    try:
        broadcast_store.enable(db, pid, until_at, current_admin["username"], now=now)
    except LookupError:
        raise HTTPException(status_code=404, detail="Плейлист не найден")
    db.commit()
    return {"status": "ok", **broadcast_json(broadcast_store.get_broadcast(db, now))}


@router.post("/broadcast/off")
def broadcast_off(db: Session = Depends(get_db), current_admin=Depends(require_write)):
    now = now_msk()
    broadcast_store.disable(db, current_admin["username"], now=now)
    db.commit()
    return {"status": "ok", **broadcast_json(broadcast_store.get_broadcast(db, now))}
