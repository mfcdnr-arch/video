-- ============================================================
-- Digital Signage — Вариант Б (Мини ПК)
-- Миграция 029: раздел «Рекламодатели» — карточка, документы, договоры
-- ------------------------------------------------------------
-- Идемпотентна. Применяется ПОСЛЕ 028_display_signal.sql.
--
-- Зачем: данные по рекламодателю сейчас разбросаны по четырём разделам
-- (медиатека, отчёты, кампании, биллинг). Раздел собирает их в карточку и
-- добавляет то, чего не было: реквизиты сторон, договоры, подписываемые
-- документы (эфирная справка, акт), компенсации за недоставленные выходы,
-- заявки на размещение и доступ самого рекламодателя (только к своему).
--
-- Решения заказчика (28.07.2026), закреплённые в схеме:
--   • контакты/OTS НЕ считаем — только доказуемый факт из play_log;
--   • НДС не начисляем: расчёты по УСН (vat_mode='usn', vat_rate=0),
--     но поля есть — если режим сменится, менять придётся данные, не схему;
--   • расчётный период произвольный, из договора, а не календарный месяц;
--   • сумма счёта автоматически не меняется: компенсация — отдельная
--     запись с решением человека.
-- ============================================================

-- ─── 1. Реквизиты исполнителя (наши). Одна строка, как notification_settings ─
CREATE TABLE IF NOT EXISTS company_settings (
    id             SMALLINT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    legal_name     TEXT,                    -- ООО «Ромашка»
    short_name     TEXT,                    -- как подписывать в шапке документов
    inn            VARCHAR(12),
    kpp            VARCHAR(9),
    ogrn           VARCHAR(15),
    legal_address  TEXT,
    postal_address TEXT,
    bank_name      TEXT,
    bank_bik       VARCHAR(9),
    bank_account   VARCHAR(20),
    corr_account   VARCHAR(20),
    director_post  TEXT DEFAULT 'Генеральный директор',
    director_name  TEXT,
    accountant_name TEXT,
    phone          TEXT,
    email          TEXT,
    -- Налоговый режим. 'usn' — «НДС не облагается (УСН)», так и печатаем
    -- в счёте и акте; 'vat' — считать НДС по ставке vat_rate сверх тарифа.
    vat_mode       VARCHAR(8) NOT NULL DEFAULT 'usn' CHECK (vat_mode IN ('usn', 'vat')),
    vat_rate       NUMERIC(5,2) NOT NULL DEFAULT 0,
    updated_at     TIMESTAMP DEFAULT NOW()
);
INSERT INTO company_settings (id) VALUES (1) ON CONFLICT (id) DO NOTHING;

-- ─── 2. Реквизиты рекламодателя ─────────────────────────────────────────────
ALTER TABLE advertisers
    ADD COLUMN IF NOT EXISTS legal_name     TEXT,
    ADD COLUMN IF NOT EXISTS inn            VARCHAR(12),
    ADD COLUMN IF NOT EXISTS kpp            VARCHAR(9),
    ADD COLUMN IF NOT EXISTS legal_address  TEXT,
    ADD COLUMN IF NOT EXISTS contact_person TEXT,
    ADD COLUMN IF NOT EXISTS phone          TEXT,
    ADD COLUMN IF NOT EXISTS email          TEXT;

-- ─── 3. Договоры ────────────────────────────────────────────────────────────
-- Расчётный период задаётся договором и НЕ обязан совпадать с календарным
-- месяцем: period_days + period_anchor задают сетку («каждые 30 дней от
-- 15.03»), period_kind='month' — обычный календарный месяц.
CREATE TABLE IF NOT EXISTS contracts (
    id            SERIAL PRIMARY KEY,
    advertiser_id INTEGER NOT NULL REFERENCES advertisers(id) ON DELETE CASCADE,
    number        VARCHAR(64) NOT NULL,
    signed_on     DATE,
    valid_from    DATE,
    valid_to      DATE,
    period_kind   VARCHAR(8) NOT NULL DEFAULT 'month' CHECK (period_kind IN ('month', 'days')),
    period_days   INTEGER,              -- для period_kind='days'
    period_anchor DATE,                 -- от какой даты отсчитывать периоды
    payment_days  INTEGER NOT NULL DEFAULT 5,   -- срок оплаты счёта, календарных дней
    auto_renew    BOOLEAN NOT NULL DEFAULT FALSE,
    is_active     BOOLEAN NOT NULL DEFAULT TRUE,
    note          TEXT,
    created_at    TIMESTAMP DEFAULT NOW(),
    created_by    VARCHAR(64)
);
CREATE INDEX IF NOT EXISTS idx_contracts_adv ON contracts(advertiser_id, is_active);

-- Срок оплаты у счёта — для сводки по дебиторке («висит N дней»).
ALTER TABLE invoices
    ADD COLUMN IF NOT EXISTS due_date    DATE,
    ADD COLUMN IF NOT EXISTS contract_id INTEGER REFERENCES contracts(id) ON DELETE SET NULL;

-- ─── 4. Реестр сформированных документов ────────────────────────────────────
-- Повторная выгрузка обязана отдавать ТОТ ЖЕ файл: подписанный акт и его
-- копия не должны разойтись из-за пересчёта. Поэтому файл сохраняется на
-- диск, а sha256 фиксирует содержимое.
CREATE TABLE IF NOT EXISTS advertiser_documents (
    id            SERIAL PRIMARY KEY,
    advertiser_id INTEGER NOT NULL REFERENCES advertisers(id) ON DELETE CASCADE,
    invoice_id    INTEGER REFERENCES invoices(id) ON DELETE SET NULL,
    doc_type      VARCHAR(16) NOT NULL
                  CHECK (doc_type IN ('airtime', 'act', 'summary', 'invoice')),
    doc_format    VARCHAR(8) NOT NULL DEFAULT 'pdf' CHECK (doc_format IN ('pdf', 'xlsx')),
    period_start  DATE NOT NULL,
    period_end    DATE NOT NULL,
    number        VARCHAR(64),          -- номер документа (акт № …)
    filename      VARCHAR(256) NOT NULL,
    size_bytes    BIGINT,
    sha256        CHAR(64),
    created_by    VARCHAR(64),
    created_at    TIMESTAMP DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_adv_docs ON advertiser_documents(advertiser_id, period_start DESC);

-- ─── 5. Компенсации за недоставленные выходы ────────────────────────────────
-- Система считает недобор и ПРЕДЛАГАЕТ вариант; применяет — человек.
-- proposed_* хранит расчёт, applied_* — то, что решили на самом деле.
CREATE TABLE IF NOT EXISTS compensations (
    id             SERIAL PRIMARY KEY,
    advertiser_id  INTEGER NOT NULL REFERENCES advertisers(id) ON DELETE CASCADE,
    invoice_id     INTEGER REFERENCES invoices(id) ON DELETE SET NULL,
    period_start   DATE NOT NULL,
    period_end     DATE NOT NULL,
    reason         VARCHAR(24) NOT NULL,   -- offline | display_off | rejected | no_slots | other
    missed_plays   INTEGER NOT NULL DEFAULT 0,
    missed_minutes NUMERIC(12,1) NOT NULL DEFAULT 0,
    kind           VARCHAR(16) NOT NULL DEFAULT 'discount'
                   CHECK (kind IN ('discount', 'extra_plays')),
    proposed_amount NUMERIC(14,2) NOT NULL DEFAULT 0,
    proposed_plays  INTEGER NOT NULL DEFAULT 0,
    status         VARCHAR(16) NOT NULL DEFAULT 'proposed'
                   CHECK (status IN ('proposed', 'applied', 'declined')),
    applied_amount NUMERIC(14,2),
    applied_plays  INTEGER,
    decided_by     VARCHAR(64),
    decided_at     TIMESTAMP,
    note           TEXT,
    created_at     TIMESTAMP DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_comp_adv ON compensations(advertiser_id, period_start DESC);

-- ─── 6. Доступ самого рекламодателя ─────────────────────────────────────────
-- Роль 'advertiser' + привязка учётки к рекламодателю. ВАЖНО: ограничение
-- выборки по этому полю делается НА СЕРВЕРЕ (см. require_own_advertiser);
-- скрытие пунктов меню в панели защитой не является.
ALTER TABLE users
    ADD COLUMN IF NOT EXISTS advertiser_id INTEGER REFERENCES advertisers(id) ON DELETE CASCADE;

-- ─── 7. Заявки на размещение ────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS placement_requests (
    id            SERIAL PRIMARY KEY,
    advertiser_id INTEGER NOT NULL REFERENCES advertisers(id) ON DELETE CASCADE,
    period_start  DATE NOT NULL,
    period_end    DATE NOT NULL,
    screens       TEXT,                  -- список id экранов через запятую; пусто = вся сеть
    plays_wanted  INTEGER,               -- желаемое число выходов за период
    comment       TEXT,
    status        VARCHAR(16) NOT NULL DEFAULT 'new'
                  CHECK (status IN ('new', 'approved', 'declined', 'campaign')),
    campaign_id   INTEGER REFERENCES campaigns(id) ON DELETE SET NULL,
    decided_by    VARCHAR(64),
    decided_at    TIMESTAMP,
    decision_note TEXT,
    created_by    VARCHAR(64),
    created_at    TIMESTAMP DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_requests_status ON placement_requests(status, created_at DESC);

-- ─── 8. Токен маркировки у креатива ─────────────────────────────────────────
-- Реклама на офлайн-экранах под учёт в ЕРИР не подпадает (закон требует
-- учёта рекламы, размещённой в сети «Интернет»), никуда ничего не передаём.
-- Поле заведено на случай, если рекламодатель принесёт креатив с готовым
-- токеном или трактовка регулятора изменится — тогда не придётся менять
-- схему и переносить данные.
ALTER TABLE media
    ADD COLUMN IF NOT EXISTS erid VARCHAR(64);

-- ============================================================
-- Конец миграции 029
-- ============================================================
