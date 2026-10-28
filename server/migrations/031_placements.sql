-- ============================================================
-- Digital Signage — Вариант Б (Мини ПК)
-- Миграция 031: показы по датам (placements)
-- ------------------------------------------------------------
-- Идемпотентна. Применяется ПОСЛЕ 030_gov_owners.sql.
--
-- Показ ставит плейлист на выбранные экраны (или на все экраны сети) на
-- диапазон дат [date_from .. date_to] включительно и ежедневно на часы
-- [hour_from, hour_to) — целые часы МСК, hour_to может быть 24.
--
-- Пересечения с другими показами и датовыми переопределениями админ решает
-- сам при сохранении, отдельно для каждого экрана: «replace» — сохраняемый
-- показ вытесняет другой, «alternate» — играют по очереди в общем часу,
-- «yield» — сохраняемый показ уступает: в часы пересечения идёт другой.
-- Решения лежат под id показа, который сохраняли, поэтому приоритет у показа
-- с самым поздним saved_at (см. server/placements_logic.py).
--
-- Все отметки времени (saved_at, stopped_at, created_at) — наивное МСК и
-- пишутся из Python явно: контейнер Postgres живёт в UTC, NOW() здесь — лишь
-- запасное значение по умолчанию.
-- ============================================================

CREATE TABLE IF NOT EXISTS placements (
    id          SERIAL PRIMARY KEY,
    playlist_id INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
    all_screens BOOLEAN NOT NULL DEFAULT FALSE,   -- TRUE — все экраны сети, включая добавленные позже
    date_from   DATE NOT NULL,
    date_to     DATE NOT NULL CHECK (date_to >= date_from),
    hour_from   SMALLINT NOT NULL CHECK (hour_from BETWEEN 0 AND 23),
    hour_to     SMALLINT NOT NULL CHECK (hour_to BETWEEN 1 AND 24),
    stopped_at  TIMESTAMP,              -- МСК; NULL — не остановлен
    saved_at    TIMESTAMP NOT NULL DEFAULT NOW(),  -- МСК; создан или правился последний раз
    created_by  VARCHAR(64),
    created_at  TIMESTAMP DEFAULT NOW(),
    CHECK (hour_to > hour_from)
);

-- Экраны показа. Для all_screens = TRUE строк нет: экраны берутся из screens
-- в момент чтения, чтобы новый экран сразу попадал в показ «на все экраны».
CREATE TABLE IF NOT EXISTS placement_screens (
    placement_id INTEGER NOT NULL REFERENCES placements(id) ON DELETE CASCADE,
    screen_id    INTEGER NOT NULL REFERENCES screens(id) ON DELETE CASCADE,
    PRIMARY KEY (placement_id, screen_id)
);

-- Решения по пересечениям: показ placement_id на экране screen_id против
-- другого показа (other_kind = 'placement') или переопределения даты
-- (other_kind = 'override'). other_id без внешнего ключа — он ссылается на
-- одну из двух таблиц; висячее решение безвредно (не с чем пересекаться).
CREATE TABLE IF NOT EXISTS placement_decisions (
    placement_id INTEGER NOT NULL REFERENCES placements(id) ON DELETE CASCADE,
    screen_id    INTEGER NOT NULL REFERENCES screens(id) ON DELETE CASCADE,
    other_kind   VARCHAR(10) NOT NULL CHECK (other_kind IN ('placement', 'override')),
    other_id     INTEGER NOT NULL,
    mode         VARCHAR(10) NOT NULL CHECK (mode IN ('replace', 'alternate', 'yield')),
    PRIMARY KEY (placement_id, screen_id, other_kind, other_id)
);

CREATE INDEX IF NOT EXISTS idx_placements_dates ON placements(date_from, date_to);
CREATE INDEX IF NOT EXISTS idx_placements_playlist ON placements(playlist_id);
