"""Бегущая строка: запуск/снятие, ставит ticker_show/ticker_hide в очередь команд агента."""
from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from deps import get_db, get_current_admin, require_write

router = APIRouter()


def _send_ticker_command(db: Session, screen_id: int, cmd_type: str, payload: dict):
    """
    Поставить команду ticker_show/ticker_hide в очередь для экрана.
    Агент забирает её через уже существующий polling (/command/poll/{screen_id}).
    ПРИМЕЧАНИЕ: push через ws_manager здесь не используется — ws_manager.py
    сейчас DummyWSManager без connected_screens()/send(), т.е. WS-push команд
    агентам вообще не реализован (та же проблема уже есть в существующем
    /command/restart/{screen_id} — отдельный баг, не в рамках этой задачи).
    """
    import json as _json
    db.execute(text(
        "INSERT INTO commands (screen_id, type, payload) VALUES (:sid, :t, CAST(:p AS jsonb))"
    ), {"sid": screen_id, "t": cmd_type, "p": _json.dumps(payload)})


def auto_clear_tickers(db: Session, reason: str):
    """Снять ВСЕ активные бегущие строки автоматически (без коммита — коммитит
    вызывающий). Используется при окончании показа плейлиста: команда «Стоп»
    или смена активного плейлиста экрана — задание не должно «висеть» после
    показа, при котором его запускали. Каждой строке рассылается ticker_hide,
    снятие фиксируется в журнале."""
    active = db.execute(text(
        "SELECT id, message, is_all FROM tickers WHERE is_active = TRUE")).fetchall()
    if not active:
        return 0
    for t in active:
        if t.is_all:
            targets = [r.id for r in db.execute(text("SELECT id FROM screens")).fetchall()]
        else:
            targets = [r.screen_id for r in db.execute(text(
                "SELECT screen_id FROM ticker_screens WHERE ticker_id = :tid"
            ), {"tid": t.id}).fetchall()]
        for sid in targets:
            _send_ticker_command(db, sid, "ticker_hide", {})
        db.execute(text("DELETE FROM tickers WHERE id = :tid"), {"tid": t.id})
        db.execute(text("""
            INSERT INTO audit_log (event_type, title, detail, actor)
            VALUES ('ticker', 'Бегущая строка снята автоматически', :detail, 'system')
        """), {"detail": f"{t.message} — {reason}"})
    return len(active)


@router.get("/tickers")
def get_tickers(db: Session = Depends(get_db), current_admin=Depends(get_current_admin)):
    # Истёкшие по duration строки агент снимает сам — активными их не показываем
    rows = db.execute(text("""
        SELECT id, message, color, speed, is_all
        FROM tickers WHERE is_active = TRUE
          AND (expires_at IS NULL OR expires_at > NOW())
        ORDER BY created_at DESC
    """)).fetchall()
    return [dict(r._mapping) for r in rows]


@router.post("/tickers")
def create_ticker(body: dict = Body(...), db: Session = Depends(get_db),
                   current_admin=Depends(require_write)):
    message = (body.get("message") or "").strip()
    if not message:
        raise HTTPException(status_code=400, detail="message обязателен")
    color = body.get("color") or "#ffd34d"
    speed = body.get("speed") or "medium"
    is_all = bool(body.get("is_all", True))
    screen_ids = body.get("screen_ids") or []
    # Время показа в секундах: 0/пусто = бессрочно (до снятия). >0 → проставляем
    # expires_at и передаём агенту, чтобы он сам снял строку через N секунд.
    try:
        duration = max(0, int(body.get("duration") or 0))
    except (TypeError, ValueError):
        duration = 0

    row = db.execute(text("""
        INSERT INTO tickers (message, color, speed, is_all, created_by, expires_at)
        VALUES (:msg, :color, :speed, :is_all, :who,
                CASE WHEN :dur > 0 THEN NOW() + make_interval(secs => :dur) ELSE NULL END)
        RETURNING id
    """), {"msg": message, "color": color, "speed": speed, "is_all": is_all,
           "who": current_admin["username"], "dur": duration}).fetchone()
    ticker_id = row.id

    if is_all:
        targets = [r.id for r in db.execute(text("SELECT id FROM screens")).fetchall()]
    else:
        targets = [int(i) for i in screen_ids]
        for sid in targets:
            db.execute(text(
                "INSERT INTO ticker_screens (ticker_id, screen_id) VALUES (:tid, :sid)"
            ), {"tid": ticker_id, "sid": sid})

    for sid in targets:
        _send_ticker_command(db, sid, "ticker_show", {
            "message": message, "color": color, "speed": speed, "duration": duration})

    db.execute(text("""
        INSERT INTO audit_log (event_type, title, detail, actor)
        VALUES ('ticker', 'Бегущая строка запущена', :detail, :who)
    """), {"detail": message, "who": current_admin["username"]})
    db.commit()
    return {"id": ticker_id, "status": "ok"}


@router.delete("/tickers/{ticker_id}")
def delete_ticker(ticker_id: int, db: Session = Depends(get_db),
                   current_admin=Depends(require_write)):
    ticker = db.execute(text(
        "SELECT id, message, is_all FROM tickers WHERE id = :tid"
    ), {"tid": ticker_id}).fetchone()
    if not ticker:
        raise HTTPException(status_code=404, detail="Бегущая строка не найдена")

    if ticker.is_all:
        targets = [r.id for r in db.execute(text("SELECT id FROM screens")).fetchall()]
    else:
        targets = [r.screen_id for r in db.execute(text(
            "SELECT screen_id FROM ticker_screens WHERE ticker_id = :tid"
        ), {"tid": ticker_id}).fetchall()]

    for sid in targets:
        _send_ticker_command(db, sid, "ticker_hide", {})

    db.execute(text("DELETE FROM tickers WHERE id = :tid"), {"tid": ticker_id})
    db.execute(text("""
        INSERT INTO audit_log (event_type, title, detail, actor)
        VALUES ('ticker', 'Бегущая строка снята', :detail, :who)
    """), {"detail": ticker.message, "who": current_admin["username"]})
    db.commit()
    return {"status": "ok"}
