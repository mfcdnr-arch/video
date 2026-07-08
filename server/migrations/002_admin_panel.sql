-- ============================================================
-- Digital Signage — Вариант Б (Мини ПК)
-- Миграция 002: доработки админ-панели
-- ------------------------------------------------------------
-- Идемпотентна: безопасна к повторному запуску (IF NOT EXISTS).
-- Применяется ПОСЛЕ init.sql (которая = миграция 001).
-- Добавляет: рекламодателей, папки медиатеки, кампании,
--            датовые переопределения расписания, цель экран/группа,
--            режим общего эфира сети, журнал операций,
--            бегущие строки, общий объём диска.
-- ============================================================

-- ------------------------------------------------------------
-- 1. РЕКЛАМОДАТЕЛИ И ПАПКИ МЕДИАТЕКИ
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS advertisers (
    id SERIAL PRIMARY KEY,
    name VARCHAR(128) UNIQUE NOT NULL,
    color VARCHAR(9) DEFAULT '#7fe3c4',   -- цвет-метка в интерфейсе
    note TEXT,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS media_folders (
    id SERIAL PRIMARY KEY,
    advertiser_id INTEGER REFERENCES advertisers(id) ON DELETE CASCADE,
    name VARCHAR(128) NOT NULL,
    created_at TIMESTAMP DEFAULT NOW(),
    UNIQUE (advertiser_id, name)
);

-- Привязка ролика к рекламодателю/папке + даты кампании
ALTER TABLE media ADD COLUMN IF NOT EXISTS advertiser_id INTEGER REFERENCES advertisers(id) ON DELETE SET NULL;
ALTER TABLE media ADD COLUMN IF NOT EXISTS folder_id INTEGER REFERENCES media_folders(id) ON DELETE SET NULL;
ALTER TABLE media ADD COLUMN IF NOT EXISTS campaign_start DATE;
ALTER TABLE media ADD COLUMN IF NOT EXISTS campaign_end DATE;
ALTER TABLE media ADD COLUMN IF NOT EXISTS uploaded_by VARCHAR(64);

CREATE INDEX IF NOT EXISTS idx_media_advertiser ON media(advertiser_id);
CREATE INDEX IF NOT EXISTS idx_media_folder ON media(folder_id);

-- ------------------------------------------------------------
-- 2. РАСПИСАНИЕ: цель экран/группа + датовые переопределения
-- ------------------------------------------------------------
-- Недельный шаблон уже есть (schedule_slots на screen_id).
-- Расширяем: слот может относиться к группе; плейлист на весь день.
ALTER TABLE schedule_slots ADD COLUMN IF NOT EXISTS group_id INTEGER REFERENCES sync_groups(id) ON DELETE CASCADE;
-- day_of_week остаётся; hour делаем необязательным (NULL = весь день)
ALTER TABLE schedule_slots ALTER COLUMN hour DROP NOT NULL;
ALTER TABLE schedule_slots ADD COLUMN IF NOT EXISTS all_day BOOLEAN DEFAULT TRUE;

-- Переопределения на конкретную дату (поверх недельного шаблона).
-- target: либо screen_id, либо group_id (одно из двух).
CREATE TABLE IF NOT EXISTS schedule_overrides (
    id SERIAL PRIMARY KEY,
    screen_id INTEGER REFERENCES screens(id) ON DELETE CASCADE,
    group_id INTEGER REFERENCES sync_groups(id) ON DELETE CASCADE,
    on_date DATE NOT NULL,
    playlist_id INTEGER REFERENCES playlists(id) ON DELETE CASCADE,
    is_off BOOLEAN DEFAULT FALSE,           -- TRUE = в этот день ничего не показывать
    created_by VARCHAR(64),
    created_at TIMESTAMP DEFAULT NOW(),
    CHECK ((screen_id IS NOT NULL) <> (group_id IS NOT NULL))  -- ровно одна цель
);
CREATE INDEX IF NOT EXISTS idx_overrides_screen_date ON schedule_overrides(screen_id, on_date);
CREATE INDEX IF NOT EXISTS idx_overrides_group_date  ON schedule_overrides(group_id, on_date);

-- ------------------------------------------------------------
-- 3. РЕЖИМ ОБЩЕГО ЭФИРА СЕТИ (один плейлист на «Все экраны»)
-- ------------------------------------------------------------
-- Одна строка-состояние (singleton). Включён → перекрывает
-- индивидуальные расписания; выключен → каждый экран показывает своё.
CREATE TABLE IF NOT EXISTS network_broadcast (
    id INTEGER PRIMARY KEY DEFAULT 1,
    is_on BOOLEAN DEFAULT FALSE,
    playlist_id INTEGER REFERENCES playlists(id) ON DELETE SET NULL,
    enabled_by VARCHAR(64),
    enabled_at TIMESTAMP,
    CHECK (id = 1)
);
INSERT INTO network_broadcast (id, is_on) VALUES (1, FALSE)
ON CONFLICT (id) DO NOTHING;

-- ------------------------------------------------------------
-- 4. ЖУРНАЛ ОПЕРАЦИЙ (лента «Последние операции» на дашборде)
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS audit_log (
    id SERIAL PRIMARY KEY,
    event_type VARCHAR(48) NOT NULL,    -- upload | sync | stop | register | schedule | backup | broadcast | ticker
    title VARCHAR(256) NOT NULL,
    detail TEXT,
    actor VARCHAR(64),                  -- кто (admin) или 'system'
    screen_id INTEGER REFERENCES screens(id) ON DELETE SET NULL,
    created_at TIMESTAMP DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_log(created_at DESC);

-- ------------------------------------------------------------
-- 5. БЕГУЩАЯ СТРОКА (текстовый оверлей поверх видео)
-- ------------------------------------------------------------
-- target: все экраны (group «Все экраны») или конкретные.
CREATE TABLE IF NOT EXISTS tickers (
    id SERIAL PRIMARY KEY,
    message TEXT NOT NULL,
    color VARCHAR(9) DEFAULT '#ffd34d',
    speed VARCHAR(16) DEFAULT 'medium',   -- slow | medium | fast
    is_all BOOLEAN DEFAULT TRUE,          -- TRUE = на все экраны
    is_active BOOLEAN DEFAULT TRUE,
    expires_at TIMESTAMP,                 -- NULL = до отмены
    created_by VARCHAR(64),
    created_at TIMESTAMP DEFAULT NOW()
);

-- Какие экраны охвачены строкой (если is_all = FALSE)
CREATE TABLE IF NOT EXISTS ticker_screens (
    ticker_id INTEGER REFERENCES tickers(id) ON DELETE CASCADE,
    screen_id INTEGER REFERENCES screens(id) ON DELETE CASCADE,
    PRIMARY KEY (ticker_id, screen_id)
);
CREATE INDEX IF NOT EXISTS idx_tickers_active ON tickers(is_active) WHERE is_active = TRUE;

-- ------------------------------------------------------------
-- 6. ОБЪЁМ ДИСКА (для процента заполнения, а не только свободного)
-- ------------------------------------------------------------
ALTER TABLE screens       ADD COLUMN IF NOT EXISTS disk_total_gb FLOAT;
ALTER TABLE minipc_status ADD COLUMN IF NOT EXISTS disk_total_gb FLOAT;

-- ------------------------------------------------------------
-- 7. СЛУЖЕБНАЯ ГРУППА «ВСЕ ЭКРАНЫ»
-- ------------------------------------------------------------
-- Особая группа: новый экран автоматически попадает в неё (логика в API).
INSERT INTO sync_groups (name, description) VALUES
  ('Все экраны', 'Служебная группа: включает все зарегистрированные экраны')
ON CONFLICT DO NOTHING;

-- ============================================================
-- Конец миграции 002
-- ============================================================
