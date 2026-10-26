# Переделка плейлистов — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** окно сборки плейлиста с просмотром роликов, повторами и проверками, отправка плейлиста на экраны по датам и часам с разбором пересечений, и чтобы повторы ×N реально играли на экране.

**Architecture:** чистая логика (повторы, предупреждения, чередование «общий час», пересечения, приоритеты показов) — в двух модулях без БД (`server/playlist_rules.py`, `server/placements_logic.py`), покрытых pytest. SQL — в `server/placements_store.py` и роутерах. Новая сущность «показ» (таблицы `placements`, `placement_screens`, `placement_decisions`). Агенту показ отдаётся существующим механизмом «активного плейлиста» в `GET /schedule/minipc/{id}` (как эфир сети и замена на дату), общий час — готовой последовательностью `hourly_sequence`; агент меняется только в одном месте — перестаёт схлопывать повторы. Фронт — новые модули vanilla JS с общим модальным окном.

**Tech Stack:** FastAPI + SQLAlchemy `text()` + PostgreSQL 15; vanilla JS (без сборки), `style.css`; агент Python 3 + mpv; pytest; puppeteer (`ui_tests/ui_test.js`).

**Spec:** `docs/superpowers/specs/2026-10-26-playlists-redesign-design.md` (согласовано 26.10.2026). Читать вместе с планом. Макеты: `Video Claude/.superpowers/brainstorm/14021-1793016390/content/` (`playlist-window-v3.html`, `send-to-screens.html`, `send-to-screens-v2.html`, `placements-v2.html`).

## Global Constraints

- Время — московское: «сегодня», часы показа, границы дат считаются по `MSK` (`datetime.now(MSK)`, как в `routers/screens.py`), не по UTC контейнера.
- Часы показа — только целые: `hour_from` 0…23, `hour_to` 1…24, `hour_to > hour_from`; «с 09:00 до 13:00» = часы 9, 10, 11, 12.
- Повторы `repeat_count` — 1…99.
- Пороги предупреждений: больше **3** повторов подряд; один ролик больше **50 %** длительности плейлиста (только если в плейлисте ≥ 2 разных ролика). Константы `MAX_REPEATS_IN_ROW = 3`, `MAX_SHARE = 0.5` в `server/playlist_rules.py`.
- Все предупреждения мягкие: создать/сохранить/отправить можно; кнопка меняется на «Создать всё равно» / «Сохранить всё равно».
- Пересечения — только с другим показом или датовым переопределением с плейлистом. Недельный шаблон пересечением не является.
- «По очереди» = общий час: ролики плейлистов вперемешку по одному, заглушки добивают остаток (если у кого-то из плейлистов `fill_to_hour`; длина блока — наибольшая из `target_seconds`).
- На экран идут только ролики `status='ready' AND review_status='approved'`, в сроке `valid_from/valid_until`, с фильтром алкоголя по `venue_type` — все существующие фильтры выдачи сохраняются.
- Дубль = совпадение `media.md5_hash`; документы (`status='document'`) в проверке не участвуют.
- Новая миграция `server/migrations/031_placements.sql` — идемпотентная (`IF NOT EXISTS`), добавить в список initdb в `docker-compose.yml`.
- Фронт — vanilla JS без фреймворков; новые модули подключаются в `server/static/index.html` с `?v=20261026`; функции, которые зовут из других модулей, экспортируются в `window.Signage`.
- Права: изменять — `require_write` на сервере / `canWrite()` на фронте; читать — `get_current_admin`.
- Никакого AI/ML. Секреты только в `.env`.
- Коммиты — по задаче, сообщение по-русски в стиле репозитория, последней строкой `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Запуск pytest локально (на Mac нет зависимостей): `docker run --rm -v "$PWD/server":/src:ro python:3.11-slim sh -c "apt-get update -qq >/dev/null && apt-get install -y -qq gcc python3-dev >/dev/null 2>&1; cp -r /src /app && cd /app && pip install -q --root-user-action=ignore -r requirements.txt -r requirements-dev.txt && python -m pytest tests -q -p no:cacheprovider"` (ниже — «pytest-контейнер»; к команде можно добавить путь/`-k`).

## Review Focus

1. **Полночь по МСК против UTC** — в 00:30 МСК (21:30 UTC прошлого дня) показ «с 01.11» уже должен идти 01.11 в час 0, а в 23:30 МСК — ещё нет. Тест: Task 3 `test_applies_at_uses_given_msk_hour`, Task 6 `test_resolver_uses_msk_now`.
2. **Поиск «пятерочка» должен находить «Пятёрочка»** — пользователи печатают «е» вместо «ё». Нормализация `toLowerCase().replace(/ё/g,'е')` в поиске окна сборки (Task 10) и экранов (Task 11). Проверка: Task 10 Step 2 (ролик «Пятёрочка_тест» ищется строкой «пятерочка»), Task 11 Step 2 (экран с «ё» в названии ищется через «е»).
3. **«Остановить» в середине часа** — остановили в 10:37: час 10 больше не играет сразу, прошедшие часы остаются в истории. Тест: Task 3 `test_stop_mid_hour_stops_current_hour`.
4. **Правка идущего показа** — `date_from` уже в прошлом; сохранение не должно падать с «дата в прошлом», если `date_from` не меняли. Тест: Task 4 `test_parse_body_allows_past_date_from_when_unchanged`.
5. **Плейлист показа без проигрываемых роликов** (все сняты с модерации/истекли) — экран не должен уйти в чёрное: показ пропускается, работает обычное расписание. Тест: Task 6 `test_pick_active_skips_playlists_without_items`.

---

## Карта файлов

| Файл | Что | Задачи |
|---|---|---|
| `agent/ds_player.py` | + `build_playable()` — список для mpv с повторами | 1 |
| `agent/ds_agent.py` | использовать `build_playable()` | 1 |
| `agent/ds_heartbeat.py` | `AGENT_VERSION = "1.3.0"` | 1 |
| `agent/tests/test_playable.py` | новый | 1 |
| `server/playlist_rules.py` | новый: повторы, предупреждения, чередование, общий час | 2 |
| `server/admin_panel.py` | вынести `ad_sequence()` и `distribute_fillers()` | 2 |
| `server/placements_logic.py` | новый: даты/часы, статус, пересечения, выбор плейлистов, недельная сетка | 3 |
| `server/migrations/031_placements.sql`, `docker-compose.yml` | новые таблицы | 4 |
| `server/placements_store.py` | новый: SQL-загрузка показов/решений/переопределений | 4, 5, 6 |
| `server/routers/placements.py`, `server/main.py` | новый API показов | 4, 5 |
| `docker-compose.dev.yml`, `dev_stand.sh`, `.gitignore` | локальный стенд для проверок | 4 |
| `smoke_test.sh` | + цикл показа, + плейлист через `/playlists/full` | 4, 7 |
| `server/routers/screens.py` | показы и повторы в выдаче агенту | 6 |
| `server/routers/schedule.py` | симулятор учитывает показы | 6 |
| `server/routers/playlists.py` | `/playlists/check`, `/playlists/full`, `PUT /playlists/{id}/items` | 7 |
| `server/routers/media.py` | `review_status` в списке, дубли, проверка при загрузке, сведения для просмотра | 8 |
| `server/static/js/core/modal.js` | новый: модальные окна | 9 |
| `server/static/js/views/media-preview.js` | новый: просмотр ролика | 9 |
| `server/static/js/views/playlist-editor.js` | новый: окно «Новый плейлист» шаг 1 | 10 |
| `server/static/js/views/placement-editor.js` | новый: шаг 2 «Куда и когда» | 11 |
| `server/static/js/views/playlists.js` | создание/правка через окно, блок «Где и когда идёт» | 10, 12 |
| `server/static/js/views/schedule.js` | режим «По датам» | 13 |
| `server/static/js/views/media.js` | просмотр по клику, дубли (метка, окно при загрузке) | 9, 14 |
| `server/static/css/style.css`, `server/static/index.html` | стили, подключение модулей | 9–13 |
| `ui_tests/ui_test.js` | новый цикл плейлиста и показа | 15 |
| `docs/Инструкции/03_РОЛЬ_АДМИНИСТРАТОР.md`, `07_ОБНОВЛЕНИЕ_АГЕНТА.md` | разделы «Медиатека», «Плейлисты», «Расписание», OTA 1.3.0 | 15 |

---

### Task 1: Агент не схлопывает повторы

**Files:**
- Modify: `agent/ds_player.py` (новая функция модульного уровня)
- Modify: `agent/ds_agent.py:414-423` (цикл `for fname in files:` → вызов функции)
- Modify: `agent/ds_heartbeat.py:11`
- Test: `agent/tests/test_playable.py`

**Interfaces:**
- Produces: `build_playable(files: list[str], broken: set[str], exists: Callable[[str], bool]) -> list[str]` в `ds_player.py` — порядок и повторы сохраняются, выкидываются только `broken` и несуществующие.

- [ ] **Step 1: Write the failing test** (`agent/tests/test_playable.py`; в начале `sys.path.insert(0, <каталог agent>)`, `from ds_player import build_playable`)

```python
def test_repeats_and_order_kept():
    assert build_playable(["a", "a", "b", "a"], set(), lambda f: True) == ["a", "a", "b", "a"]

def test_broken_and_missing_dropped():
    assert build_playable(["a", "x", "b", "m"], {"x"}, lambda f: f != "m") == ["a", "b"]

def test_empty():
    assert build_playable([], set(), lambda f: True) == []
```

- [ ] **Step 2: Run, expect FAIL** — `docker run --rm -v "$PWD/agent":/a:ro python:3.11-slim sh -c "pip install -q pytest && cd /a && python -m pytest tests -q -p no:cacheprovider"` → ImportError `build_playable`.
- [ ] **Step 3: Implement `build_playable` in `ds_player.py`; in `ds_agent.py` replace the loop with `playable = build_playable(files, broken_files, lambda f: (Path(MEDIA_DIR) / f).exists())`** — сохранить предупреждение в лог про отсутствующий файл: логировать внутри `exists`-лямбды нельзя, поэтому в `ds_agent.py` перед вызовом оставить цикл логирования отсутствующих (без `append`). Обновить `AGENT_VERSION = "1.3.0"`.
- [ ] **Step 4: Run, expect PASS** (та же команда, 3 passed).
- [ ] **Step 5: Commit** — `git add agent/ && git commit` («Агент 1.3.0: повторы ×N и круги заглушек больше не схлопываются»).

---

### Task 2: Правила плейлиста — повторы, предупреждения, общий час

**Files:**
- Create: `server/playlist_rules.py`
- Modify: `server/admin_panel.py` — вынести из `build_hourly_detail` два куска в функции, `build_hourly_detail` их использует (поведение не меняется)
- Test: `server/tests/test_playlist_rules.py`

**Interfaces:**
- Produces (`admin_panel.py`):
  - `ad_sequence(db, playlist_id: int, venue_type: str = "other") -> list[tuple[str, float]]` — первый SQL из `build_hourly_detail` + развёртка `repeat_count` (имя файла, длительность).
  - `filler_list(db, venue_type: str = "other") -> list[tuple[str, float]]` — второй SQL.
  - `distribute_fillers(ads: list[tuple[str, float]], fillers: list[tuple[str, float]], target_seconds: float) -> tuple[list[str], float, int]` — алгоритм раскладки из `build_hourly_detail` (последовательность имён, секунд заглушек, штук заглушек); при пустых `ads` → `([], 0.0, 0)`.
- Produces (`playlist_rules.py`):
  - `MAX_REPEATS_IN_ROW = 3`, `MAX_SHARE = 0.5`
  - `expand_repeats(items: list[dict]) -> list[dict]` — каждый элемент `max(1, repeat_count or 1)` раз подряд.
  - `interleave(sequences: list[list]) -> list` — по одному элементу из каждой по кругу, пока все не кончатся.
  - `repeat_warnings(items: list[dict]) -> list[str]` — `items`: `{"media_id","title","duration","repeat_count","md5"}` в порядке плейлиста. Порядок предупреждений: сначала все «подряд» (по порядку роликов), затем «доля», затем «дубли».
  - `build_shared_hour(ad_seqs: list[list[tuple[str, float]]], fillers: list[tuple[str, float]], target_seconds: float | None) -> list[str]` — `interleave(ad_seqs)`, затем при `target_seconds` → `distribute_fillers`, иначе просто имена.
  - `overflow_minutes(ad_seconds_total: float, limit_seconds: float = 3600) -> int | None` — `round(total/60)` если больше лимита, иначе `None`.

- [ ] **Step 1: Write the failing tests**

```python
from playlist_rules import (expand_repeats, interleave, repeat_warnings,
                            build_shared_hour, overflow_minutes)
from admin_panel import distribute_fillers

def it(mid, title, dur, rep=1, md5=None):
    return {"media_id": mid, "title": title, "duration": dur, "repeat_count": rep, "md5": md5 or f"h{mid}"}

def test_expand_repeats():
    assert [x["media_id"] for x in expand_repeats([it(1, "a", 10, 3), it(2, "b", 5)])] == [1, 1, 1, 2]

def test_interleave_uneven():
    assert interleave([["a1", "a2", "a3"], ["b1"]]) == ["a1", "b1", "a2", "a3"]

def test_warn_more_than_three_in_row():
    assert repeat_warnings([it(1, "Пятёрочка_осень", 15, 4), it(2, "Афиша", 30)])[0] == \
        "«Пятёрочка_осень» ×4 подряд — больше 3 повторов"

def test_no_warn_at_exactly_three():
    assert not any("подряд" in w for w in repeat_warnings([it(1, "A", 15, 3), it(2, "B", 100)]))

def test_warn_share_over_half():
    w = repeat_warnings([it(1, "Пятёрочка_осень", 15, 4), it(2, "Афиша", 30), it(3, "Погода", 12, 2)])
    assert "«Пятёрочка_осень» занимает 60 из 114 с (53 %) — больше половины плейлиста" in w

def test_no_share_warning_for_single_item():
    assert repeat_warnings([it(1, "A", 15, 2)]) == []

def test_warn_duplicate_files():
    w = repeat_warnings([it(1, "X", 10, md5="same"), it(2, "X (2)", 10, md5="same")])
    assert "«X» и «X (2)» — это один и тот же файл" in w

def test_shared_hour_without_fill_is_interleave():
    assert build_shared_hour([[("a", 10), ("a", 10)], [("b", 10)]], [("f", 5)], None) == ["a", "b", "a"]

def test_shared_hour_with_fill_adds_fillers_but_keeps_ad_order():
    seq = build_shared_hour([[("a", 10)], [("b", 10)]], [("f", 10)], 60)
    assert [x for x in seq if x != "f"] == ["a", "b"] and seq.count("f") == 4

def test_distribute_fillers_never_exceeds_target():
    seq, secs, n = distribute_fillers([("a", 20)], [("f", 15)], 60)
    assert secs <= 40 and n == 2 and seq[0] == "a"

def test_overflow_minutes():
    assert overflow_minutes(72 * 60) == 72 and overflow_minutes(3600) is None
```

- [ ] **Step 2: Run, expect FAIL** — pytest-контейнер `tests/test_playlist_rules.py` → ModuleNotFoundError `playlist_rules`.
- [ ] **Step 3: Implement** — рефакторинг `admin_panel.py` (три функции выше; `build_hourly_detail` собирает `detail` из них, ключи ответа прежние) и `playlist_rules.py`. Дубли в `repeat_warnings` — по первому вхождению каждой пары (`md5` совпал у разных `media_id`); доля считается по `duration × repeat_count`, проценты `round()`; числа секунд — `int(round())`.
- [ ] **Step 4: Run, expect PASS** — pytest-контейнер (весь каталог `tests`, старые 47 тоже зелёные: `build_hourly_detail` не сломан).
- [ ] **Step 5: Commit** («Правила плейлиста: повторы, предупреждения, общий час»).

---

### Task 3: Логика показов — даты, часы, статусы, пересечения, выбор плейлистов

**Files:**
- Create: `server/placements_logic.py`
- Test: `server/tests/test_placements_logic.py`

**Interfaces:**
- Показ в логике — `dict`: `id, playlist_id, playlist_name, date_from: date, date_to: date, hour_from: int, hour_to: int, stopped_at: datetime | None` (наивный МСК), `screens: set[int]`.
- Переопределение — `dict`: `id, screen_id, on_date: date, playlist_id, playlist_name, is_off: bool` (групповые уже развёрнуты по экранам).
- Решения — `dict[(placement_id, screen_id, other_kind, other_id)] -> "replace" | "alternate"`.
- Produces:
  - `dates_overlap(a_from, a_to, b_from, b_to) -> tuple[date, date] | None`
  - `hours_overlap(a_from, a_to, b_from, b_to) -> tuple[int, int] | None` (полуинтервалы `[from, to)`)
  - `applies_at(p: dict, day: date, hour: int) -> bool` — дата и час внутри; если `stopped_at` задан — только часы, **закончившиеся** не позже `stopped_at` (`datetime.combine(day, time(hour)) + 1ч <= stopped_at`).
  - `placement_status(p: dict, now: datetime) -> str` — `"stopped"` если `stopped_at`; `"done"` если `now.date() > date_to` или (`== date_to` и `now.hour >= hour_to`); `"planned"` если `now.date() < date_from`; иначе `"active"`.
  - `find_conflicts(new: dict, others: list[dict], overrides: list[dict]) -> tuple[list[dict], list[dict]]` — (пересечения, выключенные дни). Пересечение: `{"screen_id","other_kind","other_id","other_playlist_id","other_playlist_name","date_from","date_to","hour_from","hour_to"}`; `others` с `stopped_at` и с `id == new.get("id")` пропускаются; переопределение с плейлистом занимает весь день → часы пересечения = часы нового показа; `is_off` → в выключенные дни `{"screen_id","on_date"}`. Порядок: по `screen_id`, затем `date_from`.
  - `choose_playlists(applicable: list[dict], decisions: dict, override: dict | None, screen_id: int) -> list[int]` — новейший показ (max `id`) первым; старшие показы — только при решении `alternate` у новейшего; переопределение с плейлистом (не `is_off`) — в конце при решении `alternate`; без решения или `replace` — исключить. Без повторов `playlist_id`. Пусто, если `applicable` пуст.
  - `template_playlist(slots: list[dict], dow: int, hour: int) -> int | None` — `slots`: `{"day_of_week","hour"(None=весь день),"playlist_id","prio"}`; почасовой слот приоритетнее «весь день», внутри — больший `prio`.

- [ ] **Step 1: Write the failing tests**

```python
from datetime import date, datetime
from placements_logic import (dates_overlap, hours_overlap, applies_at, placement_status,
                              find_conflicts, choose_playlists, template_playlist)

def P(id, d1, d2, h1, h2, screens, pl=10, stopped=None):
    return {"id": id, "playlist_id": pl, "playlist_name": f"PL{pl}", "date_from": d1, "date_to": d2,
            "hour_from": h1, "hour_to": h2, "stopped_at": stopped, "screens": set(screens)}

N1, N30 = date(2026, 11, 1), date(2026, 11, 30)

def test_hours_overlap_half_open():
    assert hours_overlap(9, 13, 13, 15) is None and hours_overlap(9, 13, 10, 12) == (10, 12)

def test_applies_at_uses_given_msk_hour():
    p = P(1, N1, N30, 0, 24, [1])
    assert applies_at(p, date(2026, 11, 1), 0) and not applies_at(p, date(2026, 10, 31), 23)

def test_stop_mid_hour_stops_current_hour():
    p = P(1, N1, N30, 9, 13, [1], stopped=datetime(2026, 11, 5, 10, 37))
    assert applies_at(p, date(2026, 11, 5), 9) and not applies_at(p, date(2026, 11, 5), 10)

def test_status():
    p = P(1, N1, N30, 9, 13, [1])
    assert placement_status(p, datetime(2026, 10, 31, 12)) == "planned"
    assert placement_status(p, datetime(2026, 11, 30, 12)) == "active"
    assert placement_status(p, datetime(2026, 11, 30, 13)) == "done"

def test_conflict_with_other_placement_and_override():
    new = P(None, N1, N30, 9, 13, [1, 2])
    other = P(7, date(2026, 11, 5), date(2026, 11, 10), 10, 12, [1], pl=20)
    ov = {"id": 3, "screen_id": 2, "on_date": date(2026, 11, 7), "playlist_id": 30, "playlist_name": "PL30", "is_off": False}
    off = {"id": 4, "screen_id": 1, "on_date": date(2026, 11, 8), "playlist_id": None, "playlist_name": None, "is_off": True}
    conflicts, off_days = find_conflicts(new, [other], [ov, off])
    assert [(c["screen_id"], c["other_kind"], c["date_from"], c["hour_from"], c["hour_to"]) for c in conflicts] == \
        [(1, "placement", date(2026, 11, 5), 10, 12), (2, "override", date(2026, 11, 7), 9, 13)]
    assert off_days == [{"screen_id": 1, "on_date": date(2026, 11, 8)}]

def test_conflicts_skip_self_and_stopped():
    new = P(5, N1, N30, 9, 13, [1])
    assert find_conflicts(new, [P(5, N1, N30, 9, 13, [1]), P(6, N1, N30, 9, 13, [1], stopped=datetime(2026, 11, 2))], []) == ([], [])

def test_choose_newest_wins_without_decision():
    assert choose_playlists([P(1, N1, N30, 9, 13, [1], pl=10), P(2, N1, N30, 9, 13, [1], pl=20)], {}, None, 1) == [20]

def test_choose_alternate_includes_older_and_override():
    d = {(2, 1, "placement", 1): "alternate", (2, 1, "override", 3): "alternate"}
    ov = {"id": 3, "playlist_id": 30, "is_off": False}
    assert choose_playlists([P(1, N1, N30, 9, 13, [1], pl=10), P(2, N1, N30, 9, 13, [1], pl=20)], d, ov, 1) == [20, 10, 30]

def test_template_hour_beats_all_day():
    slots = [{"day_of_week": 0, "hour": None, "playlist_id": 1, "prio": 3},
             {"day_of_week": 0, "hour": 9, "playlist_id": 2, "prio": 1}]
    assert template_playlist(slots, 0, 9) == 2 and template_playlist(slots, 0, 10) == 1
```

- [ ] **Step 2: Run, expect FAIL** — pytest-контейнер `tests/test_placements_logic.py`.
- [ ] **Step 3: Implement `server/placements_logic.py`** (только stdlib).
- [ ] **Step 4: Run, expect PASS**.
- [ ] **Step 5: Commit** («Логика показов: даты, часы, пересечения, выбор плейлистов»).

---

### Task 4: Таблицы показов, предпросмотр и создание показа, локальный стенд

**Files:**
- Create: `server/migrations/031_placements.sql`; Modify: `docker-compose.yml` (initdb: `./server/migrations/031_placements.sql:/docker-entrypoint-initdb.d/31_placements.sql:ro`)
- Create: `server/placements_store.py`, `server/routers/placements.py`; Modify: `server/main.py` (импорт и `app.include_router(placements.router)` рядом с прочими)
- Create: `docker-compose.dev.yml`, `dev_stand.sh`; Modify: `.gitignore` (+ `.dev_admin_pass`)
- Modify: `smoke_test.sh` (новый шаг после «Основной цикл эфира»)
- Test: `server/tests/test_placements_api.py`

**Interfaces:**
- Схема (миграция):

```sql
CREATE TABLE IF NOT EXISTS placements (
    id          SERIAL PRIMARY KEY,
    playlist_id INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
    all_screens BOOLEAN NOT NULL DEFAULT FALSE,
    date_from   DATE NOT NULL,
    date_to     DATE NOT NULL CHECK (date_to >= date_from),
    hour_from   SMALLINT NOT NULL CHECK (hour_from BETWEEN 0 AND 23),
    hour_to     SMALLINT NOT NULL CHECK (hour_to BETWEEN 1 AND 24),
    stopped_at  TIMESTAMP,              -- МСК; NULL — не остановлен
    created_by  VARCHAR(64),
    created_at  TIMESTAMP DEFAULT NOW(),
    CHECK (hour_to > hour_from)
);
CREATE TABLE IF NOT EXISTS placement_screens (
    placement_id INTEGER NOT NULL REFERENCES placements(id) ON DELETE CASCADE,
    screen_id    INTEGER NOT NULL REFERENCES screens(id) ON DELETE CASCADE,
    PRIMARY KEY (placement_id, screen_id)
);
CREATE TABLE IF NOT EXISTS placement_decisions (
    placement_id INTEGER NOT NULL REFERENCES placements(id) ON DELETE CASCADE,
    screen_id    INTEGER NOT NULL REFERENCES screens(id) ON DELETE CASCADE,
    other_kind   VARCHAR(10) NOT NULL CHECK (other_kind IN ('placement', 'override')),
    other_id     INTEGER NOT NULL,
    mode         VARCHAR(10) NOT NULL CHECK (mode IN ('replace', 'alternate')),
    PRIMARY KEY (placement_id, screen_id, other_kind, other_id)
);
CREATE INDEX IF NOT EXISTS idx_placements_dates ON placements(date_from, date_to);
CREATE INDEX IF NOT EXISTS idx_placements_playlist ON placements(playlist_id);
```

- `placements_store.py`:
  - `all_screen_ids(db) -> set[int]`
  - `load_placements(db, d_from: date, d_to: date, playlist_id: int | None = None) -> list[dict]` — формат Task 3, `screens` развёрнуты (`all_screens` → `all_screen_ids`), плюс поля `all_screens`, `created_by`, `created_at`.
  - `load_overrides(db, d_from: date, d_to: date) -> list[dict]` — формат Task 3; групповые переопределения развёрнуты по экранам группы; переопределение экрана приоритетнее группового на ту же дату.
  - `load_decisions(db, placement_ids: list[int]) -> dict` — формат Task 3.
  - `save_placement(db, data: dict, decisions: list[dict], actor: str, placement_id: int | None = None) -> int` — insert/update `placements`, перезапись `placement_screens` и `placement_decisions`, запись в `audit_log` (`event_type='schedule'`, `title` «Показ: создан» / «Показ: изменён»), без `commit` (коммитит роутер).
- `routers/placements.py`:
  - Тело: `{"playlist_id": int, "all_screens": bool, "screen_ids": [int], "date_from": "YYYY-MM-DD", "date_to": "YYYY-MM-DD", "hour_from": int, "hour_to": int}`; для правки ещё `"exclude_id"` в preview.
  - `parse_body(body: dict, today: date, existing_date_from: date | None = None) -> dict` — 400 при: нет плейлиста/экранов (если не `all_screens`), кривые даты, `date_from < today` (кроме случая `date_from == existing_date_from`), `date_to < date_from`, часы вне границ или `hour_to <= hour_from`. Возвращает данные в формате Task 3 + `all_screens`.
  - `POST /placements/preview` → `{"conflicts": [...+ "screen_name"], "off_days": [...+ "screen_name"], "notices": [str], "overflow": [str], "summary": str, "screen_count": int}`. `notices`: если включён эфир сети — «Сейчас включён «Эфир сети» — пока он включён, показы на экранах не идут». `overflow`: для каждого пересечения, где сумма длительностей рекламы двух плейлистов (с повторами, `ad_sequence`) больше часа — «в {hour_from}:00 ролики двух плейлистов занимают {N} мин — в час не помещаются, часть выйдет реже». `summary` — «Плейлист пойдёт на {N} экран(ах) с {dd.mm} по {dd.mm.yyyy}, ежедневно {HH:00}–{HH:00}. В остальное время экраны показывают своё обычное расписание.» (для всех экранов — «на всех экранах сети ({N})»).
  - `POST /placements` (тело + `"decisions": [{"screen_id","other_kind","other_id","mode"}]`) → `{"id": int}`; 409 `{"detail": "Не решено пересечений: N"}`, если на какое-то пересечение нет решения.

- [ ] **Step 1: Write the failing tests** (`test_placements_api.py`, чистые функции роутера)

```python
from datetime import date
import pytest
from fastapi import HTTPException
from routers.placements import parse_body

T = date(2026, 10, 26)
B = {"playlist_id": 1, "all_screens": False, "screen_ids": [3], "date_from": "2026-11-01",
     "date_to": "2026-11-30", "hour_from": 9, "hour_to": 13}

def test_parse_body_ok():
    d = parse_body(B, T)
    assert (d["date_from"], d["hour_to"], d["screens"]) == (date(2026, 11, 1), 13, {3})

@pytest.mark.parametrize("patch", [{"hour_to": 9}, {"hour_from": 24}, {"hour_to": 25},
                                   {"date_to": "2026-10-31"}, {"screen_ids": []}, {"date_from": "2026-10-25"}])
def test_parse_body_rejects(patch):
    with pytest.raises(HTTPException) as e:
        parse_body({**B, **patch}, T)
    assert e.value.status_code == 400

def test_parse_body_allows_past_date_from_when_unchanged():
    d = parse_body({**B, "date_from": "2026-10-20"}, T, existing_date_from=date(2026, 10, 20))
    assert d["date_from"] == date(2026, 10, 20)

def test_all_screens_needs_no_list():
    assert parse_body({**B, "all_screens": True, "screen_ids": []}, T)["all_screens"] is True
```

- [ ] **Step 2: Run, expect FAIL** — pytest-контейнер `tests/test_placements_api.py`.
- [ ] **Step 3: Implement migration, `placements_store.py`, `routers/placements.py` (preview + create), регистрацию в `main.py`, строку initdb в `docker-compose.yml`.** `screens` для «все экраны» внутри `parse_body` не разворачиваются (это делает роутер через `all_screen_ids`, у `parse_body` нет БД) — `screens` = `set()` при `all_screens`.
- [ ] **Step 4: Run, expect PASS** (весь `tests`).
- [ ] **Step 5: Локальный стенд.** `docker-compose.dev.yml`: `nginx.ports: !override ["8080:80"]`, `ntp.ports: !reset []`. `dev_stand.sh up|down|reset`: `up` — `bash install_server.sh --env-only` (создаёт `.env`, если нет), `docker compose -p dsdev -f docker-compose.yml -f docker-compose.dev.yml up -d --build`, ожидание `pg_isready`, `bash migrate.sh`, затем для локального стенда снять принудительную смену пароля у `admin` (`UPDATE users SET must_change_password=FALSE WHERE username='admin'`) — пароль стенда — сид-значение из миграции 001 (`admin123`), записать его в `.dev_admin_pass` (в `.gitignore`); `down` — `docker compose -p dsdev ... down` (без `-v`); `reset` — `down -v` + `up`. Проверка: `bash dev_stand.sh up && curl -s localhost:8080/health` → `{"status":"ok"...}`.
- [ ] **Step 6: Smoke на стенде.** В `smoke_test.sh` после шага «Основной цикл эфира» новый шаг «Показ по датам»: при наличии `$SID` и `$PID` — `POST /placements/preview` (тело JSON через `-H 'Content-Type: application/json' -d`, даты = завтра…послезавтра по `date -d`, часы 10–12) → 200 и `summary` непустой; `POST /placements` без решений → 200 и `id`; запомнить `PLC`. В «Уборке» — `DELETE /placements/$PLC` до удаления плейлиста (появится в Task 5; до тех пор показ удаляется каскадом с плейлистом — допустимо). Run: `ADMIN_PASS=$(cat .dev_admin_pass) bash smoke_test.sh` → «провалено 0».
- [ ] **Step 7: Commit** («Показы: таблицы, предпросмотр пересечений, создание; локальный стенд»).

---

### Task 5: Показы — список, правка, остановка, отмена, недельная сетка

**Files:**
- Modify: `server/routers/placements.py`, `server/placements_store.py`, `smoke_test.sh`
- Test: `server/tests/test_placements_logic.py` (добавить), `server/tests/test_placements_api.py` (добавить)

**Interfaces:**
- Consumes: Task 3 (`placement_status`, `choose_playlists`, `template_playlist`, `applies_at`), Task 4 (`load_*`, `save_placement`, `parse_body`).
- Produces:
  - `GET /placements?playlist_id=` → `[{"id","playlist_id","all_screens","screen_count","screens":[{"id","name"}],"date_from","date_to","hour_from","hour_to","status","alternates":[{"screen_name","other_playlist_name","date_from","date_to","hour_from","hour_to"}],"created_by","created_at"}]`, сортировка: active, planned, done/stopped; внутри — `date_from` по убыванию.
  - `PUT /placements/{id}` — как `POST`, `parse_body(..., existing_date_from=<текущее>)`; preview с `exclude_id`.
  - `POST /placements/{id}/stop` — только `status == "active"` → `stopped_at = now MSK`; иначе 409 «Остановить можно только идущий показ».
  - `DELETE /placements/{id}` — только `planned` → удалить; иначе 409 «Показ уже идёт или закончился — используйте «Остановить»».
  - `GET /placements/week?screen_id=&start=YYYY-MM-DD` (start — понедельник; иначе 400) → `{"days": ["YYYY-MM-DD"×7], "cells": [{"date","hour","kind","playlists":[name],"placement_id"}]}`, 168 клеток; `kind`: `off` | `placement` | `alternate` (≥2 плейлиста) | `override` | `template` | `empty`.
  - В `placements_logic.py`: `week_cells(days: list[date], screen_id: int, placements: list[dict], decisions: dict, overrides: list[dict], slots: list[dict], names: dict[int, str]) -> list[dict]` — порядок источников как в спеке §4.7 (без эфира сети): `off` → показы (`choose_playlists`) → `override` → `template_playlist` → `empty`.
  - Удаление плейлиста (`DELETE /playlists/{id}`) не меняется — показы удаляются каскадом.

- [ ] **Step 1: Write the failing tests**

```python
# test_placements_logic.py
from datetime import timedelta
from placements_logic import week_cells

def test_week_cells_priorities():
    days = [date(2026, 11, 2) + timedelta(days=i) for i in range(7)]
    pl = [P(1, N1, N30, 9, 13, [5], pl=10)]
    ovs = [{"id": 9, "screen_id": 5, "on_date": date(2026, 11, 3), "playlist_id": None, "playlist_name": None, "is_off": True}]
    slots = [{"day_of_week": d, "hour": None, "playlist_id": 1, "prio": 1} for d in range(7)]
    cells = {(c["date"], c["hour"]): c for c in week_cells(days, 5, pl, {}, ovs, slots, {10: "Утро", 1: "Общий"})}
    assert cells[(date(2026, 11, 2), 9)]["kind"] == "placement" and cells[(date(2026, 11, 2), 9)]["playlists"] == ["Утро"]
    assert cells[(date(2026, 11, 2), 13)]["kind"] == "template"
    assert cells[(date(2026, 11, 3), 9)]["kind"] == "off"
    assert len(cells) == 168
```

```python
# test_placements_api.py
from routers.placements import week_start
def test_week_start_must_be_monday():
    assert week_start("2026-11-02") == date(2026, 11, 2)
    with pytest.raises(HTTPException):
        week_start("2026-11-03")
```

- [ ] **Step 2: Run, expect FAIL**.
- [ ] **Step 3: Implement** `week_cells`, `week_start(s: str) -> date` (400 если не понедельник), эндпоинты списка/правки/остановки/отмены/недели. В `audit_log`: «Показ: остановлен», «Показ: отменён».
- [ ] **Step 4: Run, expect PASS**.
- [ ] **Step 5: Smoke** — в шаг «Показ по датам» добавить `GET /placements?playlist_id=$PID` → 200 и содержит `$PLC`; `GET /placements/week?screen_id=$SID&start=<понедельник текущей недели>` → 200; в «Уборке» `DELETE /placements/$PLC` (показ запланирован на завтра → 200). Run на стенде → «провалено 0».
- [ ] **Step 6: Commit** («Показы: список, правка, остановка, отмена, недельная сетка»).

---

### Task 6: Показы и повторы доходят до экрана; симулятор

**Files:**
- Modify: `server/routers/screens.py` (`schedule_for_minipc`), `server/routers/schedule.py` (`simulate_schedule`), `server/placements_store.py`
- Test: `server/tests/test_placements_logic.py` (добавить), `server/tests/test_resolver.py` (новый)

**Interfaces:**
- Consumes: Task 2 (`expand_repeats`, `build_shared_hour`, `ad_sequence`, `filler_list`), Task 3 (`applies_at`, `choose_playlists`), Task 4 (`load_*`).
- Produces:
  - `placements_store.resolve_placement_hour(db, screen_id: int, now: datetime) -> list[int]` — плейлисты показа на текущий час МСК (`now` — МСК), с учётом решений и сегодняшнего переопределения; `[]` если показа нет.
  - `placements_logic.pick_active(playlist_ids: list[int], playable: dict[int, list]) -> list[int]` — оставить только плейлисты с непустым списком проигрываемых роликов.
- Поведение `GET /schedule/minipc/{id}` (порядок): 1) эфир сети; 2) сегодняшнее переопределение `is_off` → `off`; 3) показ: `ids = pick_active(resolve_placement_hour(...), …)`; один плейлист → как сейчас для активного (`active_items` + `hourly_sequence` при `fill_to_hour`); несколько → `active_playlist = {"playlist_id": ids[0], "playlist_ids": ids, "playlist_name": " ⇄ ".join(names)}`, `hourly_sequence = build_shared_hour([ad_sequence(db, pid, venue) …], filler_list(db, venue), max(target_seconds тех, у кого fill_to_hour) или None)`, `active_items` — ролики всех плейлистов (для загрузки файлов); `active_source = "placement"`; 4) переопределение с плейлистом; 5) шаблон. **Повторы:** `active_items = expand_repeats(...)`, `items` каждого слота шаблона — `expand_repeats(slot["items"])`. Список `files` не дублирует файлы.
- `simulate_schedule`: после «показ выключен» — показ (`source="placement"`, `source_label="Показ по датам"`, при нескольких — «Показ по датам: по очереди»), элементы — `interleave` элементов плейлистов с повторами.

- [ ] **Step 1: Write the failing tests**

```python
# test_placements_logic.py
from placements_logic import pick_active
def test_pick_active_skips_playlists_without_items():
    assert pick_active([20, 10], {20: [], 10: ["a.mp4"]}) == [10]
    assert pick_active([20], {20: []}) == []
```

```python
# test_resolver.py — store с подменой загрузчиков (monkeypatch), без SQL
from datetime import datetime, date
import placements_store as st

def test_resolver_uses_msk_now(monkeypatch):
    p = {"id": 1, "playlist_id": 10, "playlist_name": "A", "date_from": date(2026, 11, 1), "date_to": date(2026, 11, 1),
         "hour_from": 0, "hour_to": 1, "stopped_at": None, "screens": {5}}
    monkeypatch.setattr(st, "load_placements", lambda db, a, b, playlist_id=None: [p])
    monkeypatch.setattr(st, "load_overrides", lambda db, a, b: [])
    monkeypatch.setattr(st, "load_decisions", lambda db, ids: {})
    assert st.resolve_placement_hour(None, 5, datetime(2026, 11, 1, 0, 30)) == [10]
    assert st.resolve_placement_hour(None, 5, datetime(2026, 10, 31, 23, 30)) == []
```

- [ ] **Step 2: Run, expect FAIL**.
- [ ] **Step 3: Implement** `pick_active`, `resolve_placement_hour` (берёт `load_*` через модульные имена, чтобы monkeypatch работал), встраивание в `schedule_for_minipc` и `simulate_schedule`. `now` в эндпоинте — `datetime.now(MSK).replace(tzinfo=None)`.
- [ ] **Step 4: Run, expect PASS** (весь `tests`).
- [ ] **Step 5: Проверка на стенде** — создать на стенде экран (`smoke_test.sh` создаёт и удаляет; для ручной проверки — `POST /minipc/register?name=DEV_scr`), плейлист из двух одобренных роликов (×2 у первого), показ на текущий час; `curl` к `/api/schedule/minipc/{id}` с `X-Token` экрана → `active_source == "placement"`, в `active_items`/`hourly_sequence` первый ролик дважды подряд. Второй показ «по очереди» на тот же час → `hourly_sequence` чередует имена двух плейлистов. `GET /api/schedule/simulate` на этот час → `source == "placement"`.
- [ ] **Step 6: Commit** («Показы и повторы ×N в выдаче экрану, симулятор»).

---

### Task 7: API плейлиста — проверка, создание одним запросом, замена роликов

**Files:**
- Modify: `server/routers/playlists.py`, `smoke_test.sh`
- Test: `server/tests/test_playlists_api.py`

**Interfaces:**
- Consumes: Task 2 `repeat_warnings`.
- Produces:
  - `normalize_items(raw: list) -> list[dict]` — `[{"media_id": int, "repeat_count": int}]`; 400 при пустом списке, повторе `media_id`, `repeat_count` вне 1…99, нецелых значениях.
  - `POST /playlists/check` `{"items":[...]}` → `{"warnings":[str], "total_seconds": float, "plays": int}` (пустой список допустим → нули и `[]`).
  - `POST /playlists/full` `{"name": str, "items":[...]}` → `{"id", "name", "warnings"}`; 400 на пустое имя; 409 с теми же текстами, что в `add_to_playlist` (документ / не одобрен) — вынести проверку ролика в `_check_media(m) -> None` и использовать в обоих местах; всё в одной транзакции.
  - `PUT /playlists/{id}/items` `{"name"?: str, "items":[...]}` → `{"status": "ok", "warnings": [...]}` — переименование (если `name`), удаление всех `playlist_items` плейлиста и вставка новых с `position` 1…N, одна транзакция; 404 если плейлиста нет.
  - Старые `POST /playlists?name=` и `POST /playlists/{id}/items` остаются (ими пользуются smoke/E2E и, возможно, внешние скрипты).

- [ ] **Step 1: Write the failing tests**

```python
import pytest
from fastapi import HTTPException
from routers.playlists import normalize_items

def test_normalize_ok():
    assert normalize_items([{"media_id": 3, "repeat_count": 2}, {"media_id": "4"}]) == \
        [{"media_id": 3, "repeat_count": 2}, {"media_id": 4, "repeat_count": 1}]

@pytest.mark.parametrize("raw", [[], [{"media_id": 3}, {"media_id": 3}], [{"media_id": 3, "repeat_count": 0}],
                                 [{"media_id": 3, "repeat_count": 100}], [{"media_id": "x"}]])
def test_normalize_rejects(raw):
    with pytest.raises(HTTPException) as e:
        normalize_items(raw)
    assert e.value.status_code == 400
```

- [ ] **Step 2: Run, expect FAIL**. **Step 3: Implement.** **Step 4: Run, expect PASS.**
- [ ] **Step 5: Smoke** — в «Основной цикл эфира» после загрузки медиа добавить: `POST /playlists/check` с `[{"media_id":$MID,"repeat_count":4}]` → 200 и в `warnings` нет падения (один ролик → `[]`); создание второго плейлиста `SMOKE_full_$STAMP` через `POST /playlists/full` → `id`; `PUT /playlists/$PID2/items` → 200; удалить его в «Уборке». Тестовое медиа smoke грузит с `category=service` — оно одобряется автоматически (`AD_CATEGORIES['service'].auto_ok`), поэтому `/playlists/full` с ним отвечает 200. Run на стенде → «провалено 0».
- [ ] **Step 6: Commit** («API плейлиста: проверка, создание одним запросом, замена роликов»).

---

### Task 8: API медиатеки — статус в списке, дубли, сведения для просмотра

**Files:**
- Modify: `server/routers/media.py`, `smoke_test.sh`
- Test: `server/tests/test_media_dups.py`

**Interfaces:**
- Produces:
  - `GET /media` — добавить `m.review_status` в выборку.
  - `group_duplicates(rows: list) -> dict[int, list[dict]]` (чистая; `rows` — `id,title,md5_hash`) — для каждого `id` список остальных роликов с тем же `md5_hash` (`{"id","title"}`), только группы ≥ 2, `md5_hash` пустой не группируется.
  - `GET /media/duplicates` → `{"<id>": [{"id","title"}]}` по всем не-документам.
  - `POST /media/upload` — новый параметр `allow_duplicate: bool = Query(False)`; сразу после `md5_hash = md5.hexdigest()`, для не-документа и при `allow_duplicate == False`: если есть ролик с тем же `md5_hash` и `status <> 'document'` — удалить записанный файл и `HTTPException(409, detail={"message": "Такой ролик уже есть в медиатеке", "duplicate_of": {"id","title","filename","folder","advertiser","created_at","duration_seconds","playlists"}})`. Для заглушек (`is_filler`) — так же.
  - `GET /media/{id}` — добавить `"playlists_count"` (число плейлистов с этим роликом), `"folder_name"`, `"advertiser_name"`.

- [ ] **Step 1: Write the failing tests**

```python
from types import SimpleNamespace as R
from routers.media import group_duplicates

def test_group_duplicates():
    rows = [R(id=1, title="A", md5_hash="x"), R(id=2, title="A (2)", md5_hash="x"),
            R(id=3, title="B", md5_hash="y"), R(id=4, title="C", md5_hash=None)]
    assert group_duplicates(rows) == {1: [{"id": 2, "title": "A (2)"}], 2: [{"id": 1, "title": "A"}]}
```

- [ ] **Step 2: Run, expect FAIL**. **Step 3: Implement.** **Step 4: Run, expect PASS.**
- [ ] **Step 5: Smoke** — повторная загрузка того же `/tmp/smoke.mp4` без `allow_duplicate` → 409; с `allow_duplicate=true` → 200 и `id` (удалить в «Уборке»); `GET /media/duplicates` → 200 и содержит `$MID`. Run на стенде → «провалено 0».
- [ ] **Step 6: Commit** («Медиатека: проверка дублей, статус модерации в списке, сведения для просмотра»).

---

### Task 9: Модальные окна и просмотр ролика

**Files:**
- Create: `server/static/js/core/modal.js`, `server/static/js/views/media-preview.js`
- Modify: `server/static/css/style.css`, `server/static/index.html`, `server/static/js/views/media.js` (клик по превью во всех списках: общая медиатека, папки, рекламодатель, заглушки)

**Interfaces:**
- Produces:
  - `openModal({title: string, body: string, footer: string, width?: number}) -> {el: HTMLElement, close: () => void}` — окна складываются стопкой (просмотр поверх окна сборки), Esc и клик по фону закрывают верхнее; `closeTopModal()`. Классы: `.modal-backdrop .modal .modal-head .modal-body .modal-foot`.
  - `openMediaPreview(mediaId: number)` → `window.Signage.openMediaPreview` — `GET /media/{id}`; видео: `<video controls autoplay src="${location.origin}/media/${encodeURIComponent(filename)}">`, размер кадра — из `videoWidth×videoHeight` после `loadedmetadata`; картинка — `<img>`; документ — не открывать. Сведения: длительность (`m:ss`), размер кадра, файл и МБ, папка/рекламодатель, статус модерации по-русски («одобрен», «на модерации», «отклонён»), «В плейлистах: N».
  - Действие клика: `data-action="mprev-open" data-media-id="…"` (обработчик в `media-preview.js` по образцу `initPlaylistsViewActions`).

- [ ] **Step 1: Implement** модуль окна, просмотр, стили (тёмная тема из `:root`, ширина ≤ 92vw, на узком экране сведения под видео), подключение в `index.html` (`core/modal.js` после `helpers.js`, `views/media-preview.js` после `media.js`).
- [ ] **Step 2: Проверка на стенде** — `bash dev_stand.sh up`, загрузить ролик в медиатеку, клик по превью → окно, видео играет, перемотка работает (Range), Esc закрывает; то же в папке «Заглушки». Консоль браузера без ошибок.
- [ ] **Step 3: Commit** («Панель: модальные окна и просмотр ролика»).

---

### Task 10: Окно «Новый плейлист» — шаг 1

**Files:**
- Create: `server/static/js/views/playlist-editor.js`
- Modify: `server/static/js/views/playlists.js` (`createPlaylistPrompt` → окно; в карточке плейлиста кнопка «✎ Изменить ролики» вместо строки «папка/ролик/повторов/+ Добавить»), `style.css`, `index.html`

**Interfaces:**
- Consumes: Task 7 (`/playlists/check`, `/playlists/full`, `PUT /playlists/{id}/items`), Task 8 (`/media` с `review_status`, `/media/duplicates`), Task 9 (`openModal`, `openMediaPreview`).
- Produces: `openPlaylistEditor({playlistId?: number}) -> void` → `window.Signage.openPlaylistEditor`; по завершении «Создать и отправить →» вызывает `window.Signage.openPlacementEditor({playlistId})` (Task 11; до Task 11 кнопка скрыта, если функции нет).
- Поведение (спека §4.2, §4.3, §4.4): поле `#pled-name`; слева группы папок — та же сборка групп, что сейчас в `openPlaylist` (вынести в `buildMediaGroups(media, folders) -> [{key,label}]` в `playlists.js` и использовать в обоих местах до удаления старой строки); поиск `#pled-search` по всем папкам с нормализацией `s.toLowerCase().replace(/ё/g,'е')`, подсветка `<mark>`, «найдено N»; плитки `data-action="pled-toggle" data-media-id`; цифра порядка; счётчик `pled-rep-minus/plus` (1…99); «▶ смотреть» `data-action="mprev-open"`; неодобренные — серые с подписью, не выбираются; заглушки и документы не показываются; метка «дубль» + подпись «тот же файл, что „…“»; предупреждения — `POST /playlists/check` с задержкой 300 мс после изменения, жёлтый блок, кнопки меняют подписи на «Создать всё равно» / «Сохранить всё равно»; низ — «Выбрано N роликов · M показов · m:ss»; кнопки `pled-cancel`, `pled-save`, `pled-save-send`. Правка: открывается с отмеченными роликами в их порядке и с повторами, сохранение — `PUT`.
- Обновить `ui_tests/ui_test.js`: убрать `playlists-add-item` и связанные действия из белого списка, если они там есть.

- [ ] **Step 1: Implement**.
- [ ] **Step 2: Проверка на стенде** — создать плейлист из трёх роликов, у одного ×4 → два предупреждения (×4 подряд, больше половины) и «Создать всё равно»; поиск «пятерочка» находит ролик «Пятёрочка_тест» (назвать так тестовый ролик); правка плейлиста открывает тот же выбор; удаление галочки сдвигает номера.
- [ ] **Step 3: Commit** («Окно «Новый плейлист»: галочки, поиск, повторы, предупреждения»).

---

### Task 11: Шаг 2 «Куда и когда»

**Files:**
- Create: `server/static/js/views/placement-editor.js`; Modify: `style.css`, `index.html`

**Interfaces:**
- Consumes: Task 4/5 API (`/placements/preview`, `POST/PUT /placements`), `GET /minipc` (экраны, онлайн-статус — поля как в разделе «Экраны»).
- Produces: `openPlacementEditor({playlistId: number, placementId?: number, repeatFrom?: number}) -> void` → `window.Signage.openPlacementEditor`; после успешной отправки — `toast('Показ отправлен на экраны')` и событие `document.dispatchEvent(new CustomEvent('placements-changed', {detail:{playlistId}}))`.
- Поведение (спека §4.5, §4.6): переключатель «Все экраны сети (N)» / «Выбрать экраны»; список экранов с галочками, поиском (та же нормализация ё/е) по названию и адресу, точка «в сети»; даты `<input type="date">` (мин. — сегодня по МСК, кроме правки); часы — два `<select>` 00:00…23:00 / 01:00…24:00; при каждом изменении — `POST /placements/preview` (задержка 300 мс); блок «Пересечения · N»: по каждому — экран, даты, часы, «уже стоит «…»», две радиокнопки с пояснениями (тексты из спеки §4.6, «Крутить по очереди» — «Ролики обоих плейлистов идут вперемешку в одном часе, заглушки добивают остаток»), галочка «Применить выбранный вариант ко всем пересечениям»; `off_days`, `notices`, `overflow` — жёлтым без выбора; итог — `summary`; кнопки `plc-back` (к шагу 1, только при создании), `plc-save-only` («Сохранить без отправки» = закрыть), `plc-send` — неактивна, пока есть пересечение без решения; подпись «Не решено: N пересечений». `repeatFrom` — подставить экраны и часы закончившегося показа, даты пустые. Правка — `PUT`, preview с `exclude_id`.

- [ ] **Step 1: Implement**.
- [ ] **Step 2: Проверка на стенде** — два экрана (один назвать «Пушкинская, вёсла», искать строкой «весла»); показ A на оба, 10–12, на неделю; показ B на один экран, 9–13 на те же дни → одно пересечение, «Отправить» неактивна до выбора; «по очереди» → после отправки `GET /schedule/minipc/{id}` в 10:00 отдаёт чередование (или симулятор на 10:00 — «по очереди»).
- [ ] **Step 3: Commit** («Шаг «Куда и когда»: экраны, даты, часы, пересечения»).

---

### Task 12: Карточка плейлиста — «Где и когда идёт»

**Files:**
- Modify: `server/static/js/views/playlists.js`, `style.css`

**Interfaces:**
- Consumes: `GET /placements?playlist_id=`, `POST /placements/{id}/stop`, `DELETE /placements/{id}`, Task 11 `openPlacementEditor`, событие `placements-changed`.
- Поведение (спека §4.8): блок над роликами — строки показов (экраны или «Все экраны сети · N», даты `dd.mm – dd.mm.yyyy`, часы, статус-плашка: active «идёт сейчас», planned «запланирован», done «закончился», stopped «остановлен», строки `alternates` — «dd.mm–dd.mm, HH–HH ч: по очереди с «…»»); кнопки по статусу: active — «Изменить», «Остановить» (`confirm`); planned — «Изменить», «Отменить» (`confirm`); done/stopped — «Повторить». Шапка карточки: «✎ Изменить ролики», «📤 Отправить на экраны». Удаление плейлиста: если есть active/planned показы — текст `confirm` «Плейлист стоит в N показах — они будут отменены. Удалить плейлист «…»?».

- [ ] **Step 1: Implement**. **Step 2: Проверка на стенде** — все четыре статуса видны (done — показ с датами в прошлом создать напрямую SQL на стенде), «Остановить» меняет статус, «Повторить» открывает шаг 2 с пустыми датами. **Step 3: Commit** («Карточка плейлиста: где и когда идёт»).

---

### Task 13: Расписание — режим «По датам»

**Files:**
- Modify: `server/static/js/views/schedule.js`, `style.css`

**Interfaces:**
- Consumes: `GET /placements/week`, `POST /placements/{id}/stop`, `openPlacementEditor`, `openPlaylist` (из `playlists.js`, через `Signage.nav`/`Signage.openPlaylist`).
- Поведение (спека §4.8): `SCH_MODE='dates'` — кнопка «По датам» в сегменте режимов, только для цели «Экран»; неделя с понедельника текущей, стрелки ‹ › (`schedule-week-prev/next`), заголовки «Пн 02.11», сегодня выделен; 24 строки часов; классы клеток по `kind` (`template` голубой, `placement` пунктир + 📅, `alternate` штриховка + ⇄, `override`, `off`, `empty`); клик по клетке с `placement_id` — карточка под сеткой: название, даты, часы, экраны, статус, «Открыть плейлист», «Изменить», «Остановить»; легенда. Режимы «Неделя» и «Даты» не меняются.

- [ ] **Step 1: Implement**. **Step 2: Проверка на стенде** — на неделе с показами из Task 11 видны показ, «по очереди» и шаблон; ‹ › листают; «Остановить» из карточки обновляет сетку. **Step 3: Commit** («Расписание: режим «По датам»»).

---

### Task 14: Медиатека — дубли

**Files:**
- Modify: `server/static/js/views/media.js`, `style.css`

**Interfaces:**
- Consumes: Task 8 (`/media/duplicates`, 409 с `duplicate_of`, `allow_duplicate`), Task 9 (`openModal`, `openMediaPreview`).
- Produces: `uploadWithDuplicateCheck(file: File, params: URLSearchParams) -> Promise<object|null>` в `media.js` — общий для всех мест загрузки (обычная загрузка и «↑ Загрузить заглушки»): при ошибке разобрать `JSON.parse(e.message).detail.duplicate_of` (`api()` бросает текст ответа); показать окно «Такой ролик уже есть в медиатеке» (превью `/media/{id}/thumbnail`, «Вы загружаете <имя> — это точно тот же файл, что и «…»», «Папка: … · загружен dd.mm.yyyy · m:ss · используется в N плейлистах»), кнопки «Отмена» (→ `null`), «Всё равно загрузить» (повтор с `allow_duplicate=true`), «Открыть существующий» (`openMediaPreview`, → `null`). При нескольких файлах в «Заглушках» — окно на каждый дубль по очереди.
- Метка «дубль» на карточках медиатеки (все списки) по `/media/duplicates` с подсказкой «тот же файл, что „…“».

- [ ] **Step 1: Implement**. **Step 2: Проверка на стенде** — загрузить тот же файл повторно в обычную папку и в «Заглушки» → окно; «Отмена» — файла нет ни в списке, ни на диске (`docker exec ds_api ls /data/media | wc -l` не изменилось); «Всё равно загрузить» — появился с меткой «дубль». **Step 3: Commit** («Медиатека: предупреждение о дубле при загрузке, метка «дубль»»).

---

### Task 15: E2E, инструкции, выкатка

**Files:**
- Modify: `ui_tests/ui_test.js`, `docs/Инструкции/03_РОЛЬ_АДМИНИСТРАТОР.md` (разделы «Медиатека», «Заглушки», «Плейлисты», «Расписание»), `docs/Инструкции/07_ОБНОВЛЕНИЕ_АГЕНТА.md` (версия 1.3.0 — что меняется: повторы ×N), `docs/Техдокументация/04_ТЕСТЫ.md` (стенд `dev_stand.sh`, новые тесты)

- [ ] **Step 1: E2E** — заменить цикл «плейлист» (шаг 5): `playlists-create` → окно; ввести `PLAYLIST_NAME` в `#pled-name`; в `#pled-search` ввести часть названия существующего одобренного ролика; клик по первой выбираемой `[data-action="pled-toggle"]`; `pled-rep-plus` ×4 → в окне появился текст «подряд»; `pled-save-send` → окно шага 2 → «Выбрать экраны» не трогать, выбрать «Все экраны сети», даты завтра…завтра, 10–11 → `plc-send` (если есть пересечения — выбрать «Заменить» и «ко всем») → в карточке строка «запланирован» → «Отменить» → удалить плейлист. Плюс: в «Медиатеке» клик по первому `[data-action="mprev-open"]` → есть `<video>` или `<img>`, Esc. Добавить `mprev-open`, `schedule-mode-dates`, `schedule-week-next` в белый список безопасных действий. Run: `ADMIN_PASS=$(cat .dev_admin_pass) DS_URL=http://nginx bash ui_test.sh` на стенде → 0 провалов.
- [ ] **Step 2: Весь pytest** — pytest-контейнер (весь `tests`) и тест агента → зелёные; `ADMIN_PASS=$(cat .dev_admin_pass) bash smoke_test.sh` на стенде → «провалено 0».
- [ ] **Step 3: Инструкции** — обновить разделы (что делает окно, повторы и предупреждения, дубли, «Куда и когда», пересечения, «Где и когда идёт», «По датам»; OTA 1.3.0).
- [ ] **Step 4: Commit** («E2E и инструкции для новых плейлистов»).
- [ ] **Step 5: Выкатка — только по согласию пользователя.** Порядок на `toor@10.0.119.100`: дамп БД и архив папки (как в `08_ОБНОВЛЕНИЕ_СЕРВЕРА.md`); залить `git archive` в `/home/toor/Video_miniPC_v16.2_modular` (`.env` не трогать); `bash install_server.sh` (применит 031); обновить `DEPLOYED_COMMIT`; проверить контрольные суммы и `/health`. Затем напомнить пользователю: `smoke_test.sh` на сервере (пароль вводит сам) и загрузка агента 1.3.0 через «Обновление агентов (OTA)» в панели.
