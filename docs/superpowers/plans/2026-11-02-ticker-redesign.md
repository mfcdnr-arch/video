# «Бегущая строка» (срочные объявления) — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** раздел «Бегущая строка» — одна страница: «Сейчас идут» и конструктор с живым превью. В «Сейчас идут» —
что бежит, где, до какого времени и подтверждение с экранов «бежит на X из N». В конструкторе — шаблоны и срок
«до снятия / до ЧЧ:00 / на N минут». Строка надёжно держится на экранах: переживает перезагрузку, смену
плейлиста и «Стоп», несколько строк идут по очереди.

**Architecture:**
- Источник правды — сервер:
  - строки и их «где» (все / группы / экраны) хранятся в БД и разворачиваются в экраны **в момент чтения**;
  - чистая логика — `server/ticker_rules.py`, SQL — `server/ticker_store.py`, как `broadcast_rules` /
    `broadcast_store`.
- Экран получает свой список строк полем `tickers` в ленте `/schedule/minipc/{id}`. Быстрый путь — те же
  команды `ticker_show` / `ticker_hide`:
  - старый агент выполняет их по-старому;
  - новый агент 1.4.0 воспринимает любую из них как «обнови строки сейчас».
- Подтверждение: агент 1.4.0 шлёт в heartbeat `ticker_state`, сервер хранит его в `screens`, панель считает
  «бежит на X из N» с причинами.
- Истёкшие строки убирает Celery `expire_tickers`, с записью в журнал.

**Tech Stack:**
- Сервер: FastAPI + SQLAlchemy `text()` + PostgreSQL 15, Celery beat.
- Агент: Python 3.7, mpv ASS-оверлей.
- Панель: vanilla JS без сборки.
- Тесты: pytest, smoke, puppeteer.

**Spec:** `docs/superpowers/specs/2026-11-02-ticker-redesign-design.md` (согласовано 02.11.2026, d02c15a).
Макет: `Video Claude/.superpowers/brainstorm/3929-1793609095/content/ticker-ideas.html` — вариант А, вид
«как сейчас».

## Global Constraints

- **Срок.**
  - `until_at` — наивное МСК из Python (`placements_store.now_msk()`), `None` — «до снятия». `NOW()` Postgres
    для сроков не использовать.
  - «На N минут»: целое N от **1 до 1440**, может перейти через полночь.
  - «До ЧЧ:00»: `broadcast_rules.parse_until_hour` и та же проверка `on_date` («Время изменилось — проверьте
    час окончания»).
- **Текст:** 1–**200** символов после `strip()`, переводы строк заменяются пробелом.
- **Цвет и скорость.** Цвет — `#RRGGBB`; в панели 4 цвета: `#ffd34d`, `#ffffff`, `#ff6b5e`, `#7fe3c4`.
  Скорость — `slow | medium | fast`.
- **«Где»:** `all | groups | screens`. Списки id для `groups` / `screens` непустые.
- **Подтверждение:**
  - агент ≥ **1.4.0** (`CONFIRM_VERSION = (1, 4, 0)`);
  - окно «ждём подтверждения» — **90 с** от запуска или правки;
  - «в сети» — только `screen_online.online_sql`.
- **Причины, дословно:**
  - «не в сети»;
  - «старый агент — без подтверждения»;
  - «остановлен («Стоп»)»;
  - «экран выключен по расписанию питания»;
  - «ждём подтверждения»;
  - «не подтвердил — проверьте экран».
- **Журнал** (`audit_log.event_type = 'ticker'`):
  - «Бегущая строка запущена»;
  - «Бегущая строка изменена»;
  - «Бегущая строка снята»;
  - «Бегущая строка снята по сроку» (actor «система»).
- **Тексты панели, дословно:**
  - «Сейчас идут»;
  - «Новая строка»;
  - «▶ Запустить на N экранах»;
  - «Сохранить изменения»;
  - «+ сохранить как шаблон»;
  - «До снятия», «До ЧЧ:00 сегодня», «На N минут»;
  - «бежит на X из N»;
  - «на этих экранах будет N строк — каждая будет проходить реже» (больше трёх на каком-либо экране).
- **Права:** смотреть — `get_current_admin`; менять — `require_write` / `canWrite()`. Модерация 38-ФЗ к строке
  не применяется.
- **Совместимость.** Ответы API только расширяются:
  - `GET /tickers` остаётся **списком**;
  - старое тело `POST /tickers` (`is_all`, `screen_ids`, `duration` в секундах) принимается.
- **Агент.**
  - Новых файлов не создавать: OTA разрешает только 7 модулей (`routers/agent_ota.py`
    `ALLOWED_AGENT_FILES`).
  - `AGENT_VERSION = "1.4.0"`.
  - Агент 1.4.0 с сервером без поля `tickers` в ленте работает по-старому, по командам.
- **Миграция** `server/migrations/034_ticker_redesign.sql`: идемпотентная, добавлена в initdb в
  `docker-compose.yml` (`34_ticker_redesign.sql`).
- **Смоук и E2E.** Строка выводится **только на свой тестовый экран** (цель `screens`). На все экраны —
  никогда: флага нет, шаг не нужен.
- **Фронт.** `esc()` для всех серверных строк. `?v=20261102` у изменённых файлов. Экспорт в `window.Signage`.
- **Коммиты** по-русски, последней строкой `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- **Команды.**
  - pytest: `docker run --rm -v "<wt>/server":/app -v "<wt>/agent":/agent:ro -w /app ds-test:py311 python -m pytest tests -q -p no:cacheprovider`.
  - Агент: `docker run --rm -v "<wt>/agent":/a -w /a ds-test:py311 python -m pytest tests -q -p no:cacheprovider`.
  - Стенд: `bash dev_stand.sh reset` (старый `.env` стенда удалён).
  - smoke: `ADMIN_PASS=$(cat .dev_admin_pass) bash smoke_test.sh`.
  - E2E: `ADMIN_PASS=$(cat .dev_admin_pass) DS_URL=http://nginx bash ui_test.sh`.
  - После правок сервера: `docker restart ds_api ds_celery`.

## Review Focus

1. **Часы мини-ПК сбиты.**
   - Агент снимает строку по своему МСК, а панель верит его отчёту `ticker_state.ids`. «Бежит на X из N» должно
     отражать то, что сообщил экран, а не расчёт сервера.
   - Тест: Task 1 `test_reason_running_only_if_screen_reported_id`.
   - Тест: Task 5 `test_live_tickers_uses_given_now_not_server_time`.
2. **Группу или экран удалили, а строка на них нацелена.**
   - Строка не падает и показывается «на 0 экранах», её можно снять.
   - Тест: Task 1 `test_resolve_screens_ignores_missing_ids`.
   - Тест: Task 3 `test_overview_ticker_without_screens`.
3. **Спецсимволы в тексте** (`{ } \ < > "`, перевод строки). Панель экранирует, агент обезвреживает ASS.
   - Тест: Task 1 `test_message_newlines_become_spaces`.
   - Тест: Task 5 `test_marquee_text_is_ass_escaped`.
   - Task 6: проверка на стенде строкой `<b>{\\an8}"x"</b>`.
4. **Правка или снятие уже снятой (или истёкшей) строки** — 404 «Бегущая строка не найдена», ничего не записано.
   - Тест: Task 3 `test_update_missing_ticker_404`.
   - Тест: Task 2 `test_update_expired_ticker_is_lookup_error`.
5. **Старый агент и снятие одной из двух строк.** Экрану заново отправляется самая новая из оставшихся, а не
   `ticker_hide`.
   - Тест: Task 2 `test_notify_resends_newest_remaining_instead_of_hide`.

---

## Карта файлов

| Файл | Что | Задачи |
|---|---|---|
| `server/ticker_rules.py` (новый) | чистая логика: разбор тела и срока, «где» → экраны, строки экрана, причина подтверждения, сводка | 1 |
| `server/tests/test_ticker_rules.py` (новый) | тесты правил | 1 |
| `server/migrations/034_ticker_redesign.sql` (новый), `docker-compose.yml` | схема | 2 |
| `server/ticker_store.py` (новый) | SQL: строки, шаблоны, отчёты экранов, команды старым агентам, истечение | 2 |
| `server/tests/test_ticker_store.py` (новый) | тесты на фейк-БД | 2 |
| `server/routers/ticker.py` (переписать) | API строк и шаблонов, WS-будильник | 3 |
| `server/tasks.py` | Celery `expire_tickers` | 3 |
| `server/tests/test_ticker_api.py` (новый) | тесты роутера | 3 |
| `server/routers/screens.py` | лента `tickers`, heartbeat `ticker_state`, «Стоп»/«Показ» без автоснятия | 4 |
| `smoke_test.sh` | шаг «Бегущая строка» на своём экране | 3, 4 |
| `agent/ds_player.py`, `ds_sync.py`, `ds_agent.py`, `ds_heartbeat.py` | агент 1.4.0 | 5 |
| `agent/tests/test_tickers.py` (новый) | тесты агента | 5 |
| `server/static/js/views/ticker.js` (переписать), `server/static/index.html`, `server/static/css/*` | страница | 6 |
| `ui_tests/ui_test.js` | E2E раздела | 7 |
| docs (03, 04, 07, 10, 01_АРХИТЕКТУРА, README, CLAUDE.md, `img/12_ticker.png`) | документы | 7 |

---

### Task 1: Чистая логика бегущей строки

**Files:**
- Create: `server/ticker_rules.py`
- Test: `server/tests/test_ticker_rules.py`
- Modify: `server/broadcast_rules.py` — перенести сюда константу `STALE_WINDOW_TEXT` из `routers/broadcast.py`.
  `routers/broadcast.py` импортирует её отсюда, имя в роутере остаётся (его импортируют тесты).

**Interfaces:**
- Consumes: `broadcast_rules.parse_until_hour(value, now)`, `broadcast_rules.STALE_WINDOW_TEXT`.
- Produces (имена фиксированы, их используют задачи 2–4):
  - константы: `MAX_LEN = 200`, `MAX_MINUTES = 1440`, `SPEEDS`, `TARGETS = ("all", "groups", "screens")`,
    `CONFIRM_VERSION = (1, 4, 0)`, `PENDING_SECONDS = 90`;
  - `REASONS`: `"running"`, `"offline"`, `"old_agent"`, `"stopped"`, `"screen_off"`, `"pending"`,
    `"not_confirmed"`;
  - `REASON_TEXT: dict[str, str]` — тексты из Global Constraints;
  - `parse_term(term: dict | None, now: datetime) -> datetime | None` — `{"kind": "cancel"}` → None;
    `{"kind": "minutes", "minutes": N}` → `now + N мин`; `{"kind": "hour", "hour": H, "on_date": "ГГГГ-ММ-ДД"?}`
    → `parse_until_hour`. Иначе `ValueError` с русским текстом;
  - `parse_ticker_body(body: dict, now: datetime, keep_term: bool = False) -> dict` →
    `{"message", "color", "speed", "target", "group_ids", "screen_ids", "until_at", "term_given"}`:
    - понимает и старое тело: `is_all`, `screen_ids`, `duration` в секундах; 0 или нет → «до снятия»;
    - при `keep_term=True` и отсутствии `term` → `term_given=False`, `until_at=None`, и вызывающий сохраняет
      прежний срок;
  - `ticker_live(until_at: datetime | None, now: datetime) -> bool`;
  - `resolve_screens(target: str, group_ids: list[int], screen_ids: list[int], screens: list[tuple[int, int | None]]) -> list[int]` — `screens` = все экраны `(id, group_id)`; результат отсортирован, неизвестные id отброшены;
  - `tickers_for_screen(tickers: list[dict], screen_id: int, group_id: int | None, now: datetime) -> list[dict]` —
    живые строки этого экрана в порядке запуска (`started_at`, затем `id`). Каждая строка (dict) содержит
    `id, message, color, speed, target, group_ids, screen_ids, until_at, started_at`;
  - `version_tuple(v: str | None) -> tuple[int, ...]` — `"1.4.0"` → `(1, 4, 0)`; мусор и None → `()`;
  - `screen_reason(ticker_id: int, screen: dict, changed_at: datetime, now: datetime) -> str`:
    - `screen` — `{"online": bool, "agent_version": str | None, "ticker_ids": list[int] | None, "ticker_stopped": bool | None, "ticker_screen_off": bool | None}`;
    - порядок проверок: `offline` → `old_agent` (версия < CONFIRM_VERSION) → `stopped` → `screen_off` →
      `running` (id в `ticker_ids`) → `pending` (`now - changed_at < 90 с`) → `not_confirmed`;
  - `summarize(rows: list[tuple[str, str]]) -> dict` — пары `(имя экрана, причина)` →
    `{"total": N, "running": X, "problems": [{"screen": имя, "reason": код, "text": REASON_TEXT[код]}]}`.
    В `problems` — всё, что не `running`.

- [ ] **Step 1: Write the failing tests** (`N = datetime(2026, 11, 2, 14, 30)`)
  - `test_term_cancel_is_none`: `parse_term({"kind": "cancel"}, N) is None`; `parse_term(None, N) is None`.
  - `test_term_minutes_crosses_midnight`: `parse_term({"kind": "minutes", "minutes": 15}, datetime(2026, 11, 2, 23, 50)) == datetime(2026, 11, 3, 0, 5)`.
  - `test_term_minutes_bounds` (parametrize 0, 1441, -5, True, "15", 15.5) → `ValueError`, match «от 1 до 1440».
  - `test_term_hour_uses_broadcast_rules`: `{"kind": "hour", "hour": 16}` → `datetime(2026, 11, 2, 16, 0)`; `24` → `datetime(2026, 11, 3, 0, 0)`; `14` → `ValueError`.
  - `test_term_hour_stale_on_date`: `{"kind": "hour", "hour": 16, "on_date": "2026-11-01"}` → `ValueError(STALE_WINDOW_TEXT)`; `"2026-11-02"` → ok; `"02.11.2026"` → `ValueError`.
  - `test_body_new_shape`: `{"message": "  Касса  ", "color": "#ffffff", "speed": "fast", "target": "screens", "screen_ids": [3, 1], "term": {"kind": "minutes", "minutes": 30}}` → `message == "Касса"`, `screen_ids == [1, 3]`, `until_at == N + 30 мин`, `term_given is True`.
  - `test_body_legacy_shape`: `{"message": "x", "is_all": False, "screen_ids": [2], "duration": 60}` → `target == "screens"`, `until_at == N + 60 с`; `{"message": "x", "is_all": True}` → `target == "all"`, `until_at is None`.
  - `test_message_newlines_become_spaces`: `"Строка 1\nстрока 2"` → `"Строка 1 строка 2"`.
  - `test_body_rejects` (parametrize):
    - пустой текст;
    - 201 символ;
    - `color "red"`;
    - `speed "turbo"`;
    - `target "groups"` без `group_ids`;
    - `screen_ids ["a"]`.
    
    Все → `ValueError`, текст по-русски.
  - `test_keep_term_when_absent`: `parse_ticker_body({"message": "x", "target": "all"}, N, keep_term=True)["term_given"] is False`.
  - `test_ticker_live_until_exclusive`: `ticker_live(N, N) is False`; `ticker_live(None, N) is True`.
  - `test_resolve_screens` и `test_resolve_screens_ignores_missing_ids`:
    - `screens = [(1, 10), (2, 10), (3, None)]`;
    - `"all"` → `[1, 2, 3]`;
    - `"groups", [10]` → `[1, 2]`;
    - `"screens", [3, 99]` → `[3]`;
    - `"groups", [77]` → `[]`.
  - `test_tickers_for_screen_order_and_expiry`:
    - две живые строки на экран 1, запущенные в 10:00 и 09:00, и одна истёкшая;
    - результат — `[09:00, 10:00]`, без истёкшей;
    - строка группы 10 попадает экрану 1 с `group_id = 10`.
  - `test_reason_order` (parametrize):
    - offline при `ticker_ids` с id → `"offline"`;
    - `agent_version "1.3.0"` в сети → `"old_agent"`;
    - `"1.4.0"` и `stopped` → `"stopped"`;
    - `screen_off` → `"screen_off"`.
  - `test_reason_running_only_if_screen_reported_id`: `"1.4.0"`, в сети, `ticker_ids = [5]` →
    `screen_reason(5, ...) == "running"`, `screen_reason(6, ...)` при `changed_at = N - 30 с` → `"pending"`, при
    `N - 120 с` → `"not_confirmed"`.
  - `test_version_tuple`: `"1.10.0" > "1.4.0"` по кортежу; `None` → `()`; `"abc"` → `()`.
  - `test_summarize`:
    - вход `[("A", "running"), ("B", "offline"), ("C", "running")]`;
    - результат: `total == 3`, `running == 2`;
    - `problems == [{"screen": "B", "reason": "offline", "text": "не в сети"}]`.

- [ ] **Step 2: Run** `... -k ticker_rules` → FAIL (`ModuleNotFoundError: ticker_rules`).
- [ ] **Step 3: Implement** `server/ticker_rules.py`. Только stdlib и `broadcast_rules`. Docstring модуля — по образцу
  `broadcast_rules.py`: что такое МСК, «до снятия».
- [ ] **Step 4: Run** `... -k "ticker_rules or broadcast"` → PASS. Тесты эфира не сломались из-за переноса
  `STALE_WINDOW_TEXT`.
- [ ] **Step 5: Commit** «Бегущая строка: правила — срок, «где», строки экрана, подтверждение».

---

### Task 2: Схема и SQL бегущей строки

**Files:**
- Create: `server/migrations/034_ticker_redesign.sql`
- Modify: `docker-compose.yml` — строка initdb `./server/migrations/034_ticker_redesign.sql:/docker-entrypoint-initdb.d/34_ticker_redesign.sql:ro`.
- Create: `server/ticker_store.py`
- Test: `server/tests/test_ticker_store.py` — `RoutedDB` и `_Result` импортировать из `test_broadcast`, не копировать.

**Interfaces:**
- Consumes: всё из Task 1; `screen_online.online_sql`; `placements_store.now_msk`.
- Produces:
  - константы: `AUDIT_TYPE = "ticker"`, `TITLE_ON`, `TITLE_CHANGED`, `TITLE_OFF`, `TITLE_EXPIRED` (тексты из
    Global Constraints), `SYSTEM_ACTOR = "система"`;
  - `list_live(db, now) -> list[dict]` — живые строки (`until_at IS NULL OR until_at > :now`) с `group_ids` и
    `screen_ids` (массивы). Поля: `id, message, color, speed, target, group_ids, screen_ids, until_at,
    started_at, started_by, changed_at, changed_by`;
  - `screens_brief(db) -> list[dict]` — все экраны:
    `id, name, group_id, group_name, online, offline_for_seconds, agent_version, ticker_ids, ticker_stopped,
    ticker_screen_off`. «В сети» — `online_sql("s")` / `offline_for_sql("s")`;
  - `for_screen(db, screen_id, now) -> list[dict]` — для ленты: `[{"id", "message", "color", "speed",
    "until_at": ISO | None}]`, порядок `tickers_for_screen`;
  - `create(db, data: dict, actor: str, now) -> int` — `started_at = changed_at = now`,
    `started_by = changed_by = actor`;
  - `update(db, ticker_id, data: dict, actor, now) -> None` — `term_given=False` сохраняет прежний `until_at`.
    Нет или истекла → `LookupError("Бегущая строка не найдена")`;
  - `remove(db, ticker_id, actor, now) -> None` — `LookupError`, если нет;
  - `expire_due(db, now) -> int` — `DELETE … WHERE until_at <= :now RETURNING id, message`, по одной записи
    «снята по сроку» на строку;
  - `record_report(db, screen_id: int, state: dict | None) -> None` — `None` → ничего не делать, это старый агент;
  - `notify_screens(db, before: set[int], after: set[int], now) -> list[int]`:
    - в `commands`: каждому экрану из `after` — `ticker_show` самой новой живой строки этого экрана
      (`payload: message, color, speed, duration` = оставшиеся секунды или 0);
    - экранам из `before − after` — `ticker_show` самой новой оставшейся или `ticker_hide`, если строк не осталось;
    - возвращает затронутые id для WS-будильника;
  - `targets_of(db, ticker_id) -> set[int]` — экраны строки сейчас;
  - шаблоны:
    - `list_templates(db) -> list[dict]`;
    - `create_template(db, data, actor) -> int`;
    - `update_template(db, tid, data) -> None` (LookupError);
    - `delete_template(db, tid) -> None` (LookupError).
    
    `data` шаблона: `{"message", "color", "speed", "term_kind": "cancel" | "minutes" | "hour", "term_value": int | None}`.
- Функции не делают `commit`.

**Миграция 034** (идемпотентная):
- `tickers`: колонки `until_at TIMESTAMP`, `target VARCHAR(8) NOT NULL DEFAULT 'all'`, `started_at TIMESTAMP`,
  `started_by VARCHAR(64)`, `changed_at TIMESTAMP`, `changed_by VARCHAR(64)`.
- Существующие строки:
  - удалить неактивные и истёкшие (`expires_at <= NOW()`);
  - остальным проставить:
    - `target` из `is_all` (`'all'` / `'screens'`);
    - `until_at = (expires_at AT TIME ZONE 'UTC') AT TIME ZONE 'Europe/Moscow'`, где `expires_at` не NULL и
      `until_at` ещё NULL;
    - `started_at` — так же из `created_at`;
    - `started_by = created_by`.
- `ticker_groups(ticker_id → tickers ON DELETE CASCADE, group_id → sync_groups ON DELETE CASCADE, PK)`.
- `ticker_templates(id SERIAL, message TEXT NOT NULL, color VARCHAR(9) NOT NULL DEFAULT '#ffd34d', speed VARCHAR(16) NOT NULL DEFAULT 'medium', term_kind VARCHAR(8) NOT NULL DEFAULT 'cancel' CHECK (term_kind IN ('cancel','minutes','hour')), term_value INTEGER, created_by VARCHAR(64), created_at TIMESTAMP DEFAULT NOW())`.
  Три стартовых шаблона вставлять, только если таблица пуста:
  - «Магазин закрывается через 15 минут. Спасибо за покупки!» — `#ffd34d`, `medium`, `minutes`, 15;
  - «Технический перерыв. Приносим извинения за неудобства» — `#ff6b5e`, `medium`, `cancel`;
  - «Касса временно не работает. Пожалуйста, пройдите к соседней кассе» — `#ffffff`, `medium`, `minutes`, 30.
- `screens`: `ticker_ids INTEGER[]`, `ticker_stopped BOOLEAN`, `ticker_screen_off BOOLEAN`.

- [ ] **Step 1: Write the failing tests** (`RoutedDB`, `N = datetime(2026, 11, 2, 14, 30)`)
  - `test_create_writes_msk_times_and_journals`:
    - INSERT в `tickers` с `started_at == N`, `until_at` из `data`;
    - `ticker_screens` для `screens`, `ticker_groups` для `groups`;
    - журнал `("Бегущая строка запущена", "<текст>", "admin")`;
    - `db.commits == 0`.
  - `test_update_keeps_until_when_term_not_given` / `test_update_new_term_from_save_moment`:
    - params UPDATE: `until_at` прежний / новый;
    - `changed_at == N`;
    - журнал «изменена»;
    - связи `ticker_screens` / `ticker_groups` пересобраны.
  - `test_update_expired_ticker_is_lookup_error`:
    - строка с `until_at = N - 1 мин` → `LookupError`;
    - ни UPDATE, ни журнала.
  - `test_remove_deletes_and_journals` / `test_remove_missing_is_lookup_error`.
  - `test_expire_due_journals_each_once`:
    - маршрут `DELETE FROM tickers` возвращает 2 строки → 2 записи «снята по сроку» от «система»;
    - функция возвращает 2.
  - `test_list_live_filters_by_python_now`: в SQL нет `NOW()`, параметр `now == N`.
  - `test_for_screen_shape`:
    - поля `id, message, color, speed, until_at`;
    - `until_at` — ISO без пояса или None;
    - порядок по `started_at`.
  - `test_record_report_none_is_noop` / `test_record_report_writes_columns`:
    - `{"ids": [5], "stopped": False, "screen_off": False}`;
    - UPDATE `screens` c `ticker_ids = [5]`.
  - `test_notify_show_new_targets`:
    - `before = set()`, `after = {1, 2}` → два `ticker_show`;
    - `duration` = оставшиеся секунды: срок N+15 мин → 900.
  - `test_notify_resends_newest_remaining_instead_of_hide`:
    - экран 1 из `before − after`, у него осталась другая живая строка;
    - → `ticker_show` с её текстом, не `ticker_hide`.
  - `test_notify_hide_when_nothing_left` → `ticker_hide` с `{}`.
  - `test_templates_crud_and_lookup_errors`.
- [ ] **Step 2: Run** `... -k ticker_store` → FAIL.
- [ ] **Step 3: Implement** `ticker_store.py` (docstring по образцу `broadcast_store.py`) и миграцию.
  Список в `docker-compose.yml`.
- [ ] **Step 4: Run** весь pytest → PASS. На стенде:
  - `bash dev_stand.sh reset`;
  - `bash migrate.sh` → «применено 0, уже было 34» после initdb;
  - `docker exec -i ds_postgres psql … -c "SELECT count(*) FROM ticker_templates"` → 3.
- [ ] **Step 5: Commit** «Бегущая строка: схема (миграция 034) и SQL — сроки, группы, шаблоны, отчёты экранов».

---

### Task 3: API строк и шаблонов, истечение по сроку

**Files:**
- Modify (переписать): `server/routers/ticker.py`. Удалить `auto_clear_tickers` — его вызовы убирает Task 4.
  Пока функцию оставить как обёртку `return 0` с пометкой «удаляется в Task 4», чтобы `screens.py` не падал между
  коммитами.
- Modify: `server/tasks.py` — задача `expire_tickers`, `beat_schedule["expire-tickers"] = {"task": "expire_tickers", "schedule": 60.0}`
  (образец `expire_broadcast`).
- Test: `server/tests/test_ticker_api.py`
- Modify: `smoke_test.sh` — шаг «Бегущая строка: запуск, правка, снятие (только тестовый экран)» перед «Уборкой».

**Interfaces:**
- Consumes: Tasks 1–2; `ws_manager` как в `/command/stop`.
- Produces (их использует Task 6):
  - `GET /tickers` → **список**, как раньше (`id, message, color, speed, is_all`), плюс `target, until_at,
    started_at, started_by`.
  - `GET /tickers/overview` → `{"generated_at": ISO МСК, "tickers": [...], "screens": [...], "groups": [...],
    "templates": [...]}`:
    - строка в `tickers`: поля `list_live`, плюс `screens_total`, `confirm` (результат `summarize`),
      `remaining_seconds` (None для «до снятия»);
    - `screens`: поля `screens_brief` плюс `live_count` — сколько живых строк у экрана;
    - `groups`: `[{"id", "name", "screen_count"}]`.
  - `POST /tickers` → `{"id", "status": "ok"}`. Тело — новое или старое. `ValueError` → 400 с текстом.
  - `PUT /tickers/{id}` → `{"status": "ok"}`. 404 «Бегущая строка не найдена».
  - `DELETE /tickers/{id}` → `{"status": "ok"}`, 404 так же.
  - `GET|POST /ticker-templates`, `PUT|DELETE /ticker-templates/{id}`. Тело шаблона:
    `{"message", "color", "speed", "term": {"kind", "minutes" | "hour"}}`. Проверки — как у строки; `on_date`
    у шаблона не хранится.
  - Каждый POST/PUT/DELETE строки:
    - `before = targets_of` (для PUT/DELETE);
    - изменение;
    - `after`;
    - `notify_screens(before, after)`;
    - `commit`;
    - WS `new_command` подключённым экранам.
  - Чистые функции роутера для тестов: `overview_json(live, screens, groups, templates, now) -> dict`,
    `ticker_json(row, now) -> dict`.
  - В `screen_reason` момент правки — `changed_at or started_at`. У строк, перенесённых миграцией без обоих
    полей, — `now - 1 день`, то есть сразу «не подтвердил», а не вечное «ждём».

- [ ] **Step 1: Write the failing tests**
  - `test_overview_counts_and_reasons`:
    - 3 экрана: в сети 1.4.0 с id в `ticker_ids`, не в сети, 1.3.0;
    - `confirm == {"total": 3, "running": 1, "problems": [не в сети, старый агент]}`.
  - `test_overview_ticker_without_screens` — строка на удалённую группу: `screens_total == 0`, без ошибок.
  - `test_overview_live_count_per_screen` — 4 строки на экране 1 → `live_count == 4`.
  - `test_remaining_seconds` — `until_at = N + 15 мин` → 900; «до снятия» → None.
  - `test_update_missing_ticker_404` / `test_delete_missing_ticker_404`: `HTTPException.status_code == 404`,
    `detail == "Бегущая строка не найдена"`.
  - `test_post_bad_body_400_russian` (parametrize: пустой текст, 201 символ, `minutes 0`, `hour` в прошлом) →
    400, `detail` по-русски.
  - `test_get_tickers_stays_a_list` — `GET /tickers` отдаёт list с ключами `id, message, color, speed, is_all`.
  - `test_expire_tickers_task_commits_once` — задача вызывает `ticker_store.expire_due(db, now_msk())` и
    один `commit`.
- [ ] **Step 2: Run** `... -k ticker_api` → FAIL.
- [ ] **Step 3: Implement** роутер, задачу Celery и шаг smoke:
  - экран SMOKE шлёт heartbeat с `agent_version "1.4.0"` и
    `ticker_state {"ids": [<id>], "stopped": false, "screen_off": false}`;
  - `POST /tickers` (`target screens [SID]`, `term minutes 15`) → 200;
  - overview: строка есть, `screens_total == 1`, `confirm.running == 1`;
  - `PUT` текста → в журнале (`/dashboard` feed или `/audit`) «Бегущая строка изменена»;
  - `DELETE` → строки нет;
  - старое тело (`is_all false`, `screen_ids [SID]`, `duration 60`) → `until_at` ≈ +60 с, снять;
  - шаблон: создать, найти, удалить;
  - в «Уборке» снять строки SMOKE, если остались.
- [ ] **Step 4: Run** pytest → PASS. `docker restart ds_api ds_celery`. smoke без флагов → «провалено 0», число
  записать в отчёт. `docker logs ds_celery | grep expire_tickers` — задача зарегистрирована.
- [ ] **Step 5: Commit** «Бегущая строка: API строк и шаблонов, подтверждение экранов, снятие по сроку».

---

### Task 4: Лента экрана, heartbeat, «Стоп» без автоснятия

**Files:**
- Modify: `server/routers/screens.py`:
  - лента `/schedule/minipc/{id}`: добавить `"tickers": ticker_store.for_screen(db, screen_id, now)`. Ошибка
    SQL строк — `[]` и `rollback`, остальная лента не падает (образец — эфир в `/files/list`);
  - удалить блок автоснятия при смене плейлиста (~650: `last_playlist_id` можно продолжать обновлять,
    `auto_clear_tickers` не вызывать);
  - `/command/stop/{id}`: убрать `auto_clear_tickers`; экрану `ticker_hide`;
  - `/command/resume/{id}`: экрану `ticker_show` самой новой его живой строки, если есть (через
    `notify_screens(db, set(), {id}, now)`);
  - heartbeat: `ticker_store.record_report(db, screen_id, body.get("ticker_state") if isinstance(..., dict) else None)`.
- Modify: `server/routers/ticker.py` — удалить заглушку `auto_clear_tickers`.
- Test: `server/tests/test_ticker_api.py` (дополнить)
- Modify: `smoke_test.sh` — в шаге «Бегущая строка»: лента экрана SMOKE содержит строку; `/command/stop/SID` →
  строка осталась в overview, в `commands` экрана есть `ticker_hide`; resume → `ticker_show`.

**Interfaces:**
- Consumes: `ticker_store.for_screen`, `record_report`, `notify_screens`.
- Produces: поле ленты `tickers: [{"id", "message", "color", "speed", "until_at"}]` (его читает агент, Task 5);
  heartbeat принимает `ticker_state: {"ids": [int], "stopped": bool, "screen_off": bool}`.

- [ ] **Step 1: Write the failing tests**
  - `test_feed_has_tickers` — лента экрана с одной живой строкой → `tickers` с `id, message, color, speed,
    until_at`.
  - `test_feed_tickers_failure_is_empty_list_not_500` — `fail_on="FROM tickers"` → `tickers == []`, остальная
    лента есть.
  - `test_playlist_change_does_not_touch_tickers` — смена `last_playlist_id` → ни одного `DELETE FROM tickers`.
  - `test_stop_keeps_tickers_and_hides_on_that_screen` — нет `DELETE FROM tickers`, есть `commands` `ticker_hide`
    для этого экрана.
  - `test_heartbeat_records_ticker_state` / `test_heartbeat_without_ticker_state_leaves_columns` — у старого агента
    поле не трогается.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** pytest и smoke → PASS, «провалено 0».
- [ ] **Step 5: Commit** «Бегущая строка в ленте экрана и heartbeat; «Стоп» и смена плейлиста её больше не снимают».

---

### Task 5: Агент 1.4.0 — очередь строк, подтверждение

**Files:**
- Modify: `agent/ds_player.py`:
  - модульная `live_tickers(items: list, now: datetime) -> list` — живые строки по `until_at` (наивное МСК),
    порядок сохраняется, битые элементы пропускаются;
  - методы `VLCPlayer`:
    - `set_tickers(items: list) -> None` — заменить очередь; идемпотентно для того же списка;
    - `set_tickers_paused(paused: bool, stopped: bool, screen_off: bool) -> None`;
    - `ticker_state() -> dict` — `{"ids": [...живые показываемые id], "stopped": bool, "screen_off": bool}`;
      на паузе `ids = []`;
  - поток очереди: по одному полному проходу каждой живой строки по кругу, её цветом и скоростью (`_marquee_loop`
    — та же отрисовка, вид без изменений):
    - снятая или изменённая строка исчезает или меняется не позже ~1 с;
    - истёкшая по часам агента выпадает;
    - пустая очередь — оверлей убран;
  - `set_marquee` / `clear_marquee` остаются для старого режима.
- Modify: `agent/ds_sync.py` — `ticker_show` / `ticker_hide`:
  - если `self.player.tickers_from_feed` — `self.refresh_requested = True` и ack;
  - иначе — как сейчас (`set_marquee` / `clear_marquee`).
- Modify: `agent/ds_agent.py`:
  - после каждой загрузки ленты (сеть или кеш): если в ней есть ключ `tickers` —
    `player.tickers_from_feed = True; player.set_tickers(schedule["tickers"])`;
  - если `sync_hdl.refresh_requested` — сбросить флаг и загрузить ленту в этом же проходе цикла;
  - каждый проход: `player.set_tickers_paused(player.user_stopped or not display_on, player.user_stopped, not display_on)`.
- Modify: `agent/ds_heartbeat.py` — `send(..., ticker_state: dict | None = None)` кладёт поле в тело, если не None;
  `AGENT_VERSION = "1.4.0"`. `ds_agent.py` передаёт `player.ticker_state()`.
- Test: `agent/tests/test_tickers.py`

- [ ] **Step 1: Write the failing tests**
  - `test_live_tickers_drops_expired_keeps_order`:
    - три строки: `until_at` прошёл, None, будущее;
    - результат — две последние в исходном порядке.
  - `test_live_tickers_uses_given_now_not_server_time` — одна и та же строка жива при `now = 14:59`, мертва при
    `15:00` (`until_at = 15:00`).
  - `test_live_tickers_skips_garbage` — элементы без `id` или с неразборным `until_at` пропускаются, исключения нет.
  - `test_ticker_state_paused_reports_no_ids` — после `set_tickers([...])` и
    `set_tickers_paused(True, True, False)` → `{"ids": [], "stopped": True, "screen_off": False}`.
  - `test_ticker_state_running_reports_live_ids` — с подменённым временем.
  - `test_sync_ticker_show_requests_refresh_in_feed_mode` — фейк-плеер с `tickers_from_feed = True` → флаг
    поднят, `set_marquee` не вызван, ack отправлен.
  - `test_sync_ticker_show_legacy_without_feed` — `tickers_from_feed = False` → `set_marquee(message, color, speed, duration)`.
  - `test_marquee_text_is_ass_escaped` — `_ass_escape("{\\an8}x")` не содержит `{`, `}`, `\`.
  - `test_heartbeat_sends_ticker_state` — тело запроса содержит `ticker_state`; с `None` поля нет;
    `agent_version == "1.4.0"`.
  
  Поток mpv в тестах не запускать — проверять чистые функции и состояние.
- [ ] **Step 2: Run** агентские тесты → FAIL.
- [ ] **Step 3: Implement.** Совместимость с Python 3.7: без `|` в аннотациях времени выполнения, без
  walrus-оператора внутри lambda.
- [ ] **Step 4: Run** агентские тесты → PASS; `python3 -m py_compile agent/*.py`. Серверный pytest с `/agent` →
  PASS.
- [ ] **Step 5: Commit** «Агент 1.4.0: очередь бегущих строк из ленты, пауза на «Стоп», подтверждение в heartbeat».

---

### Task 6: Страница «Бегущая строка»

**Files:**
- Modify (переписать): `server/static/js/views/ticker.js`
- Modify: `server/static/index.html` — `ticker.js?v=20261102`; стили — в существующем CSS панели, где лежат
  `.tickprev` / `.tickbar` / `.tickrun`.

**Interfaces:**
- Consumes: `GET /tickers/overview`, `POST /tickers`, `PUT /tickers/{id}`, `DELETE /tickers/{id}`,
  `/ticker-templates*` (Task 3); `api`, `esc`, `toast`, `canWrite`, `fmtOfflineFor`.
- Produces — id и `data-action` для E2E (Task 7):
  - `#tk-live` — «Сейчас идут»;
  - карточка `[data-ticker-id]` со строкой «бежит на X из N» и кнопкой подробностей
    `data-action="ticker-details"`;
  - кнопки `ticker-edit`, `ticker-stop`;
  - конструктор `#tk-form`:
    - `#tk-msg` и `#tk-count` («N / 200»);
    - чипы `ticker-template` `[data-template-id]`, `ticker-save-template`, `ticker-template-delete`;
    - `ticker-pick-color` `[data-color]`;
    - `ticker-speed` `[data-speed]`;
    - `ticker-target` `[data-target=all|groups|screens]`, `ticker-toggle-group` `[data-group-id]`,
      `ticker-toggle-screen` `[data-screen-id]`;
    - `ticker-term` `[data-term=cancel|hour|minutes]`, `#tk-hour`, `ticker-minutes` `[data-min=15|30|60]`,
      `#tk-minutes`;
    - `#tk-warn`;
    - `ticker-start` («▶ Запустить на N экранах»);
    - в режиме правки — `ticker-save` («Сохранить изменения») и `ticker-cancel-edit` («Отмена»);
  - превью `#tk-prev` (CSS-анимация; скорости 80 / 140 / 220 px/с относительно кадра 1280, масштаб под ширину
    превью).

Поведение:
- **«Сейчас идут».** Строки из `overview.tickers` в порядке запуска. У карточки:
  - цвет и текст;
  - «где»: «на всех N экранах», «группа «…» (N экранов)» или «N экранов: …»;
  - срок: «до 21:00, осталось 14 мин» / «до снятия» / «ещё 25 мин»;
  - «запустил <кто> в ЧЧ:ММ»;
  - «бежит на X из N». По `ticker-details` раскрывается список `problems` (экран — текст причины).
  
  Нет строк — «Сейчас ничего не идёт».
- **Шаблон.** Клик заполняет текст, цвет, скорость и срок. Если час шаблона `hour` уже не позже текущего часа МСК,
  час не выбран, `#tk-hour` подсвечен, «Запустить» неактивна до выбора.
- **Часы.** Час «сейчас» и список часов — от `overview.generated_at` плюс прошедшее время: свой маленький «часовой»
  помощник в `ticker.js`, а не часы браузера. При отправке «до ЧЧ:00» в теле `on_date`. При смене часа или даты —
  тот же отказ, что в окне эфира, ничего не отправлено.
- **Счётчик экранов.** «Запустить на N экранах» — N по выбранному «где» из `overview.screens`. При 0 кнопка
  неактивна.
- **Список экранов и групп.** Экраны не в сети показаны с «не в сети X мин» (`fmtOfflineFor`), выбрать их можно.
  У группы — число экранов.
- **Предупреждение `#tk-warn`.** Если у какого-либо выбранного экрана `live_count + 1 > 3` (в правке — без самой
  строки) — текст из Global Constraints.
- **«Изменить».** Конструктор переходит в режим правки этой строки. Если срок не трогали, в теле нет `term` —
  сервер сохранит прежний.
- **«Снять».** `confirm('Снять бегущую строку?')`.
- **Обновление** каждые **15 с**, пока вкладка видима; не во время отправки. Открытая правка, введённый текст и
  раскрытые подробности не сбрасываются.
- **Права.** Без `canWrite()` конструктор и кнопки скрыты, «Сейчас идут» видно.

- [ ] **Step 1: Implement** (JS-юнит-тестов в проекте нет — проверка шагами 2–3).
- [ ] **Step 2: Verify** `node --check server/static/js/views/ticker.js` → без ошибок.
- [ ] **Step 3: Verify на стенде** (`docker restart ds_api`; браузер на `http://127.0.0.1:8088`):
  - тестовый экран с heartbeat `agent_version 1.4.0` (curl с его токеном);
  - запуск из шаблона на «Экраны» → карточка «ждём подтверждения»;
  - heartbeat с `ticker_state` → «бежит на 1 из 1»;
  - «Изменить» текст → сохранено;
  - строка `<b>{\an8}"x"</b>` отображается как текст;
  - четвёртая строка на экран → предупреждение;
  - «Снять» → карточка ушла;
  - сохранить шаблон → чип появился → удалить.
  
  Снять за собой всё созданное.
- [ ] **Step 4: Commit** «Бегущая строка: страница — «Сейчас идут» с подтверждением экранов, конструктор, шаблоны».

---

### Task 7: E2E и документы

**Files:**
- Modify: `ui_tests/ui_test.js`:
  - цикл «Бегущая строка» **только на фикстурном экране** `E2E_стенд_*`:
    - шаблон → «Экраны» → свой экран → «на N минут» → запустить;
    - heartbeat фикстуры с `agent_version 1.4.0` и `ticker_state` → «бежит на 1 из 1»;
    - изменить текст → снять;
    - сохранить шаблон → удалить;
  - уборка в `finally`;
  - в `SAFE_ACTIONS` слепого прохода — только `ticker-details`, `ticker-pick-color`, `ticker-speed`,
    `ticker-term`, `ticker-target` (не `ticker-start` / `ticker-save` / `ticker-stop` / `ticker-template-delete`).
- Modify docs:
  - `docs/Инструкции/03_РОЛЬ_АДМИНИСТРАТОР.md` §«Бегущая строка» — заново по новой странице, снять
    `img/12_ticker.png`;
  - `docs/Техдокументация/04_ТЕСТЫ.md` — точные числа pytest (с `/agent` и без), агент, smoke, E2E;
  - `docs/Инструкции/07_*` — агент 1.4.0, изменённые модули `ds_player.py`, `ds_sync.py`, `ds_agent.py`,
    `ds_heartbeat.py`; для агентов < 1.2.0 — все 7;
  - `docs/Инструкции/10_ПЛАН_ПРИЁМКИ_ФУНКЦИЙ.md` — пункты приёмки §8 спеки;
  - `docs/Техдокументация/01_АРХИТЕКТУРА.md` и `README.md` — модули, миграции `001…034`, Celery `expire_tickers`,
    новые эндпоинты;
  - `CLAUDE.md` — только стабильное: строки `ticker_rules.py` / `ticker_store.py` в «Структуре», `001`…`034`,
    Celery «завершение бегущих строк по сроку».
- [ ] **Step 1: Implement** E2E.
- [ ] **Step 2: Run** E2E на стенде → «провалено 0»; с `E2E_ALLOW_BROADCAST=1` → «провалено 0». Записать оба числа.
- [ ] **Step 3: Update docs** с точными числами из прогонов (pytest с `/agent` и без, агент, smoke, E2E).
- [ ] **Step 4: Commit** «E2E и документы: «Бегущая строка» — срочные объявления».
