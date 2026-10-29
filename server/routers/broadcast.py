"""Общий эфир сети — один плейлист на все экраны, перекрывает индивидуальные расписания.

Эфир идёт «до отмены» или «до ЧЧ:00 сегодня» (целый час МСК, до 24); по
наступлении срока он выключается сам (Celery-задача expire_broadcast), экраны
возвращаются к расписанию. Правила — broadcast_rules, SQL — broadcast_store.
"""
from datetime import datetime

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from sqlalchemy.orm import Session

import broadcast_store
from broadcast_rules import parse_until_hour
from deps import get_db, get_current_admin, require_write
from placements_store import now_msk

router = APIRouter()


def _playlist_id(value) -> int | None:
    """Положительное целое id плейлиста из JSON (int или строка цифр); bool и дроби — нет."""
    if isinstance(value, bool):
        return None
    if isinstance(value, str) and value.strip().isdigit():
        value = int(value.strip())
    return value if isinstance(value, int) and value > 0 else None


def parse_on_request(body, query_playlist_id, now: datetime) -> tuple[int, datetime | None]:
    """Разобрать запрос на включение эфира → (playlist_id, until_at).

    Основной вид — JSON {"playlist_id": N, "until_hour": H | null}. Для обратной
    совместимости работает и старый вызов без тела: ?playlist_id=N — «до отмены».
    id плейлиста из тела главнее запроса. Отсутствующий или null `until_hour` —
    «до отмены»; всё, что не целый час 1…24 позже текущего, — 400 с понятным текстом.
    """
    if body is not None and not isinstance(body, dict):
        raise HTTPException(400, "Ожидается JSON-объект {playlist_id, until_hour}")
    body = body or {}
    raw_id = body["playlist_id"] if body.get("playlist_id") is not None else query_playlist_id
    playlist_id = _playlist_id(raw_id)
    if playlist_id is None:
        raise HTTPException(400, "Не выбран плейлист")
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
    broadcast_store.disable(db, current_admin["username"])
    db.commit()
    return {"status": "ok", **broadcast_json(broadcast_store.get_broadcast(db, now_msk()))}
