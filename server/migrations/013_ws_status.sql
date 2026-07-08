-- Миграция 012: признак активного WS-соединения у экрана
-- Идемпотентна.
ALTER TABLE screens ADD COLUMN IF NOT EXISTS ws_connected BOOLEAN DEFAULT FALSE;

-- При рестарте сервера сбрасываем все WS-статусы (соединения разорваны)
UPDATE screens SET ws_connected = FALSE;
