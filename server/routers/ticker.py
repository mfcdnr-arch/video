"""Бегущая строка: срочные объявления на экранах, шаблоны, подтверждение с экранов.

Строка живёт до срока (`until_at`) или «до снятия»; показывается на всех экранах, в группах или на
выбранных экранах — это решается в момент показа (ticker_rules). Правила — ticker_rules, SQL —
ticker_store, здесь только разбор запроса, сборка ответа и рассылка.

Любое изменение (запуск, правка, снятие) доходит до экранов двумя путями. Агент 1.4.0 берёт строки из
ленты и сообщает в heartbeat, какие у него бегут (панель верит этому отчёту). Старому агенту (< 1.4.0)
сервер ставит в очередь команду ticker_show / ticker_hide; её получают экраны строки ДО и ПОСЛЕ
изменения — тот, с которого строку убрали правкой, тоже. После commit подключённым агентам уходит
WS-будильник new_command, чтобы они не ждали следующего опроса. Срок, истёкший сам, снимает Celery
(tasks.expire_tickers). Все моменты — наивное МСК из Python (now_msk), не NOW() Postgres.
"""
import asyncio
import math
from datetime import datetime, timedelta

from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy.orm import Session

import ticker_store
from deps import get_db, get_current_admin, require_write
from placements_store import now_msk
from ticker_rules import (
    parse_template_body, parse_ticker_body, resolve_screens, screen_reason, summarize, ticker_live,
)
from ws_manager import ws_manager

router = APIRouter()

_LEGACY_KEYS = ("id", "message", "color", "speed", "is_all", "target", "until_at", "started_at", "started_by")


# --- JSON ---------------------------------------------------------------------

def _iso(moment: datetime | None) -> str | None:
    return moment.isoformat() if moment else None


def ticker_json(row: dict, now: datetime) -> dict:
    """Строка из ticker_store.list_live → JSON: моменты — ISO (наивное МСК), `remaining_seconds` — сколько
    осталось до срока (вверх до секунды; None — «до снятия»), `is_all` — для старых клиентов."""
    until_at = row["until_at"]
    left = None if until_at is None else max(0, math.ceil((until_at - now).total_seconds()))
    return {"id": row["id"], "message": row["message"], "color": row["color"], "speed": row["speed"],
            "target": row["target"], "is_all": row["target"] == "all",
            "group_ids": list(row["group_ids"]), "screen_ids": list(row["screen_ids"]),
            "until_at": _iso(until_at), "started_at": _iso(row["started_at"]), "started_by": row["started_by"],
            "changed_at": _iso(row["changed_at"]), "changed_by": row["changed_by"], "remaining_seconds": left}


def template_json(row: dict) -> dict:
    """Шаблон из ticker_store.list_templates → JSON в том же виде, что принимает POST/PUT:
    `term` — {"kind": "cancel"} | {"kind": "minutes", "minutes": N} | {"kind": "hour", "hour": H}."""
    term = {"kind": row["term_kind"]}
    if row["term_kind"] in ("minutes", "hour"):                    # значение лежит под ключом с именем вида
        term[row["term_kind"]] = row["term_value"]
    return {"id": row["id"], "message": row["message"], "color": row["color"], "speed": row["speed"], "term": term}


def _changed_at(row: dict, now: datetime) -> datetime:
    """Момент запуска или последней правки для «ждём подтверждения»: у строк, перенесённых миграцией,
    правки нет — берём запуск, а если нет и его, то «сутки назад» (сразу «не подтвердил», а не вечное «ждём»)."""
    return row["changed_at"] or row["started_at"] or now - timedelta(days=1)


def overview_json(live: list[dict], screens: list[dict], groups: list[dict], templates: list[dict],
                  now: datetime) -> dict:
    """Всё для раздела «Бегущая строка» одним ответом.

    `tickers` — живые строки в порядке запуска: ticker_json плюс `screens_total` (на скольких экранах строка
    сейчас) и `confirm` — «бежит на X из N» по отчётам экранов (ticker_rules.screen_reason / summarize;
    у каждого экрана в `problems` есть `screen_id`, имена экранов могут повторяться);
    `screens` — экраны как их отдаёт screens_brief плюс `live_count` (сколько живых строк у экрана);
    `groups` и `templates` — для конструктора. Строка на удалённую группу — «0 экранов», не ошибка.
    """
    live = [t for t in live if ticker_live(t["until_at"], now)]
    by_id = {s["id"]: s for s in screens}
    pairs = [(s["id"], s["group_id"]) for s in screens]
    live_count = {s["id"]: 0 for s in screens}
    tickers = []
    for t in live:
        ids = resolve_screens(t["target"], t["group_ids"], t["screen_ids"], pairs)
        for sid in ids:
            live_count[sid] += 1
        changed_at = _changed_at(t, now)
        confirm = summarize([(sid, by_id[sid]["name"], screen_reason(t["id"], by_id[sid], changed_at, now))
                             for sid in ids])
        tickers.append({**ticker_json(t, now), "screens_total": len(ids), "confirm": confirm})
    return {"generated_at": now.isoformat(), "tickers": tickers,
            "screens": [{**s, "live_count": live_count[s["id"]]} for s in screens],
            "groups": [dict(g) for g in groups],
            "templates": [template_json(t) for t in templates]}


# --- рассылка -----------------------------------------------------------------

def _wake_agents(screen_ids: list[int]) -> None:
    """WS-будильник new_command подключённым агентам (как в /command/stop): команда уже лежит в очереди,
    push лишь ускоряет опрос. Не дошёл — команду заберёт опрос (агент без WS) или следующий heartbeat:
    он будит подключённый по WS экран, у которого команда ждёт дольше ~10 с (routers/screens.py). Звать после commit."""
    connected = ws_manager.connected_screens()
    for sid in screen_ids:
        if sid in connected:
            asyncio.create_task(ws_manager.send(sid, {"type": "new_command", "count": 1}))


def _bad_request(e: ValueError) -> HTTPException:
    return HTTPException(status_code=400, detail=str(e))


# --- строки -------------------------------------------------------------------

@router.get("/tickers")
def get_tickers(db: Session = Depends(get_db), current_admin=Depends(get_current_admin)):
    """Живые строки, новая сверху. Прежний вид списка (id, message, color, speed, is_all) плюс
    target, until_at, started_at, started_by. Полную картину с подтверждением даёт /tickers/overview."""
    now = now_msk()
    rows = ticker_store.list_live(db, now)
    return [{k: v for k, v in ticker_json(t, now).items() if k in _LEGACY_KEYS} for t in reversed(rows)]


@router.get("/tickers/overview")
def get_overview(db: Session = Depends(get_db), current_admin=Depends(get_current_admin)):
    now = now_msk()
    return overview_json(ticker_store.list_live(db, now), ticker_store.screens_brief(db),
                         ticker_store.groups_brief(db), ticker_store.list_templates(db), now)


@router.post("/tickers")
async def create_ticker(body=Body(...), db: Session = Depends(get_db), current_admin=Depends(require_write)):
    """Запустить строку. Тело — новое ({message, color, speed, target, group_ids | screen_ids, term}) или
    старое ({message, is_all, screen_ids, duration}). Неверное — 400 с русским текстом."""
    now = now_msk()
    try:
        data = parse_ticker_body(body, now)
        ticker_id = ticker_store.create(db, data, current_admin["username"], now)
    except ValueError as e:
        raise _bad_request(e)
    woken = ticker_store.notify_screens(db, set(), ticker_store.targets_of(db, ticker_id), now)
    db.commit()
    _wake_agents(woken)
    return {"id": ticker_id, "status": "ok"}


@router.put("/tickers/{ticker_id}")
async def update_ticker(ticker_id: int, body=Body(...), db: Session = Depends(get_db),
                        current_admin=Depends(require_write)):
    """Поправить идущую строку. Срок не прислали (или `term: null`) — остаётся прежний момент окончания;
    снять срок — {"term": {"kind": "cancel"}}. Нет такой строки или срок вышел — 404."""
    now = now_msk()
    try:
        data = parse_ticker_body(body, now, keep_term=True)
    except ValueError as e:
        raise _bad_request(e)
    before = ticker_store.targets_of(db, ticker_id)
    try:
        ticker_store.update(db, ticker_id, data, current_admin["username"], now)
    except ValueError as e:
        raise _bad_request(e)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    woken = ticker_store.notify_screens(db, before, ticker_store.targets_of(db, ticker_id), now)
    db.commit()
    _wake_agents(woken)
    return {"status": "ok"}


@router.delete("/tickers/{ticker_id}")
async def delete_ticker(ticker_id: int, db: Session = Depends(get_db), current_admin=Depends(require_write)):
    now = now_msk()
    before = ticker_store.targets_of(db, ticker_id)
    try:
        ticker_store.remove(db, ticker_id, current_admin["username"], now)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    woken = ticker_store.notify_screens(db, before, set(), now)
    db.commit()
    _wake_agents(woken)
    return {"status": "ok"}


# --- шаблоны ------------------------------------------------------------------

@router.get("/ticker-templates")
def get_templates(db: Session = Depends(get_db), current_admin=Depends(get_current_admin)):
    return [template_json(t) for t in ticker_store.list_templates(db)]


@router.post("/ticker-templates")
def create_template(body=Body(...), db: Session = Depends(get_db), current_admin=Depends(require_write)):
    """Новый шаблон: {message, color, speed, term: {kind, minutes | hour}}. Экраны выбираются при запуске."""
    try:
        data = parse_template_body(body)
    except ValueError as e:
        raise _bad_request(e)
    template_id = ticker_store.create_template(db, data, current_admin["username"])
    db.commit()
    return {"id": template_id, "status": "ok"}


@router.put("/ticker-templates/{template_id}")
def update_template(template_id: int, body=Body(...), db: Session = Depends(get_db),
                    current_admin=Depends(require_write)):
    try:
        data = parse_template_body(body)
        ticker_store.update_template(db, template_id, data)
    except ValueError as e:
        raise _bad_request(e)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    db.commit()
    return {"status": "ok"}


@router.delete("/ticker-templates/{template_id}")
def delete_template(template_id: int, db: Session = Depends(get_db), current_admin=Depends(require_write)):
    try:
        ticker_store.delete_template(db, template_id)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    db.commit()
    return {"status": "ok"}
