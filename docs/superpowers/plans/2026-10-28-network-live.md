# «Эфир сети» — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** страница «Эфир сети» показывает, что сейчас идёт на каждом экране, сведения о плейлисте с правкой поверх страницы, общий эфир включается через окно с выбором «до отмены» / «до ЧЧ:00», единое правило «в сети» во всей панели.

**Architecture:** чистая логика «что идёт сейчас / до / дальше» — новый модуль `server/network_live.py` поверх существующей `placements_logic.week_cells` (одна логика приоритетов с «Расписанием → По датам»). Общий эфир: срок `until_at` в `network_broadcast`, правила — `server/broadcast_rules.py` (чисто), SQL — `server/broadcast_store.py`; эфир «истекает» при чтении и фоновой задачей Celery (запись в журнале). «В сети» считается в SQL одним фрагментом (`server/screen_online.py`), потому что `screens.last_seen` пишется в UTC, а показы и эфир живут в наивном МСК — правила времени не смешиваются. Фронт — новая вью `js/views/network-live.js` вместо `broadcast.js`.

**Tech Stack:** FastAPI + SQLAlchemy `text()` + PostgreSQL 15; Celery beat; vanilla JS (без сборки), `modal.js`/`playlist-editor.js`/`placement-editor.js`/`media-preview.js` этапа 1; pytest; puppeteer.

**Spec:** `docs/superpowers/specs/2026-10-28-network-live-design.md` (согласовано 28.10.2026). Макеты: `Video Claude/.superpowers/brainstorm/76053-1793192646/content/live-network.html` (вариант A), `live-network-v2.html` (полоса 1, окно включения, правка А).

## Global Constraints

- «В сети» = `last_seen` не старше **5 минут** (одна константа `ONLINE_MINUTES = 5`); экран без `last_seen` — «ни разу не выходил на связь». Сравнение — только в SQL с `NOW()` (оба в UTC).
- Время эфира и показов — наивное МСК (`placements_store.now_msk()`), как в этапе 1.
- «До ЧЧ:00 сегодня»: целый час, строго позже текущего часа МСК, не позже 24 (24 = ближайшая полночь).
- Эфир перекрывает всё, но на экране, где у его плейлиста нет проигрываемых роликов, не применяется (как `pick_active` у показов).
- Обновление страницы — каждые **30 с**, пока вкладка видима; открытые панель/окна/поиск не сбрасываются.
- Тексты (дословно из спеки): «Общий эфир выключен · Каждый экран показывает своё расписание», «Включить общий эфир…», «Общий эфир: «<плейлист>» на всех N экранах», «Сменить плейлист», «Выключить», «До отмены — выключу сам», «До ЧЧ:00 сегодня, затем экраны вернутся к расписанию», «Включить на N экранах», «до конца дня без изменений», «ни разу не выходил на связь», «📡 получит при подключении», «на K экранах нечего играть — там идёт обычное расписание», «Плейлист сейчас идёт на N экранах — изменения дойдут до них в течение минуты».
- Журнал операций: «Общий эфир включён», «Общий эфир сменён», «Общий эфир выключен», «Общий эфир завершён по времени».
- Права: смотреть — `get_current_admin`; менять — `require_write` / `canWrite()`.
- Новая миграция `server/migrations/032_broadcast_until.sql` — идемпотентная, в initdb `docker-compose.yml`.
- Фронт: vanilla JS, `esc()` для всех серверных значений, `?v=20261028` у новых/изменённых файлов, экспорт в `window.Signage`.
- Коммиты по-русски, последней строкой `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Тесты: сервер `docker run --rm -v "<repo>/server":/app -w /app ds-test:py311 python -m pytest tests -q -p no:cacheprovider` (образ `ds-test:py311` собран локально; база — 307 passed, 1 skipped). Стенд: `bash dev_stand.sh up` (127.0.0.1:8088), smoke `ADMIN_PASS=$(cat .dev_admin_pass) bash smoke_test.sh`, E2E `ADMIN_PASS=$(cat .dev_admin_pass) DS_URL=http://nginx bash ui_test.sh`.

## Review Focus

1. **Эфир «до 15:00» и часовые пояса** — в 14:59 МСК эфир идёт, в 15:00 МСК уже нет, хотя контейнер в UTC. Тест: Task 2 `test_effective_on_expires_exactly_at_until_msk`.
2. **Плейлист эфира удалили** (`playlist_id` → NULL по FK) — эфир должен считаться выключенным, страница и лента не падают. Тест: Task 2 `test_effective_on_false_when_playlist_deleted`.
3. **Автообновление посреди работы** — открытая панель, поиск и окно правки не сбрасываются каждые 30 с. Проверка: Task 6 Step 2 (браузерный сценарий «открыть панель, подождать 35 с»).
4. **Экран, где эфиру нечего играть** (алкоголь на площадке без лицензии) — таблица показывает обычное расписание, полоса — «на K экранах нечего играть». Тест: Task 4 `test_screen_live_broadcast_unplayable_falls_to_schedule`, Task 5 счётчик в ответе.
5. **Экран без единого сигнала и экран, пропавший вчера** — в «Экранах», на дашборде и в «Эфире сети» одинаково «не в сети». Проверка: Task 1 smoke (новый экран — `online == false`, после heartbeat — `true`).

---

## Карта файлов

| Файл | Что | Задачи |
|---|---|---|
| `server/screen_online.py` | новый: `ONLINE_MINUTES`, SQL-фрагменты «в сети» | 1 |
| `server/routers/screens.py` | `/minipc` + поля `online`, `offline_for_seconds`; лента — эфир по новым правилам | 1, 3 |
| `server/routers/system.py` | дашборд: `online` в «Сейчас в эфире» и «Диски», эфир с учётом срока | 1, 3 |
| `server/static/js/core/helpers.js` | `fmtOfflineFor(seconds)` | 1 |
| `server/static/js/views/screens.js`, `dashboard.js`, `ota.js`, `ticker.js`, `placement-editor.js` | `s.online` вместо `s.status==='online'` | 1 |
| `server/migrations/032_broadcast_until.sql`, `docker-compose.yml` | срок эфира | 2 |
| `server/broadcast_rules.py` | новый, чистый: срок, «действует ли» | 2 |
| `server/broadcast_store.py` | новый: чтение/включение/выключение/истечение эфира | 2 |
| `server/routers/broadcast.py` | API эфира с телом `{playlist_id, until_hour}` | 2 |
| `server/tasks.py` | задача Celery `expire_broadcast` раз в минуту | 2 |
| `server/placements_logic.py` | `active_until_for` для эфира; `week_cells(..., playable=None)` | 3, 4 |
| `server/routers/media.py` | `/files/list` + плейлист эфира | 3 |
| `server/routers/schedule.py` | симулятор: эфир со сроком | 3 |
| `server/network_live.py` | новый, чистый: `screen_live()` | 4 |
| `server/routers/network.py`, `server/main.py` | новый API: `/network/live`, `/network/playlist/{id}/usage` | 5 |
| `server/static/js/views/network-live.js` | новый: страница, таблица, панель, правка | 6 |
| `server/static/js/views/playlist-editor.js` | опция `notice` в окне правки | 6 |
| `server/static/js/views/broadcast-window.js` | новый: полоса эфира и окно включения | 7 |
| `server/static/js/core/layout.js`, `index.html`, `css/style.css` | подключение вью, стили | 6, 7 |
| `server/static/js/views/broadcast.js` | удалить (заменён) | 6 |
| `smoke_test.sh`, `ui_tests/ui_test.js`, `docs/Инструкции/03_РОЛЬ_АДМИНИСТРАТОР.md`, `docs/Техдокументация/04_ТЕСТЫ.md`, `CLAUDE.md` | проверки и документы | 1, 2, 3, 5, 8 |

---

### Task 1: Единое правило «в сети»

**Files:**
- Create: `server/screen_online.py`
- Modify: `server/routers/screens.py` (`list_minipcs`), `server/routers/system.py` (`dashboard`: `now_playing`, `disks`), `server/static/js/core/helpers.js`, `server/static/js/views/{screens,dashboard,ota,ticker,placement-editor}.js`, `smoke_test.sh`
- Test: `server/tests/test_screen_online.py`

**Interfaces:**
- Produces (`screen_online.py`):
  - `ONLINE_MINUTES = 5`
  - `ONLINE_SQL = "({a}.last_seen IS NOT NULL AND {a}.last_seen > NOW() - INTERVAL '5 minutes')"` и `OFFLINE_FOR_SQL = "CASE WHEN {a}.last_seen IS NULL THEN NULL ELSE CAST(EXTRACT(EPOCH FROM (NOW() - {a}.last_seen)) AS INTEGER) END"` — шаблоны с алиасом таблицы: `online_sql(alias: str = "s") -> str`, `offline_for_sql(alias: str = "s") -> str` (число минут — из `ONLINE_MINUTES`, не литерал).
- Produces (API): `GET /minipc` и в `GET /dashboard` списки `now_playing`, `disks` получают поля `"online": bool`, `"offline_for_seconds": int | null` (null — нет `last_seen`). Поле `status` остаётся (обратная совместимость), но фронт его для «в сети» больше не использует.
- Produces (JS, `helpers.js`): `fmtOfflineFor(seconds) -> string` — `null` → «ни разу не выходил на связь»; < 3600 → «не в сети N мин»; < 86400 → «не в сети N ч M мин»; иначе «не в сети N дн». Экспорт глобально (как остальные хелперы).

- [ ] **Step 1: Write the failing test**

```python
from screen_online import ONLINE_MINUTES, online_sql, offline_for_sql

def test_online_window_is_five_minutes():
    assert ONLINE_MINUTES == 5
    assert "INTERVAL '5 minutes'" in online_sql("s") and "s.last_seen" in online_sql("s")

def test_alias_is_applied():
    assert "x.last_seen" in online_sql("x") and "x.last_seen" in offline_for_sql("x")

def test_offline_for_is_null_without_last_seen():
    assert offline_for_sql("s").startswith("CASE WHEN s.last_seen IS NULL THEN NULL")
```

- [ ] **Step 2: Run, expect FAIL** (ModuleNotFoundError).
- [ ] **Step 3: Implement** `screen_online.py`, поля в `list_minipcs` и в двух выборках дашборда (через `online_sql()/offline_for_sql()`), `fmtOfflineFor`, замену `s.status==='online'` во всех пяти вью на `s.online` (подписи «не в сети …» — через `fmtOfflineFor(s.offline_for_seconds)` там, где выводится статус). Поднять `?v=` изменённых JS.
- [ ] **Step 4: Run, expect PASS** (весь набор).
- [ ] **Step 5: Smoke** — после регистрации SMOKE-экрана: `GET /minipc` → у него `online == false` и `offline_for_seconds is None`; после `POST /minipc/heartbeat/{id}` (заголовок `X-Token`, тело как у агента: `{"status":"online","playing_file":null}`) → `online == true`. На стенде → «провалено 0».
- [ ] **Step 6: Commit** («Единое правило «в сети»: последний сигнал не старше 5 минут»).

---

### Task 2: Общий эфир — срок, правила, API, истечение

**Files:**
- Create: `server/migrations/032_broadcast_until.sql` (`ALTER TABLE network_broadcast ADD COLUMN IF NOT EXISTS until_at TIMESTAMP;` — наивное МСК, NULL = до отмены), `server/broadcast_rules.py`, `server/broadcast_store.py`
- Modify: `docker-compose.yml` (initdb `32_broadcast_until.sql`), `server/routers/broadcast.py`, `server/tasks.py`, `smoke_test.sh`
- Test: `server/tests/test_broadcast.py`

**Interfaces:**
- Produces (`broadcast_rules.py`, чисто):
  - `parse_until_hour(value, now: datetime) -> datetime | None` — `None`/отсутствует → `None` (до отмены); иначе целое 1…24, строго больше `now.hour` (если `now` ровно HH:00 — тоже больше `now.hour`); 24 → `datetime.combine(now.date() + 1 день, 00:00)`; иначе `ValueError` с русским текстом («Час окончания — целое от 1 до 24», «Час окончания должен быть позже текущего»).
  - `effective_on(row: dict | None, now: datetime) -> bool` — `row["is_on"]` и `row["playlist_id"] is not None` и (`row["until_at"] is None` или `now < row["until_at"]`).
- Produces (`broadcast_store.py`):
  - `get_broadcast(db, now: datetime) -> dict` — `{"is_on": effective_on(...), "playlist_id", "playlist_name", "until_at": datetime|None, "enabled_by", "enabled_at"}`; при `is_on == False` поля плейлиста/срока `None`.
  - `enable(db, playlist_id: int, until_at: datetime | None, actor: str) -> None` — если эфир уже действует → журнал «Общий эфир сменён», иначе «Общий эфир включён»; деталь «Плейлист: «…» · до ЧЧ:ММ» / «· до отмены». `enabled_at` = `now_msk()`.
  - `disable(db, actor: str, title: str = "Общий эфир выключен") -> None` — `is_on = FALSE, playlist_id = NULL, until_at = NULL`.
  - `expire_due(db, now: datetime) -> bool` — если `is_on` и `until_at <= now` → `disable(db, "система", "Общий эфир завершён по времени")`, `True`.
  - Без `commit` внутри (коммитит вызывающий), как `placements_store`.
- Produces (API, `routers/broadcast.py`):
  - `GET /broadcast` → ответ `get_broadcast` (`until_at` ISO).
  - `POST /broadcast/on` — тело JSON `{"playlist_id": int, "until_hour": int | null}`; для обратной совместимости query-параметр `playlist_id` без тела = до отмены. 404 нет плейлиста; 400 на ошибку `parse_until_hour`. Ответ — как GET.
  - `POST /broadcast/off`.
- Produces (Celery, `tasks.py`): задача `expire_broadcast` в beat раз в 60 с — `expire_due(...)` + commit.

- [ ] **Step 1: Write the failing tests**

```python
from datetime import datetime
import pytest
from broadcast_rules import parse_until_hour, effective_on

N = datetime(2026, 10, 28, 14, 30)

def test_until_none_means_until_cancel():
    assert parse_until_hour(None, N) is None

def test_until_hour_today_and_24():
    assert parse_until_hour(16, N) == datetime(2026, 10, 28, 16, 0)
    assert parse_until_hour(24, N) == datetime(2026, 10, 29, 0, 0)

@pytest.mark.parametrize("bad", [14, 13, 0, 25, "x", 15.5])
def test_until_hour_rejected(bad):
    with pytest.raises(ValueError):
        parse_until_hour(bad, N)

def test_effective_on_expires_exactly_at_until_msk():
    row = {"is_on": True, "playlist_id": 5, "until_at": datetime(2026, 10, 28, 15, 0)}
    assert effective_on(row, datetime(2026, 10, 28, 14, 59, 59))
    assert not effective_on(row, datetime(2026, 10, 28, 15, 0))

def test_effective_on_false_when_playlist_deleted():
    assert not effective_on({"is_on": True, "playlist_id": None, "until_at": None}, N)
```

Плюс тесты `broadcast_store` на фейк-БД по образцу `test_placements_store.py` (маршрутизация по подстроке SQL): `expire_due` пишет «завершён по времени» только когда срок прошёл; `enable` при действующем эфире пишет «сменён».

- [ ] **Step 2: Run, expect FAIL.** **Step 3: Implement** (миграция, модули, роутер, задача Celery, строка initdb). **Step 4: Run, expect PASS.**
- [ ] **Step 5: Smoke** — шаг «Общий эфир» выполняется **только на стенде**: если `SMOKE_ALLOW_BROADCAST=1` не задан, шаг пропускается с пометкой «эфир на все экраны — пропущено (задайте SMOKE_ALLOW_BROADCAST=1 на стенде)». Внутри: запомнить исходное состояние; `POST /broadcast/on` до следующего часа (час вычислять в ds_api, при 23 → 24); `GET /broadcast` → `is_on` и `until_at`; `until_hour` в прошлом → 400; восстановить исходное состояние (выключить или вернуть прежний плейлист/срок). `dev_stand.sh` и `04_ТЕСТЫ.md` — команда со `SMOKE_ALLOW_BROADCAST=1` для стенда.
- [ ] **Step 6: Commit** («Общий эфир: срок «до ЧЧ:00», истечение по времени, журнал»).

---

### Task 3: Эфир в ленте экрана, списке скачивания, симуляторе и дашборде

**Files:**
- Modify: `server/routers/screens.py` (`schedule_for_minipc`), `server/placements_logic.py` (`active_until_for`), `server/routers/media.py` (`files_for_screen`), `server/routers/schedule.py` (`simulate_schedule`), `server/routers/system.py` (блок эфира дашборда), `smoke_test.sh`
- Test: `server/tests/test_broadcast.py` (добавить), `server/tests/test_placements_logic.py` (добавить)

**Interfaces:**
- Consumes: Task 2 `get_broadcast`, `effective_on`.
- Produces:
  - `active_until_for(source: str, now: datetime, broadcast_until: datetime | None = None) -> str | None` — для `"broadcast"` → `broadcast_until.isoformat()` или `None`; остальное без изменений.
  - Лента: шаг 1 — `get_broadcast(db, now)`; если действует, но у плейлиста эфира на этом экране нет проигрываемых роликов (`playlist_rows(pid)` пуст) — эфир пропускается, дальше по приоритетам. `active_until` эфира = его срок.
  - `/files/list`: плюс ролики плейлиста действующего эфира (те же фильтры одобрения/площадки).
  - Симулятор: заметка про эфир учитывает срок («…до ЧЧ:ММ» / «до отмены»).
  - Дашборд: `broadcast.is_on` — действующий (`get_broadcast`), плюс `until_at`.

- [ ] **Step 1: Write the failing tests**

```python
def test_active_until_broadcast_uses_until():
    assert active_until_for("broadcast", datetime(2026, 10, 28, 14, 30), datetime(2026, 10, 28, 16, 0)) == "2026-10-28T16:00:00"
    assert active_until_for("broadcast", datetime(2026, 10, 28, 14, 30), None) is None
```

Плюс тест ленты по образцу `test_feed_rollback.py`/`test_resolver.py`: эфир, у плейлиста которого на площадке нет роликов, не делает `active_source == "broadcast"`.
- [ ] **Step 2: Run, expect FAIL.** **Step 3: Implement.** **Step 4: Run, expect PASS.**
- [ ] **Step 5: Smoke** (внутри шага с `SMOKE_ALLOW_BROADCAST=1`): лента SMOKE-экрана `active_source == "broadcast"`, `active_until` = срок; ролик эфира в `/files/list`; после выключения лента не `broadcast`.
- [ ] **Step 6: Commit** («Эфир сети в ленте и закачке экрана, симуляторе и дашборде»).

---

### Task 4: Чистая логика «что идёт сейчас / до / дальше»

**Files:**
- Modify: `server/placements_logic.py` (`week_cells`)
- Create: `server/network_live.py`
- Test: `server/tests/test_network_live.py`

**Interfaces:**
- Produces:
  - `week_cells(days, screen_id, placements, decisions, overrides, slots, names, playable: set[int] | None = None) -> list[dict]` — при заданном `playable` плейлисты показов, которых нет в `playable`, отбрасываются до выбора источника (как `pick_active` в ленте), час проваливается к переопределению/шаблону. `None` — поведение как сейчас.
  - `screen_live(cells: list[dict], now: datetime, broadcast: dict | None, broadcast_playable: bool) -> dict` (`network_live.py`):
    - `cells` — клетки `week_cells` этого экрана на сегодня и завтра (48 шт.), `now` — наивное МСК, `broadcast` — `get_broadcast` (или `None`, если не действует).
    - Возвращает `{"now": {"kind", "playlist_ids", "playlists", "until"}, "next": {"kind", "playlist_ids", "playlists", "from"} | None, "after_broadcast": {...} | None, "broadcast_skipped": bool}`.
    - `now.kind` — `"broadcast"`, если эфир действует и `broadcast_playable`; иначе `kind` текущей клетки. `now.until` (ISO): для эфира — его срок или `None` (до отмены); иначе начало первого часа сегодня, где `(kind, playlist_ids)` отличается; если до конца дня смены нет — `None` (фронт пишет «до конца дня без изменений»).
    - `next` — эта первая отличающаяся клетка сегодня (`from` = её начало), иначе `None`; при действующем эфире `next = None`.
    - `after_broadcast` — при эфире со сроком: клетка на момент срока; при «до отмены» — текущая клетка.
    - `broadcast_skipped = True`, если эфир действует, но на экране нечего играть.

- [ ] **Step 1: Write the failing tests** (`cells` строить хелпером из списка `(hour, kind, ids)` на сегодня/завтра)

```python
from datetime import date, datetime
from network_live import screen_live

D = date(2026, 10, 28)

def cells(spec_today, spec_tomorrow=()):
    """spec: [(hour_from, hour_to, kind, ids)] → 48 клеток week_cells (имена = «PL<id>»)."""
    out = []
    for day, spec in ((D, spec_today), (date(2026, 10, 29), spec_tomorrow)):
        for h in range(24):
            kind, ids = "empty", []
            for a, b, k, i in spec:
                if a <= h < b:
                    kind, ids = k, list(i)
            out.append({"date": day, "hour": h, "kind": kind, "playlist_ids": ids,
                        "playlists": [f"PL{i}" for i in ids], "placement_id": None})
    return out

def test_screen_live_template_until_next_change():
    c = cells([(0, 13, "template", [1]), (13, 15, "placement", [2]), (15, 24, "template", [1])])
    r = screen_live(c, datetime(2026, 10, 28, 10, 30), None, False)
    assert r["now"] == {"kind": "template", "playlist_ids": [1], "playlists": ["PL1"], "until": "2026-10-28T13:00:00"}
    assert r["next"] == {"kind": "placement", "playlist_ids": [2], "playlists": ["PL2"], "from": "2026-10-28T13:00:00"}
    assert r["after_broadcast"] is None and r["broadcast_skipped"] is False
```

```python
def test_screen_live_template_until_next_change(): ...
    # 10:30, шаблон «Общий_день» 0–13, показ «Утро» 13–15 → now template until 13:00, next placement from 13:00
def test_screen_live_no_change_until_end_of_day(): ...      # now.until is None, next is None
def test_screen_live_alternate_kind(): ...                   # клетка с 2 плейлистами → kind "alternate"
def test_screen_live_off_override(): ...                     # kind "off"
def test_screen_live_broadcast_with_until_and_after(): ...   # now broadcast until 16:00, after_broadcast = клетка 16:00
def test_screen_live_broadcast_until_cancel_after_is_current(): ...
def test_screen_live_broadcast_unplayable_falls_to_schedule(): ...  # broadcast_skipped True, now = клетка
def test_week_cells_playable_filters_placements(): ...       # показ с плейлистом вне playable → шаблон
```

(Каждый тест — с явными ожидаемыми значениями `kind`, `playlist_ids`, `until`/`from` в ISO.)
- [ ] **Step 2: Run, expect FAIL.** **Step 3: Implement.** **Step 4: Run, expect PASS** (весь набор: `week_cells` без `playable` не изменился).
- [ ] **Step 5: Commit** («Логика «что идёт сейчас и дальше» для обзора сети»).

---

### Task 5: API обзора сети

**Files:**
- Create: `server/routers/network.py`; Modify: `server/main.py`, `smoke_test.sh`
- Test: `server/tests/test_network_api.py`

**Interfaces:**
- Consumes: Task 1 SQL-фрагменты, Task 2 `get_broadcast`, Task 4 `screen_live`, `week_cells(..., playable)`, `placements_store.load_*`, `load_screen_slots`, `load_playlist_names`.
- Produces:
  - `GET /network/live` (`get_current_admin`) →
    ```
    {"generated_at": ISO, "online_minutes": 5,
     "counts": {"total", "online", "offline"},
     "broadcast": {...get_broadcast..., "screens_total", "offline_screens", "unplayable_screens"},
     "screens": [{"id","name","city","location","online","offline_for_seconds",
                  "playing": {"media_id","title","filename"} | null,
                  "now": {...}, "next": {...}|null, "after_broadcast": {...}|null, "broadcast_skipped": bool}],
     "playlists": {"<id>": {"name"}}}
    ```
    Порядок экранов — по названию. Данные грузятся пакетно: показы/переопределения/решения на сегодня–завтра один раз; слоты — на экран; «проигрываемые плейлисты» — один раз на тип площадки (`venue_type`), по тем же фильтрам, что лента; `playing` — `screens.playing_file` → `media` (по имени файла).
  - `GET /network/playlist/{id}/usage` → `{"template_slots": int}` (число слотов недельного шаблона с этим плейлистом); 404 нет плейлиста.
  - Чистый хелпер для теста: `build_live(screens: list[dict], cells_by_screen: dict, now, broadcast, playable_by_venue: dict) -> dict` (вся сборка ответа без SQL).
- [ ] **Step 1: Write the failing tests** — `build_live`: сортировка по названию; `counts`; `unplayable_screens` считает экраны с `broadcast_skipped`; `offline_screens` — экраны не в сети при действующем эфире; `playlists` содержит все id из `now/next/after`.
- [ ] **Step 2: Run, expect FAIL.** **Step 3: Implement.** **Step 4: Run, expect PASS.**
- [ ] **Step 5: Smoke** — `GET /network/live` → 200; SMOKE-экран в `screens` с `now.kind` по его показу текущего часа (показ уже создаётся в smoke); `GET /network/playlist/$PID/usage` → 200.
- [ ] **Step 6: Commit** («API обзора сети: что идёт на экранах»).

---

### Task 6: Страница «Эфир сети» — таблица, панель «О плейлисте», правка поверх

**Files:**
- Create: `server/static/js/views/network-live.js`
- Modify: `server/static/js/views/playlist-editor.js` (опция `notice`), `server/static/js/core/layout.js` (вью раздела `broadcast` → `viewNetworkLive`), `server/static/index.html`, `server/static/css/style.css`
- Delete: `server/static/js/views/broadcast.js` (и его `<script>`)

**Interfaces:**
- Consumes: Task 5 API; `openPlaylistEditor({playlistId, onSaved, notice})`, `openPlacementEditor({playlistId})` (событие `placements-changed`), `openMediaPreview(id)`, `fmtOfflineFor`.
- Produces: `viewNetworkLive()` → `window.Signage.viewNetworkLive`; `refreshNetworkLive()` (повторный запрос без сброса состояния); data-actions с префиксом `nl-`: `nl-open-playlist` (`data-playlist-id`), `nl-close-drawer`, `nl-edit-playlist`, `nl-send-playlist`, `nl-preview-clip` (`data-media-id`); поиск `#nl-search`. Полосу эфира рендерит Task 7 — здесь место под неё `<div id="nl-broadcast"></div>` и вызов `Signage.renderBroadcastBar?.(data)` после каждой загрузки.
- Поведение (спека §4.1–§4.3): шапка со счётчиками и «обновлено ЧЧ:ММ:СС»; таблица «Экран / Сейчас идёт / Играет ролик / Дальше» (при эфире последняя колонка «После эфира»); плашки плейлистов с цветом `schPlColor(id)` (как в «Расписании») и источником (тексты спеки §4.1: «по расписанию до ЧЧ:00», «📅 показ до ЧЧ:00», «⇄ по очереди с «…»», «замена на дату», «📡 общий эфир до ЧЧ:ММ / до отмены», «выключено (замена на дату)», «нет расписания — заглушки»; для экрана не в сети при эфире — «📡 получит при подключении»); колонка «Играет ролик»: в сети — превью + название, не в сети — «нет связи — последний: <ролик>»; колонка экрана у отключённого — `fmtOfflineFor(...)` вместо адреса; поиск с нормализацией «ё»→«е» по экрану, городу, месту, названиям плейлистов в строке; автообновление `setInterval` 30 с, только при `document.visibilityState === 'visible'` и пока вью открыто (очищать при уходе со страницы); после обновления сохраняются поиск, открытая панель (перерисовывается тем же плейлистом) и прокрутка. Панель: название, «N роликов · M показов · м:сс», режим блока (`/playlists/{id}/fill_info`), «Сейчас идёт на N экранах» (из данных страницы), ролики (`/playlists/{id}/items`, клик — просмотр), «Где и когда идёт» (`/placements?playlist_id=`) и «в недельном расписании: N слотов» (`/network/playlist/{id}/usage`); кнопки «✎ Изменить плейлист» и «📤 Отправить на экраны» только при `canWrite()`. «✎ Изменить» → `openPlaylistEditor({playlistId, notice: N>0 ? «Плейлист сейчас идёт на N экранах — изменения дойдут до них в течение минуты» : null, onSaved: refresh})`; `notice` в окне правки — серая строка над плитками. После `placements-changed` и `onSaved` — `refreshNetworkLive()` сразу.
- [ ] **Step 1: Implement.** **Step 2: Проверка на стенде** (браузерный скрипт вне репозитория): таблица заполнена; клик по плашке → панель; «✎ Изменить» → окно с подсказкой → сохранить → таблица обновилась без перезагрузки; открыть панель, ввести поиск, подождать 35 с → панель и поиск на месте; пустая сеть (нет экранов) — понятное пустое состояние; ошибок консоли нет; `ui_test.sh` без новых провалов.
- [ ] **Step 3: Commit** («Эфир сети: что идёт на экранах, панель плейлиста, правка поверх»).

---

### Task 7: Полоса общего эфира и окно включения

**Files:**
- Create: `server/static/js/views/broadcast-window.js`; Modify: `index.html`, `style.css`

**Interfaces:**
- Consumes: Task 2 API (`POST /broadcast/on` с `{playlist_id, until_hour}`, `/broadcast/off`), данные `/network/live` (`broadcast`, `counts`), `GET /playlists` и для пустых плейлистов — `/playlists/{id}/fill_info` или поле из ответа (решение разработчика: показать серым плейлист без проигрываемых роликов).
- Produces: `renderBroadcastBar(data)` и `openBroadcastWindow({mode: 'enable'|'change'})` в `window.Signage`; data-actions `bc-open`, `bc-change`, `bc-off`; в окне — `#bc-playlist` (выбор с поиском), радио `data-action="bc-duration" data-mode="cancel|until"`, `#bc-until-hour` (`<select>` часов от `now.hour+1` до 24 по МСК), кнопки `bc-cancel`, `bc-submit`.
- Поведение (спека §4.4): полоса всегда над таблицей — выключен (серая пунктирная, кнопка «Включить общий эфир…» только при `canWrite()`), включён (жёлтая: «📡 Общий эфир: «…» на всех N экранах», «с ЧЧ:ММ · до ЧЧ:ММ (осталось X ч Y мин) · включил <кто> · расписание на это время перекрыто» или «· до отмены», «Сменить плейлист», «Выключить» с `confirm`), при `unplayable_screens > 0` — «на K экранах нечего играть — там идёт обычное расписание». Окно: предупреждение со числами N (всего) и K (не в сети), кнопка «Включить на N экранах» / «Сменить плейлист», ошибки сервера внутри окна, блок кнопок во время запроса; после успеха — `refreshNetworkLive()`.
- [ ] **Step 1: Implement.** **Step 2: Проверка на стенде**: включить «до следующего часа» → жёлтая полоса со сроком, у экранов «📡 общий эфир до …»; «Сменить плейлист» → окно с текущими значениями; «Выключить» → серая полоса; час в прошлом недоступен в списке; без права записи — кнопок нет.
- [ ] **Step 3: Commit** («Эфир сети: полоса общего эфира и окно включения»).

---

### Task 8: E2E, документы

**Files:**
- Modify: `ui_tests/ui_test.js`, `docs/Инструкции/03_РОЛЬ_АДМИНИСТРАТОР.md` (раздел «Эфир сети», упоминание «в сети» в «Экранах»), `docs/Техдокументация/04_ТЕСТЫ.md` (новые тесты, `SMOKE_ALLOW_BROADCAST`), `CLAUDE.md` (миграции 001…032, роутер `network`, модули `network_live.py`, `broadcast_rules.py`, `broadcast_store.py`, `screen_online.py`)

- [ ] **Step 1: E2E** — раздел «Эфир сети»: таблица есть, в ней экран фикстуры E2E; клик по плашке его плейлиста → панель; «✎ Изменить» → окно правки → Esc (без сохранения); шаг с общим эфиром — **только** если `E2E_ALLOW_BROADCAST=1` (иначе пропуск с пометкой): включить до следующего часа → полоса жёлтая → выключить. Убрать из белого списка действий `broadcast-on`/`broadcast-off` (старые), добавить безопасные `nl-open-playlist`, `nl-close-drawer`.
- [ ] **Step 2: Все наборы** — pytest (весь), smoke на стенде с `SMOKE_ALLOW_BROADCAST=1` → «провалено 0», E2E с `E2E_ALLOW_BROADCAST=1` → 0 провалов.
- [ ] **Step 3: Документы.** **Step 4: Commit** («E2E и инструкции для «Эфира сети»»).
- [ ] **Step 5: Выкатка — только по согласию пользователя** (вместе с этапом 1, см. память о порядке выкатки).
