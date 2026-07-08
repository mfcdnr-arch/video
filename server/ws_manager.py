"""
Реестр активных WebSocket-соединений агентов: screen_id → WebSocket.

Используется двумя сторонами:
  * routers/websockets.py — регистрирует/снимает соединение агента
    (connect/disconnect) в эндпоинте /api/ws/agent/{screen_id};
  * routers/screens.py — пушит {"type": "new_command"} подключённым агентам
    (send), чтобы агент немедленно сделал poll очереди команд вместо
    ожидания следующего цикла опроса. Сама команда всегда лежит в таблице
    commands — push лишь «будильник», поэтому потеря push не теряет команду.

Работает в памяти одного процесса uvicorn (сервер запущен одним процессом).

ИСПРАВЛЕНО 2026-07-06: раньше здесь была заглушка DummyWSManager без
connected_screens()/send() и с сигнатурой connect(websocket, client_id),
хотя вызывающий код передаёт (screen_id, ws) — из-за этого падало и
подключение агентов по WS, и POST /command/restart/{screen_id} (500).
"""
import asyncio
import logging

log = logging.getLogger(__name__)


class WSManager:
    def __init__(self):
        self._connections = {}  # screen_id -> WebSocket
        self._lock = asyncio.Lock()

    async def connect(self, screen_id: int, websocket) -> None:
        """Принять соединение и зарегистрировать его за экраном.
        Если у экрана уже было соединение (агент переподключился быстрее,
        чем сервер заметил разрыв) — старое закрывается."""
        await websocket.accept()
        async with self._lock:
            old = self._connections.get(screen_id)
            self._connections[screen_id] = websocket
        if old is not None:
            try:
                await old.close(code=4000, reason="Заменено новым соединением")
            except Exception:
                pass

    async def disconnect(self, screen_id: int, websocket=None) -> None:
        """Снять регистрацию. Если передан websocket — удаляем только если
        зарегистрировано именно оно (защита от того, что finally старого
        обработчика удалит уже новое соединение переподключившегося агента)."""
        async with self._lock:
            current = self._connections.get(screen_id)
            if current is not None and (websocket is None or current is websocket):
                del self._connections[screen_id]

    def connected_screens(self) -> list:
        """Список screen_id с активным WS-соединением."""
        return list(self._connections.keys())

    async def send(self, screen_id: int, message: dict) -> bool:
        """Отправить JSON конкретному экрану. Возвращает True при успехе;
        если экран не подключён или отправка упала — False (и мёртвое
        соединение снимается с учёта). Не бросает исключений — вызывается
        через asyncio.create_task без обработки ошибок."""
        ws = self._connections.get(screen_id)
        if ws is None:
            return False
        try:
            await ws.send_json(message)
            return True
        except Exception as e:
            log.warning(f"[WS] Не удалось отправить экрану {screen_id}: {e}")
            await self.disconnect(screen_id, ws)
            return False

    async def broadcast(self, message: dict) -> int:
        """Отправить всем подключённым агентам. Возвращает число доставок."""
        sent = 0
        for sid in list(self._connections.keys()):
            if await self.send(sid, message):
                sent += 1
        return sent


ws_manager = WSManager()
