-- Миграция 013: журнал перезапусков агентов (диагностика сбоев питания)
-- Идемпотентна.

CREATE TABLE IF NOT EXISTS agent_restarts (
    id SERIAL PRIMARY KEY,
    screen_id INTEGER REFERENCES screens(id) ON DELETE CASCADE,
    restarted_at TIMESTAMP DEFAULT NOW(),
    restart_count INTEGER,      -- порядковый номер перезапуска
    reason VARCHAR(64),         -- 'power_loss' | 'crash' | 'ota_update' | 'manual' | 'unknown'
    agent_version VARCHAR(32),
    os_version VARCHAR(64)
);
CREATE INDEX IF NOT EXISTS idx_restarts_screen ON agent_restarts(screen_id, restarted_at DESC);
CREATE INDEX IF NOT EXISTS idx_restarts_time ON agent_restarts(restarted_at DESC);
