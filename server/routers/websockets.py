"""WebSocket-эндпоинты: команды агентам в реальном времени + живой дашборд для админов."""
import asyncio
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from sqlalchemy import text
from sqlalchemy.orm import Session

from deps import engine, get_db, get_current_admin, SECRET_KEY, ALGORITHM
from ws_manager import ws_manager
from dashboard_ws import dashboard_ws_manager
from routers.system import compute_server_metrics

log = logging.getLogger(__name__)

router = APIRouter()


# ─── WebSocket: push-команды для агентов ────────────────────────────────────

@router.websocket("/ws/agent/{screen_id}")
@router.websocket("/api/ws/agent/{screen_id}")
async def agent_websocket(screen_id: int, ws: WebSocket, db: Session = Depends(get_db)):
    """
    WebSocket-соединение для агента мини ПК.
    Аутентификация: X-Token передаётся как query-параметр ?token=...
    После подключения сервер пушит {"type":"new_command"} при появлении команд.
    Агент должен ответить {"type":"ping"} на {"type":"ping"} (keepalive).

    Роут объявлен по ДВУМ путям намеренно:
      • /ws/agent/{id}     — так запрос приходит через nginx: агент стучится на
        /api/ws/agent/{id}, а nginx (proxy_pass с завершающим «/») срезает
        префикс /api. Без этого пути handshake отдавал 404 и агент молча
        откатывался на polling (задержка команд до 5 с).
      • /api/ws/agent/{id} — прямое обращение к uvicorn на :8000 (dev, тесты,
        обращения изнутри docker-сети мимо nginx).
    """
    # Аутентификация через query-параметр token
    token = ws.query_params.get("token", "")
    row = db.execute(
        text("SELECT id, name FROM screens WHERE token = :t AND id = :sid"),
        {"t": token, "sid": screen_id}
    ).fetchone()
    if not row:
        await ws.close(code=4401, reason="Неверный токен")
        return

    await ws_manager.connect(screen_id, ws)
    try:
        # Сразу проверяем — вдруг уже есть непрочитанные команды
        pending = db.execute(text(
            "SELECT COUNT(*) FROM commands WHERE screen_id = :sid AND executed_at IS NULL"
        ), {"sid": screen_id}).scalar()
        if pending:
            await ws.send_json({"type": "new_command", "count": pending})

        # Главный цикл: слушаем keepalive от агента
        while True:
            data = await ws.receive_json()
            if data.get("type") == "ping":
                await ws.send_json({"type": "pong"})
    except WebSocketDisconnect:
        pass
    except Exception as e:
        log.warning(f"[WS] Ошибка соединения с экраном {screen_id}: {e}")
    finally:
        await ws_manager.disconnect(screen_id, ws)


@router.get("/ws/status")
def ws_status(admin: dict = Depends(get_current_admin)):
    """Список экранов с активным WS-соединением."""
    return {"connected": ws_manager.connected_screens(),
            "count": len(ws_manager.connected_screens())}


# ─── WebSocket: real-time дашборд для администраторов ───────────────────────

@router.websocket("/ws/dashboard")
async def dashboard_websocket(ws: WebSocket, db: Session = Depends(get_db)):
    """
    WebSocket для администраторов: push-обновления статусов экранов.
    Авторизация: JWT через query-параметр ?token=...
    Пушит {"type":"screens_update", "screens": [...]} каждые 10 секунд.
    """
    from jose import JWTError, jwt as jose_jwt
    token = ws.query_params.get("token", "")
    try:
        payload_jwt = jose_jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username = payload_jwt.get("sub")
        if not username:
            await ws.close(code=4401, reason="Неверный токен")
            return
    except JWTError:
        await ws.close(code=4401, reason="Неверный токен")
        return

    # Дашборд отдаёт статусы ВСЕХ экранов сети. Middleware в main.py websocket‑
    # соединения не видит, поэтому роль «рекламодатель» отсекаем здесь же —
    # иначе её JWT открыл бы поток по всей сети в обход белого списка путей.
    role = db.execute(text("SELECT role FROM users WHERE username = :u"),
                      {"u": username}).scalar()
    if str(role or "").lower() == "advertiser":
        await ws.close(code=4403, reason="Недоступно для этой роли")
        return

    await dashboard_ws_manager.connect(ws)
    try:
        while True:
            # Пушим актуальные данные каждые 10 секунд
            try:
                with Session(engine) as db_sess:
                    rows = db_sess.execute(text("""
                        SELECT s.id, s.name, s.city, s.status, s.playing_file,
                               s.last_seen, s.disk_free_gb, s.disk_total_gb,
                               s.ip_address, s.agent_version, s.os_version,
                               s.vlc_version, s.display_connected, s.display_outputs,
                               g.name as group_name
                        FROM screens s
                        LEFT JOIN sync_groups g ON g.id = s.group_id
                        ORDER BY s.name
                    """)).fetchall()
                    screens = [dict(r._mapping) for r in rows]
                    # Преобразуем datetime в строку для JSON
                    for sc in screens:
                        if sc.get("last_seen"):
                            sc["last_seen"] = str(sc["last_seen"])

                    summary = db_sess.execute(text("""
                        SELECT
                            COUNT(*) as total,
                            COUNT(*) FILTER (WHERE last_seen > NOW() - INTERVAL '5 minutes') as online,
                            COUNT(*) FILTER (WHERE status = 'offline') as offline
                        FROM screens
                    """)).fetchone()

                    # ─── Метрики сервера (psutil) ──────────────────────────────
                    server_metrics = compute_server_metrics()

                await ws.send_json({
                    "type": "screens_update",
                    "screens": screens,
                    "summary": dict(summary._mapping) if summary else {},
                    "server_metrics": server_metrics,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })
            except Exception as e:
                log.warning(f"[Dashboard WS] Ошибка отправки данных: {e}")

            # Ждём 10 секунд или disconnect
            try:
                await asyncio.wait_for(ws.receive_text(), timeout=10.0)
            except asyncio.TimeoutError:
                pass  # Нормально — просто пушим снова
            except Exception:
                break  # Disconnect

    except Exception as e:
        log.debug(f"[Dashboard WS] Соединение закрыто: {e}")
    finally:
        await dashboard_ws_manager.disconnect(ws)
