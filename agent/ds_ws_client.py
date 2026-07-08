"""
ds_ws_client.py — WebSocket-клиент для получения push-команд с сервера.

Работает в отдельном потоке. При получении {"type": "new_command"} —
вызывает callback (обычно немедленный poll команд через REST).
При разрыве соединения — уведомляет ds_agent.py о переходе на polling.
"""
from __future__ import annotations
import json
import logging
import socket
import threading
import time

log = logging.getLogger(__name__)


class WSClient(threading.Thread):
    """
    WebSocket-клиент на чистых сокетах (без внешних зависимостей).
    Использует HTTP Upgrade → WebSocket RFC 6455.
    """

    RECONNECT_DELAY = 5   # сек между попытками переподключения
    PING_INTERVAL   = 30  # сек между ping-ами для keepalive

    def __init__(self, server_url: str, screen_id: int, token: str,
                 on_command: callable, on_connect: callable = None,
                 on_disconnect: callable = None):
        super().__init__(daemon=True, name="ws-client")
        self.server_url  = server_url.rstrip("/")
        self.screen_id   = screen_id
        self.token       = token
        self.on_command  = on_command     # вызывается при {"type":"new_command"}
        self.on_connect  = on_connect     # вызывается при успешном подключении
        self.on_disconnect = on_disconnect # вызывается при разрыве

        self._stop_event = threading.Event()
        self._connected  = False

    # ── Публичный интерфейс ──────────────────────────────────────────────────

    @property
    def is_connected(self) -> bool:
        return self._connected

    def stop(self):
        self._stop_event.set()

    # ── WebSocket реализация (RFC 6455) ──────────────────────────────────────

    def _parse_url(self):
        """Разобрать SERVER_URL в (host, port, path_with_query)."""
        url = self.server_url
        if url.startswith("http://"):
            host_part = url[7:]
            default_port = 80
        elif url.startswith("https://"):
            host_part = url[8:]
            default_port = 443
        else:
            host_part = url
            default_port = 80

        if "/" in host_part:
            host_port, _ = host_part.split("/", 1)
        else:
            host_port = host_part

        if ":" in host_port:
            host, port_str = host_port.rsplit(":", 1)
            port = int(port_str)
        else:
            host = host_port
            port = default_port

        path = f"/api/ws/agent/{self.screen_id}?token={self.token}"
        return host, port, path

    def _handshake(self, sock: socket.socket, host: str, path: str) -> bool:
        """HTTP Upgrade → WebSocket. Возвращает True если успешно."""
        import base64, os
        key = base64.b64encode(os.urandom(16)).decode()
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n"
            f"\r\n"
        )
        sock.sendall(request.encode())
        # Читаем ответ
        response = b""
        while b"\r\n\r\n" not in response:
            chunk = sock.recv(4096)
            if not chunk:
                return False
            response += chunk
        return b"101" in response.split(b"\r\n")[0]

    def _recv_frame(self, sock: socket.socket) -> bytes | None:
        """Принять один WebSocket фрейм. Возвращает payload или None при ошибке."""
        try:
            header = self._recv_exact(sock, 2)
            if not header:
                return None
            b1, b2 = header[0], header[1]
            masked = (b2 & 0x80) != 0
            payload_len = b2 & 0x7F
            if payload_len == 126:
                ext = self._recv_exact(sock, 2)
                payload_len = int.from_bytes(ext, "big")
            elif payload_len == 127:
                ext = self._recv_exact(sock, 8)
                payload_len = int.from_bytes(ext, "big")
            mask = self._recv_exact(sock, 4) if masked else None
            data = self._recv_exact(sock, payload_len)
            if mask:
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            opcode = b1 & 0x0F
            if opcode == 0x8:   # Close
                return None
            if opcode == 0x9:   # Ping от сервера
                self._send_frame(sock, b"", opcode=0xA)
                return b""
            return data
        except Exception:
            return None

    def _recv_exact(self, sock: socket.socket, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("Соединение закрыто")
            buf += chunk
        return buf

    def _send_frame(self, sock: socket.socket, payload: bytes, opcode: int = 0x1):
        """Отправить WebSocket фрейм (клиентская маскировка обязательна по RFC 6455)."""
        import os
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        length = len(payload)
        if length < 126:
            header = bytes([0x80 | opcode, 0x80 | length])
        elif length < 65536:
            header = bytes([0x80 | opcode, 0x80 | 126]) + length.to_bytes(2, "big")
        else:
            header = bytes([0x80 | opcode, 0x80 | 127]) + length.to_bytes(8, "big")
        sock.sendall(header + mask + masked)

    def _send_json(self, sock: socket.socket, obj: dict):
        self._send_frame(sock, json.dumps(obj).encode())

    # ── Основной цикл ────────────────────────────────────────────────────────

    def run(self):
        log.info("[WS] Клиент запущен (server={}, screen_id={})".format(
            self.server_url, self.screen_id))
        while not self._stop_event.is_set():
            try:
                host, port, path = self._parse_url()
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(10)
                sock.connect((host, port))
                if not self._handshake(sock, host, path):
                    log.warning("[WS] Handshake не удался — сервер не поддерживает WS или неверный токен")
                    sock.close()
                    time.sleep(self.RECONNECT_DELAY)
                    continue

                self._connected = True
                log.info("[WS] Подключён к серверу")
                if self.on_connect:
                    self.on_connect()

                sock.settimeout(self.PING_INTERVAL + 5)
                last_ping = time.time()

                while not self._stop_event.is_set():
                    # Отправить ping для keepalive
                    if time.time() - last_ping >= self.PING_INTERVAL:
                        self._send_json(sock, {"type": "ping"})
                        last_ping = time.time()

                    frame = self._recv_frame(sock)
                    if frame is None:
                        break
                    if not frame:
                        continue  # Pong или пустой фрейм

                    try:
                        msg = json.loads(frame.decode())
                        mtype = msg.get("type")
                        if mtype == "new_command":
                            log.info("[WS] Push: новая команда от сервера")
                            if self.on_command:
                                self.on_command()
                        elif mtype == "pong":
                            pass  # keepalive ответ
                        else:
                            log.debug("[WS] Получено: {}".format(msg))
                    except Exception as e:
                        log.debug("[WS] Не удалось разобрать сообщение: {}".format(e))

            except Exception as e:
                log.warning("[WS] Ошибка соединения: {}".format(e))
            finally:
                self._connected = False
                try:
                    sock.close()
                except Exception:
                    pass
                if self.on_disconnect:
                    self.on_disconnect()
                log.info("[WS] Переподключение через {} сек...".format(self.RECONNECT_DELAY))
                time.sleep(self.RECONNECT_DELAY)
