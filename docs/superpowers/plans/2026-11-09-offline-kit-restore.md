# Комплект для установки и восстановление одной командой — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Сервер после каждой выкатки сам сохраняет комплект образов; суперадмин скачивает его из панели раз на
версию; по флешке (копия + ролики + комплект) систему поднимает одна команда `restore.sh` — на чистой машине и
поверх старой установки, с возвратом «как было».

**Architecture:** Вся работа с образами — один bash-скрипт `offline_kit.sh` (отпечаток, сохранение, проверка,
загрузка); `install_server.sh` и `restore.sh` его вызывают, панель читает его опись `offline_kit/KIT.txt` и
считает тот же отпечаток на Python (`server/offline_kit.py`), pytest сверяет оба расчёта. Скачивание комплекта —
ещё один вид одноразовой ссылки в существующем роутере «Копии на флешку». `restore.sh` лежит открыто во внешнем
tar ежедневной копии, его функции тестируются по отдельности (`RESTORE_LIB=1`), весь путь — учениями в
Docker-в-Docker.

**Tech Stack:** bash 5 (Astra 1.7 / Debian), Docker Engine ≥ 20.10 + Compose v2, OpenSSL 1.1.1+, Python 3.11 +
FastAPI + SQLAlchemy, PostgreSQL 15, vanilla JS, pytest, puppeteer (ui_test.js).

**Spec:** `docs/superpowers/specs/2026-11-09-offline-kit-restore-design.md` (макеты —
`docs/superpowers/specs/2026-11-09-offline-kit-restore-mockups.html`). Читать оба вместе с планом.

## Global Constraints

- Всё, что видит человек (панель, вывод скриптов, тексты отказов, инструкция), — по-русски.
- Пароль на копию и значения из `.env` не попадают в аргументы процессов (`ps`), в вывод, в логи, в ответы API.
- Моменты в БД — наивное МСК из Python (`now_msk`), не `NOW()` Postgres.
- Каждая новая миграция — строкой initdb в `docker-compose.yml` (правило проекта).
- **Имя проекта Compose не меняется** (от него зависят имена томов с данными); имена образов — постоянные через
  `image:`.
- Скачивание — только суперадмин, по одноразовой ссылке (тикет Redis, 2 мин); «скачано» и журнал — только после
  последнего байта.
- `restore.sh`, `rollback.sh`, `offline_kit.sh` работают на чистой Astra 1.7 / Debian 11: bash, GNU coreutils,
  tar, gzip, sha256sum, openssl 1.1.1; **без python** и без `jq`.
- Параметры расшифровки копии — ровно `OPENSSL_DECRYPT_CMD` из `server/offsite_crypto.py`
  (`-aes-256-cbc -pbkdf2 -md sha256 -iter 200000`).
- На диске сервера — не больше одного комплекта; `offline_kit/` не идёт ни в git, ни в копию программы.
- Никакого AI/ML. Документация — сразу и с измеренными числами (правило пользователя).
- `git add` — только явные пути (`.superpowers/`, `.impeccable/` не в `.gitignore` и не коммитятся).
- Docker-команды на Mac требуют выхода из песочницы — с одобрения пользователя. Не пушить и не трогать
  10.0.119.100 без согласия пользователя.
- Отклонение от ориентира спеки §6: отпечаток живёт в `offline_kit.sh` (функция `kit_fingerprint`), а не в
  отдельном `kit_fingerprint.sh` — «одно место» сохраняется, файлом меньше.

## Review Focus

1. **Пароль на копию со спецзнаками** (`!`, `$`, `"`, `'`, `\`, обратная кавычка, `#`) — расшифровка проходит,
   пароля нет в `ps`. Тест — Task 8, `test_restore_decrypt_special_password`.
2. **Путь флешки с пробелами и кириллицей** (`/media/user/МОЯ ФЛЕШКА`) — список копий, выбор комплекта,
   распаковка роликов работают. Тест — Task 8, `test_restore_paths_with_spaces_and_cyrillic`.
3. **На флешке копии и комплекты разных версий** — берётся комплект с версией из MANIFEST копии; нет подходящего —
   сборка с прямой строкой «подходящего комплекта нет». Тест — Task 8, `test_pick_kit_*`.
4. **Docker есть, но пользователь не в группе `docker`** (permission denied) или служба остановлена — скрипт
   переходит на `sudo`, а не падает. Тест — Task 8, `test_docker_prefix_*`.
5. **Повторный запуск после обрыва**: на чистой машине своя недоделанная установка не считается «старой» (нет
   «ДА», нет сохранения); поверх старой — прежняя `ds_before_restore_*` не тронута. Тест — Task 9,
   `test_own_half_install_*` и `test_before_dirs_never_overwritten`.

---

## Файлы

| Файл | Что делает |
|---|---|
| `offline_kit.sh` (новый, корень) | отпечаток, `save`, `verify`, `field`, `load`; при `source` — только функции |
| `server/offline_kit.py` (новый) | Python-двойник отпечатка, чтение `offline_kit/KIT.txt` → `KitInfo` |
| `server/.dockerignore` (новый) | в образ `api`/`celery` из `./server` идёт только `requirements.txt` |
| `docker-compose.yml` | `image:` у `api`/`celery`/`ntp`; initdb для 036 |
| `install_server.sh` | шаг «Комплект для установки», `--kit FILE`, `--no-kit` |
| `server/migrations/036_offsite_kit.sql` (новый) | поля «комплект скачан» в `offsite_copy_state` |
| `server/offsite_rules.py` | `kit_pending`, `reminder_state(..., kit_pending)`, `offline_kit` в исключениях |
| `server/offsite_store.py` | колонки состояния, `mark_kit_done`, строка комплекта в MANIFEST, `restore.sh` во внешнем tar |
| `server/routers/offsite.py` | раздел `kit` в статусе, `POST /offsite/kit`, вид `kit` в `/offsite/download` |
| `server/static/js/views/offsite.js` | строка «Комплект для установки», строка в баннере, скачивание и слежение |
| `restore.sh` (новый, корень) | восстановление одной командой; `RESTORE_LIB=1` — только функции |
| `selfheal.sh` | уважает замок восстановления |
| `server/offsite_restore.txt` | «БЫСТРЫЙ ПУТЬ», ветка `--kit` в ручных шагах |
| `.gitignore` | `offline_kit/` |
| `ui_tests/demo_kit.sh` (новый) | учебный маленький комплект для smoke/E2E на стенде (кладёт и убирает) |
| тесты | `server/tests/test_offline_kit.py`, `test_offline_kit_sh.py`, `test_restore_sh.py` (новые); `test_offsite_api.py`, `test_offsite_rules.py`, `test_offsite_copy_build.py`, `test_offsite_store.py`; `smoke_test.sh`, `ui_tests/ui_test.js` |

Тесты на bash-скрипты запускают их через `subprocess` из pytest: внутри `ds_api` корень проекта виден как
`/srv/project` (только чтение), в образе `ds-test` — как в существующих тестах копии. Путь к скрипту ищется так же,
как в `test_offsite_copy_build.py` (рядом с `server/`, затем `/srv/project`); нет скрипта — `pytest.skip` с
причиной. Поддельный `docker` — исполняемый bash-файл во временной папке, поставленный первым в `PATH`; он пишет
свои аргументы в журнал и отвечает заготовками.

---

### Task 1: Отпечаток программы — bash и Python одинаково

**Files:**
- Create: `offline_kit.sh` (пока только функция `kit_fingerprint` и подкоманда `fingerprint`)
- Create: `server/offline_kit.py` (пока `program_fingerprint`)
- Test: `server/tests/test_offline_kit.py`

**Interfaces:**
- Produces: `bash offline_kit.sh fingerprint DIR` → одна строка, 64 hex, код 0. При `source offline_kit.sh` CLI не
  запускается (проверка `[ "${BASH_SOURCE[0]}" = "$0" ]`), определена функция `kit_fingerprint DIR`.
- Produces: `offline_kit.program_fingerprint(project_dir: str) -> str` (64 hex).

Правило (одинаковое в обоих местах) — sha256 от потока байт:

```
для f в: server/Dockerfile, server/requirements.txt, ntp/Dockerfile, ntp/chrony.conf
    есть файл  → "== <f>\n" + байты файла как есть
    нет файла  → "== <f> MISSING\n"
затем "== images\n" + для каждой строки docker-compose.yml, совпавшей с
    ^[ \t]*image:[ \t]*["']?([^"' \t#]+)   →   группа 1 + "\n"   (в порядке файла)
```

- [ ] **Step 1: Write the failing tests** в `server/tests/test_offline_kit.py`:
  - `test_python_matches_bash` — фикстура-проект (все 4 файла + compose с тремя `image:`, один в кавычках, один с
    комментарием `# x`) → `program_fingerprint(dir) == subprocess(["bash", OFFLINE_KIT, "fingerprint", dir])`.
  - `test_fingerprint_changes_with_each_input` — параметр по каждому из 4 файлов и по строке `image:` →
    отпечаток другой.
  - `test_fingerprint_ignores_server_code_and_other_compose_lines` — правка `server/main.py`, `environment:` в
    compose → тот же.
  - `test_missing_file_is_part_of_the_rule` — нет `ntp/chrony.conf` → bash и Python совпали, отличается от
    варианта с пустым файлом.
  - `test_sourcing_runs_nothing` — `bash -c 'source offline_kit.sh; type kit_fingerprint'` → код 0, на stdout
    нет 64-hex строки.
- [ ] **Step 2: Run, verify FAIL**: `docker exec -w /app ds_api python -m pytest tests/test_offline_kit.py -q` —
  `ModuleNotFoundError: offline_kit`.
- [ ] **Step 3: Implement** `kit_fingerprint` (bash: `{ …; } | sha256sum | cut -d' ' -f1`, regex через
  `sed -nE`) и `program_fingerprint` (Python: `hashlib.sha256`, тот же regex через `re.MULTILINE`). Шапка
  `offline_kit.sh`: назначение, подкоманды, `set -uo pipefail` только в CLI-ветке (при `source` опции вызывающего
  не трогать).
- [ ] **Step 4: Run, verify PASS** (та же команда).
- [ ] **Step 5: Commit**: `git add offline_kit.sh server/offline_kit.py server/tests/test_offline_kit.py` →
  «Комплект: отпечаток программы — одно правило в bash и Python».

### Task 2: Постоянные имена образов и образ без кода

**Files:**
- Modify: `docker-compose.yml` (сервисы `api`, `celery`, `ntp`)
- Create: `server/.dockerignore`
- Test: `server/tests/test_offline_kit.py` (статические проверки конфигурации)

**Interfaces:**
- Produces: имена образов `ds-server:latest` (`api` и `celery`, у обоих остаётся `build: ./server`) и
  `ds-ntp:latest` (`ntp`, `build: ./ntp`). Эти строки входят в отпечаток (Task 1).

- [ ] **Step 1: Write the failing tests**:
  - `test_compose_built_services_have_fixed_images` — разбор `docker-compose.yml` regex-ом по блокам сервисов
    (PyYAML в зависимостях нет; путь к файлу — как `COMPOSE` в `test_offsite_store.py`): у `api` и `celery`
    `image: ds-server:latest`, у `ntp` — `ds-ntp:latest`; верхнеуровневого `name:` нет (имя проекта не задаётся).
  - `test_dockerignore_keeps_only_requirements` — `server/.dockerignore` первой значимой строкой исключает всё
    (`*`), затем `!requirements.txt`, и больше ничего не возвращает.
- [ ] **Step 2: Run, verify FAIL.**
- [ ] **Step 3: Implement**: строки `image:` с комментарием «постоянное имя: комплект для установки загружает образ
  под этим именем в любой папке; имя проекта не трогаем — от него зависят тома»; `server/.dockerignore` с
  комментарием, почему кода в образе нет (код берётся с хоста `./server:/app`; иначе каждая правка кода давала бы
  новый комплект).
- [ ] **Step 4: Run, verify PASS.**
- [ ] **Step 5: Проверка на стенде** (Docker вне песочницы, с одобрения): `docker compose -p dsdev -f
  docker-compose.yml -f docker-compose.dev.yml build` → собраны `ds-server:latest`, `ds-ntp:latest`;
  `docker run --rm ds-server:latest ls /app` → только `requirements.txt`; `bash dev_stand.sh up` → стенд
  поднялся, `bash smoke_test.sh` — 0 провалов. Затем
  `docker compose -p dsdev -f docker-compose.yml -f docker-compose.dev.yml up -d --no-build --pull never` — без
  ошибок и без скачивания. Записать в отчёт задачи, собирает ли compose образ один раз или дважды (дважды из кеша
  допустимо).
- [ ] **Step 6: Commit**: `docker-compose.yml server/.dockerignore server/tests/test_offline_kit.py` →
  «Образы: постоянные имена и без кода сервера внутри».

### Task 3: `offline_kit.sh save / verify / field / load`

**Files:**
- Modify: `offline_kit.sh`
- Test: `server/tests/test_offline_kit_sh.py` (поддельный `docker`)

**Interfaces:**
- Consumes: `kit_fingerprint` (Task 1); имена образов из compose (Task 2).
- Produces (CLI; вывод для человека — по-русски; последняя строка — машинная метка):
  - `save DIR` → код 0 и `KIT_SAVED <файл>` или `KIT_UNCHANGED <файл>`; код 2 и `KIT_WARN <причина>`.
  - `verify KIT DIR` → 0 подходит; 3 другая архитектура; 4 другой отпечаток; 5 файл повреждён/не комплект.
  - `field KIT KEY` → значение ключа из `KIT.txt` внутри tar (пусто, если нет); код 5, если не комплект.
  - `load KIT` → ставит модуль compose, если `docker compose version` не отвечает, затем `docker load`; 0 / 1.
  - `adopt KIT DIR` → копирует комплект в `DIR/offline_kit/` (если там не тот же файл), пишет сайдкар, удаляет
    прежний; код 0 и `KIT_ADOPTED <файл>`; код 2 и `KIT_WARN` при нехватке места.
  - Переменная `DOCKER` (по умолчанию `docker`) — префикс вызовов Docker (`restore.sh` передаёт `sudo docker`);
    `SUDO` (по умолчанию пусто) — префикс для записи модуля compose в системный каталог.
- Produces (форматы):
  - файл `DIR/offline_kit/ds_kit_ГГГГ-ММ-ДД_ЧЧММ_<версия8>.tar` (дата — часы хоста), внутри: `KIT.txt`,
    `images.tar.gz`, `docker-compose` (если модуль найден);
  - `KIT.txt` — строки `ключ=значение` после шапки-комментария: `format=1`, `version=<64hex>`,
    `fingerprint=<64hex>`, `arch=<uname -m>`, `created_ts=<unix>`, `host=<hostname>`, `docker=<версия Engine>`,
    `compose=docker-compose` или `compose=`, `images_sha256=`, `images_bytes=`, `compose_sha256=`, и по строке
    `image=<ref> <id>` на образ;
  - сайдкар `DIR/offline_kit/KIT.txt` — то же плюс `file=<имя tar>` и `bytes=<размер tar>`.
  - `version` = sha256 от отсортированных строк `<ref> <id>`; образы — `docker compose config --images` в DIR,
    ID — `docker image inspect -f '{{.Id}}'`; модуль — путь из
    `docker info --format '{{range .ClientInfo.Plugins}}{{if eq .Name "compose"}}{{.Path}}{{end}}{{end}}'`.

- [ ] **Step 1: Write the failing tests** (`test_offline_kit_sh.py`, поддельный `docker` отвечает на `compose
  config --images`, `image inspect`, `info`, `save` (пишет байты), `version`, `load` (пишет журнал)):
  - `test_save_writes_kit_and_sidecar` — после `save` в `offline_kit/` ровно один `ds_kit_*.tar` и `KIT.txt`;
    внутри tar три файла; `version`, `fingerprint`, `arch`, `images_sha256` верны; последняя строка `KIT_SAVED`.
  - `test_save_unchanged_images_is_skipped` — второй `save` без смены ID → `KIT_UNCHANGED`, `docker save` не
    вызывался, файл тот же.
  - `test_save_new_images_replaces_old` — ID поменялся → новый файл, старого нет, сайдкар новый.
  - `test_save_low_space_warns_and_keeps_old` — `KIT_MIN_FREE_KB` (переменная для теста, по умолчанию правило спеки)
    больше свободного → код 2, `KIT_WARN`, старый комплект на месте, временных файлов нет.
  - `test_save_docker_save_failure_warns_and_leaves_no_temp` — `docker save` падает → код 2, в `offline_kit/` нет
    `.tmp`/полуфайлов.
  - `test_save_without_compose_plugin` — `info` не знает плагин → комплект без `docker-compose`, `compose=`.
  - `test_verify_codes` — 0 / 3 (подменить `arch`) / 4 (правка `server/requirements.txt` в DIR) / 5 (обрезанный tar
    или испорченный `images.tar.gz` — sha256 не сошёлся).
  - `test_field_reads_kit_txt` — `field KIT version` = значение; несуществующий ключ — пусто, код 0.
  - `test_load_installs_compose_only_when_missing` — `docker compose version` отвечает → модуль не копируется;
    не отвечает → копируется в `${KIT_PLUGIN_DIR:-/usr/local/lib/docker/cli-plugins}/docker-compose`, права 755;
    затем вызван `docker load`.
  - `test_adopt_copies_kit_and_writes_sidecar` — после `adopt` сайдкар совпадает с тем, что написал бы `save`
    (те же ключи + `file`, `bytes`); повтор с тем же файлом — копирования нет.
- [ ] **Step 2: Run, verify FAIL.**
- [ ] **Step 3: Implement.** Запись — во временный файл в `offline_kit/` (`.ds_kit_tmp.*`), проверка
  `gzip -t images.tar.gz` и sha256 до замены, `mv` на место, затем удаление прежнего `ds_kit_*.tar`; `trap` на
  выходе удаляет временное. Порог места: свободно ≥ 2 × размер прежнего комплекта, без прежнего — 1,5 ГБ
  (`KIT_MIN_FREE_KB` перекрывает). `gzip -1` (на стенде ≈ 10 с на 760 МБ).
- [ ] **Step 4: Run, verify PASS.**
- [ ] **Step 5: Commit**: `offline_kit.sh server/tests/test_offline_kit_sh.py` → «Комплект: сохранение, проверка и
  загрузка образов».

### Task 4: `install_server.sh` — сохранение комплекта, `--kit`, `--no-kit`

**Files:**
- Modify: `install_server.sh`
- Modify: `.gitignore` (`offline_kit/`)
- Test: `server/tests/test_offline_kit_sh.py` (статические и с поддельным `docker`, где возможно)

**Interfaces:**
- Consumes: `offline_kit.sh save|verify|load` (Task 3).
- Produces: `bash install_server.sh [--env-only | --no-kit | --kit ФАЙЛ]`. С `--kit`: проверка комплекта (код
  `verify` ≠ 0 → ОСТАНОВ с его причиной), `load`, запуск `docker compose up -d --no-build --pull never`, далее как
  обычно; после успеха шаг «[4/4]» вызывает `offline_kit.sh adopt ФАЙЛ .` (а не `save`). Без `--kit` и без
  `--no-kit` — шаг «[4/4] Комплект для установки» после самопроверки только при `FAIL = 0`: `save`, код 2 →
  `warn` (⚠ + подсказка), не `bad`. С `--no-kit` шаг печатает «пропущено (--no-kit)».

- [ ] **Step 1: Write the failing tests**:
  - `test_install_server_syntax` — `bash -n install_server.sh` код 0.
  - `test_install_server_unknown_option_refused` — `bash install_server.sh --kitt` → код ≠ 0, текст «Неизвестный
    параметр» и список допустимых, **до** любых действий (поддельный `docker` не вызван, `.env` не создан —
    запуск во временной копии проекта).
  - `test_install_server_kit_missing_file` — `--kit /нет/файла` → ОСТАНОВ «Файл комплекта не найден», без вызовов
    `docker compose up`.
  - `test_gitignore_has_offline_kit` — строка `offline_kit/`.
- [ ] **Step 2: Run, verify FAIL.**
- [ ] **Step 3: Implement**: разбор параметров в начале (до preflight); в `[2/3]` ветка `--kit`; шаг `[4/4]` после
  самопроверки (нумерация шагов в выводе — `[0/4]…[4/4]`, итог печатается после шага 4); `--env-only` как был.
  Шапка скрипта описывает новые режимы.
- [ ] **Step 4: Run, verify PASS.** (Полный путь `install_server.sh` с настоящим Docker проверяют учения, Task 11.)
- [ ] **Step 5: Commit**: `install_server.sh .gitignore server/tests/test_offline_kit_sh.py` → «Установка:
  комплект после выкатки и установка из комплекта (--kit)».

### Task 5: Сервер — состояние комплекта, статус, скачивание

**Files:**
- Create: `server/migrations/036_offsite_kit.sql`; Modify: `docker-compose.yml` (initdb `36_offsite_kit.sql`)
- Modify: `server/offline_kit.py` (`read_kit`), `server/offsite_rules.py`, `server/offsite_store.py`,
  `server/routers/offsite.py`
- Test: `server/tests/test_offline_kit.py`, `test_offsite_rules.py`, `test_offsite_store.py`, `test_offsite_api.py`

**Interfaces:**
- Consumes: формат сайдкара `offline_kit/KIT.txt` (Task 3), `program_fingerprint` (Task 1).
- Produces:
  - SQL 036: `ALTER TABLE offsite_copy_state ADD COLUMN IF NOT EXISTS last_kit_at TIMESTAMP, … last_kit_by
    VARCHAR(64), … last_kit_file VARCHAR(128), … last_kit_version VARCHAR(64), … last_kit_bytes BIGINT;`
  - `offline_kit.KIT_DIR = "offline_kit"`; `@dataclass KitInfo(file: str, path: str, bytes: int, version: str,
    fingerprint: str, arch: str, created_at: datetime, compose: bool)` (`created_at` — наивное МСК из
    `created_ts`); `read_kit(project_dir: str) -> KitInfo | None` — None, если сайдкара нет, ключей не хватает,
    имя файла не `^ds_kit_\d{4}-\d{2}-\d{2}_\d{4}_[0-9a-f]{8}\.tar$`, файла нет или он не обычный, размер ≠ `bytes`.
  - `offsite_rules.kit_needs_download(available: bool, matches_program: bool, version: str | None,
    last_version: str | None) -> bool` — True, только если есть, подходит и `version != last_version`.
  - `offsite_rules.reminder_state(last_copy_at, new_media_count, now, kit_pending: bool = False) -> str` —
    `"ok"` только если копия сегодня, новых роликов 0 и `kit_pending` False.
  - `offsite_store._STATE_COLUMNS` + `last_kit_at, last_kit_by, last_kit_file, last_kit_version, last_kit_bytes`;
    `mark_kit_done(db, who: str, filename: str, version: str, size: int, now: datetime) -> None` (UPSERT как
    `mark_copy_done`).
  - `GET /offsite/status` → раздел `kit`: `available, file, bytes, version` (8 знаков), `created_at` (ISO),
    `arch, matches_program, last_at, last_by, last_version` (8 знаков), `current_downloaded` (полные версии
    равны); `state` учитывает `kit_needs_download(...)`. В БД `last_kit_version` — полные 64 знака.
  - `POST /offsite/kit` (суперадмин, тело пустое) → `{"url", "filename", "bytes", "version"}`; нет комплекта → 409
    `MSG_NO_KIT = "Комплекта для установки на сервере нет — он появится после выкатки (bash install_server.sh)"`;
    проект не подключён → 503 `MSG_NOT_MOUNTED`; тикет `{"kind": "kit", "filename", "version", "who"}`.
  - `GET /offsite/download` вид `kit`: перечитать `read_kit`; имя или версия не те → 404
    `MSG_KIT_CHANGED = "Комплект на сервере сменился — запросите скачивание заново"`; файл открыть сразу (до
    генератора), отдать потоком `CHUNK`, после последнего байта — `mark_kit_done` и журнал
    `"Скачан комплект для установки"` с подробностью `"версия <8>, <размер как _size_text>"`. Файл не удаляется.

- [ ] **Step 1: Write the failing tests**:
  - `test_offline_kit.py`: `test_read_kit_ok` (сайдкар + файл нужного размера → поля, `created_at` по МСК),
    `test_read_kit_none_cases` (параметризация: нет сайдкара, нет ключа, плохое имя `../x.tar`, файла нет,
    размер другой, симлинк вместо файла).
  - `test_offsite_rules.py`: `test_kit_needs_download_truth_table` (8 комбинаций),
    `test_reminder_ok_needs_kit_too`.
  - `test_offsite_store.py`: `test_mark_kit_done_sql_and_params` (как тест `mark_copy_done`),
    `test_migration_036_and_compose_line`.
  - `test_offsite_api.py` (фейк `World/Session._upsert` учит ветку `"last_kit_at" in q`):
    `test_status_kit_states` (А: есть, не скачан → `state=warn`; Б: скачан → `current_downloaded`; В: нет → `available
    false`, `state` от комплекта не зависит; не подходит к программе → `matches_program false`, `state` не warn из-за
    комплекта); `test_kit_download_marks_after_last_byte` (полный поток → отметка и журнал; содержимое байт в байт);
    `test_kit_download_interrupted_does_not_mark` (через `_read_part_then_drop`); `test_kit_post_409_without_kit`;
    `test_kit_changed_between_post_and_download_404`; `test_kit_ticket_single_use`; `POST /offsite/kit` добавить в
    `REQUESTS` (403 для не-суперадмина, 401 без входа).
- [ ] **Step 2: Run, verify FAIL**: `docker exec -w /app ds_api python -m pytest tests -q -k "kit or offsite"`.
- [ ] **Step 3: Implement** по Interfaces; в роутере — по образцу `_copy_response`/`_stream_copy`, но без удаления
  файла; `project_dir` — `store.PROJECT_DIR`.
- [ ] **Step 4: Run, verify PASS**, затем весь набор: `docker exec -w /app ds_api python -m pytest tests -q`.
- [ ] **Step 5: Commit**: миграция, compose, 4 модуля, тесты → «Копия на флешку: комплект для установки в
  статусе и скачивании».

### Task 6: Копия — `restore.sh` во внешнем tar, версия комплекта в описи, исключение `offline_kit/`

**Files:**
- Modify: `server/offsite_rules.py` (`_EXCLUDED_DIRS` + `"offline_kit"`), `server/offsite_store.py` (`build_copy`,
  `_manifest_text`)
- Test: `server/tests/test_offsite_rules.py`, `server/tests/test_offsite_copy_build.py`

**Interfaces:**
- Consumes: `read_kit` (Task 5).
- Produces: во внешнем tar копии — `restore.sh` из `PROJECT_DIR/restore.sh` (если файл есть; режим 0755; порядок:
  `ВОССТАНОВЛЕНИЕ.txt`, `restore.sh`, `copy.tar.gz.enc`, `.sha256`); нет файла — копия собирается без него и пишет
  в лог предупреждение. В `MANIFEST.txt` после строки «Миграция БД» — `Комплект:       версия <64hex> (<файл>)` или
  `Комплект:       нет на сервере`.

- [ ] **Step 1: Write the failing tests**: `test_offline_kit_dir_excluded` (`offline_kit/ds_kit_x.tar`,
  `offline_kit/KIT.txt` → исключены; `server/offline_kit.py` — нет); `test_outer_tar_has_restore_sh_executable`;
  `test_outer_tar_without_restore_sh_still_builds`; `test_manifest_has_kit_version` / `..._no_kit`.
- [ ] **Step 2: Run, verify FAIL.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run, verify PASS** (весь набор pytest).
- [ ] **Step 5: Commit** → «Копия на флешку: restore.sh в копии и версия комплекта в описи».

### Task 7: Панель — строка «Комплект для установки» и напоминание

**Files:**
- Modify: `server/static/js/views/offsite.js`
- Create: `ui_tests/demo_kit.sh`
- Test: `ui_tests/ui_test.js`, `ui_test.sh`, `smoke_test.sh`

**Interfaces:**
- Consumes: раздел `kit` статуса и `POST /offsite/kit` (Task 5).
- Produces: `data-action="offsite-kit"`; слежение `watch.kit` по `kit.last_at` (как `copy`); третья ячейка
  `.cell` в `#offsite-block` с заголовком «Комплект для установки» в состояниях А/Б/В и предупреждением при
  `matches_program === false`; строка в баннере по макету 2.

Тексты — ровно по макетам 1–2 (`docs/superpowers/specs/2026-11-09-offline-kit-restore-mockups.html`). Старый
сервер без раздела `kit`: строки нет, баннер как раньше (`sane()` поле `kit` не требует).

Учебный комплект для стенда: `ui_tests/demo_kit.sh` (новый) — функции `demo_kit_put DIR` (если в
`DIR/offline_kit/` нет настоящего комплекта — кладёт маленький `ds_kit_…_<8>.tar` с `KIT.txt` по формату Task 3,
отпечатком текущей программы и сайдкаром; печатает `DEMO_KIT_PUT` или `DEMO_KIT_SKIP`) и `demo_kit_remove DIR`
(удаляет только то, что положил сам). Подключают `smoke_test.sh` и `ui_test.sh` — только под своими флагами, после
существующей проверки «это стенд dsdev».

- [ ] **Step 1: Write the failing E2E checks** в `ui_test.js` (раздел «Копия на флешку»): ячеек `.oc-head` — 3;
  текст третьей содержит «Комплект для установки»; без комплекта — состояние В (кнопка `disabled`, «Комплект ещё
  не сохранён»). С `E2E_ALLOW_OFFSITE=1` (`ui_test.sh` кладёт учебный комплект до прогона и убирает после) —
  состояние А и строка в баннере дашборда; клик `offsite-kit`, ожидание `kit.current_downloaded` через
  `GET /offsite/status`; строка — «— эта версия ✓», в баннере строки про комплект нет.
  Smoke (под `SMOKE_ALLOW_OFFSITE=1`, учебный комплект): `POST /offsite/kit` → скачать, `tar -tf` содержит
  `KIT.txt`, размер = `bytes`, статус `current_downloaded=true`, повтор той же ссылки — 404; без флага — строка
  «– комплект — пропущено (задайте SMOKE_ALLOW_OFFSITE=1 на стенде)».
- [ ] **Step 2: Run, verify FAIL**: `bash ui_test.sh` (стенд), `bash smoke_test.sh`.
- [ ] **Step 3: Implement** (`blockContent`, `bannerContent`, `downloadKit()`, ветка в обработчике действий,
  `watch.kit` в `pollTick`; кнопка занята, пока идёт слежение).
- [ ] **Step 4: Run, verify PASS**: `node --check server/static/js/views/offsite.js`; `bash ui_test.sh` и
  `E2E_ALLOW_OFFSITE=1 bash ui_test.sh`; `bash smoke_test.sh` и `SMOKE_ALLOW_OFFSITE=1 bash smoke_test.sh` — 0
  провалов; записать числа проверок.
- [ ] **Step 5: Commit** → «Панель: комплект для установки в Настройках и на дашборде».

### Task 8: `restore.sh` — восстановление на чистой машине

**Files:**
- Create: `restore.sh`
- Test: `server/tests/test_restore_sh.py`

**Interfaces:**
- Consumes: `offline_kit.sh verify|field|load` из папки программы **из копии**; `install_server.sh --kit`;
  формат MANIFEST (строки «Адрес сервера:», «Комплект:», контрольные числа `  <key>: <n>    (<подпись>)`).
- Produces (функции при `RESTORE_LIB=1 source restore.sh`; основной ход — без этой переменной):
  - `rs_list_copies DIR` → полные пути `ds_copy_*.tar`, самые свежие первыми (по имени).
  - `rs_manifest_host FILE`, `rs_manifest_kit FILE` (64 hex или пусто), `rs_manifest_counts FILE` (`key n` строки).
  - `rs_counts_diff EXPECTED ACTUAL` → строки «ключ: ожидалось N, в базе M»; код 0 — совпало.
  - `rs_pick_kit FLASH_DIR PROGRAM_DIR WANT_VERSION ARCH` → путь или пусто: среди `ds_kit_*.tar` с `verify` = 0
    — с `version = WANT_VERSION`, иначе самый свежий по `created_ts`.
  - `rs_docker_prefix` → `""` (docker отвечает), `"sudo"` (permission denied); служба не запущена — запускает
    (`sudo systemctl start docker`, если нет systemd — `sudo service docker start`).
  - `rs_decrypt ENC OUT` — пароль читается `read -rs`, передаётся `openssl … -pass fd:3` через `printf` в
    подстановку процесса; параметры ровно `OPENSSL_DECRYPT_CMD`; до 3 попыток при `bad decrypt`.
  - `rs_install_docker` — `sudo apt-get install -y docker.io`, служба, группа `docker` (`usermod -aG`, с
    сообщением «на будущее»).
  - `rs_zps_on` → код 0, если ЗПС Astra включена (есть `astra-digsig-control` и он сообщает «включено»), иначе 1;
    включена → ОСТАНОВ до любых изменений с объяснением (спека §5.7).
  - Константы: `RS_WORK=~/ds_restore`, лог `~/ds_restore.log` (без паролей), целевая папка на чистой машине
    `~/digital-signage`, метка своей установки `<программа>/.restore_in_progress`.

Ход — спека §5.5, пп. 1–9, макет 3 (тексты строк — по макету). Старые копии без `offline_kit.sh` в программе —
сразу ветка сборки.

- [ ] **Step 1: Write the failing tests** (`test_restore_sh.py`; функции вызываются через
  `bash -c 'RESTORE_LIB=1 source restore.sh; rs_… "$@"' _ args`):
  - `test_restore_syntax` (`bash -n`), `test_lib_mode_runs_nothing`.
  - `test_list_copies_newest_first`.
  - `test_manifest_parsing` — на MANIFEST, собранном настоящим `_manifest_text` (импорт из `offsite_store`).
  - `test_counts_diff_equal_and_mismatch`.
  - `test_pick_kit_prefers_manifest_version`, `test_pick_kit_falls_back_to_newest_matching`,
    `test_pick_kit_none_when_nothing_matches` (поддельный `offline_kit.sh` в папке программы).
  - `test_docker_prefix_plain`, `test_docker_prefix_permission_denied_uses_sudo` (поддельные `docker`/`sudo`).
  - `test_zps_on_and_off` (поддельный `astra-digsig-control`; нет утилиты → 1).
  - `test_restore_decrypt_special_password` — копия шифруется `encrypt_file_openssl` паролем
    ``a!b$c"d'e\f`g#h`` → `rs_decrypt` с паролем на stdin расшифровывает байт в байт; во время работы `ps` не
    показывает пароль (поддельный `openssl`-обёртка пишет свои argv в журнал — пароля там нет).
  - `test_restore_decrypt_params_match_offsite_crypto` — строка параметров в `restore.sh` содержит
    `OPENSSL_DECRYPT_CMD` без `-in/-out`.
  - `test_restore_paths_with_spaces_and_cyrillic` — `rs_list_copies` и `rs_pick_kit` на папке
    `МОЯ ФЛЕШКА/с пробелом`.
- [ ] **Step 2: Run, verify FAIL.**
- [ ] **Step 3: Implement** `restore.sh` (шапка: что делает, что спросит, как запускать, `RESTORE_LIB`).
- [ ] **Step 4: Run, verify PASS** (весь pytest).
- [ ] **Step 5: Commit** → «Восстановление одной командой: restore.sh для чистой машины».

### Task 9: Поверх старой установки — «ДА», прежнее состояние, `rollback.sh`, замок

**Files:**
- Modify: `restore.sh`, `selfheal.sh`
- Test: `server/tests/test_restore_sh.py`

**Interfaces:**
- Consumes: функции Task 8.
- Produces:
  - `rs_find_install` → папка установки из метки `com.docker.compose.project.working_dir` контейнера `ds_api`
    (запущен или нет), иначе `~/digital-signage`, если там `docker-compose.yml`; плюс имя проекта из метки
    `com.docker.compose.project`.
  - `rs_is_own_half_install DIR` — есть `DIR/.restore_in_progress` → 0 (в метке — путь к `ds_before_restore_*`,
    если был).
  - Папка `~/ds_before_restore_ГГГГ-ММ-ДД_ЧЧММ/` (права 700): `program/` (перенесённая папка установки),
    `db_volume.tar.gz` (файлы тома PostgreSQL при остановленной базе, через `docker run --rm` из образа
    контейнера `ds_postgres`), `images.txt` (`<ref> <id>` прежних образов + метки `ds-before-restore/…`),
    `project.txt` (имя проекта и исходная папка), `rollback.sh`.
  - Замок `/run/lock/ds-restore.lock` (нет `/run/lock` — `/tmp/ds-restore.lock`) с PID; `selfheal.sh` в начале:
    замок есть и PID жив → «Идёт восстановление (restore.sh) — проверка пропущена», код 0.
  - Порядок до «ДА»: только чтение. После «ДА»: замок → перенос папки программы (cron-`selfheal.sh` старой версии
    по старому пути больше не запускается) → `docker compose -p <проект> -f <before>/program/docker-compose.yml
    stop` → сохранение тома → метки образов → дальше Task 8 в **ту же** исходную папку.
  - Сбой после «ДА» — останов (§4.11): шаг, причина, две готовые команды — `bash <флешка>/restore.sh` и
    `bash <before>/rollback.sh`.
  - `rollback.sh`: «ДА»; замок; `compose down` (без `-v`) текущей; текущую папку — в
    `<исходная>.after_restore_<ЧЧММ>`; `program/` — на место; метки образов назад; том PostgreSQL — очистить и
    распаковать сохранённый; `compose up -d` (без сборки); самопроверка `/health` и 7 контейнеров; «✅ ВЕРНУЛ КАК
    БЫЛО» или останов с причиной.

- [ ] **Step 1: Write the failing tests**:
  - `test_find_install_from_container_label` (поддельный `docker inspect`), `test_find_install_none`.
  - `test_own_half_install_skips_yes_and_snapshot` — main с поддельными docker/openssl доходит до установки без
    вопроса «ДА» (ввод на stdin без «ДА» не нужен) и без `ds_before_restore_*`.
  - `test_nothing_changes_before_yes` — на вопрос «ДА» ответить «нет» → папка программы на месте, поддельный
    `docker` не получил ни `stop`, ни `run`, ни `tag`.
  - `test_before_dirs_never_overwritten` — существующая `ds_before_restore_*` с той же минутой → создаётся с
    суффиксом, старая байт в байт.
  - `test_low_space_stops_before_yes` — оценка места больше свободного (переменная для теста) → ОСТАНОВ до вопроса.
  - `test_failure_after_yes_prints_two_commands` — сбой на шаге базы → в выводе обе команды, код ≠ 0.
  - `test_rollback_sh_generated_and_valid` — `bash -n`, права 700 у папки, в тексте нет паролей из `.env`.
  - `test_selfheal_respects_lock` / `test_selfheal_ignores_stale_lock` (PID мёртв).
- [ ] **Step 2: Run, verify FAIL.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run, verify PASS** (весь pytest).
- [ ] **Step 5: Commit** → «Восстановление поверх старой установки: ДА, прежнее состояние и rollback.sh».

### Task 10: Инструкция в копии — «БЫСТРЫЙ ПУТЬ» и ветка с комплектом

**Files:**
- Modify: `server/offsite_restore.txt`
- Test: `server/tests/test_offsite_copy_build.py` (шаблон)

**Interfaces:**
- Consumes: строка запуска из Task 8; `install_server.sh --kit` (Task 4).
- Produces: в начале инструкции раздел «БЫСТРЫЙ ПУТЬ: ОДНА КОМАНДА» — что понадобится (флешка: копия, все
  `ds_media_*.tar`, комплект `ds_kit_*.tar`; пароль на копию; машина с Astra и интернетом), строка
  `tar -xf {{copy_file}} restore.sh && bash restore.sh`, что спросит, чем закончится, как вернуть
  (`rollback.sh`); ниже — прежние шаги «ЗАПАСНОЙ ПУТЬ: ВРУЧНУЮ», где шаг 1 — Docker из репозитория Astra
  (`sudo apt install docker.io`), шаг 3 — «если на флешке есть `ds_kit_*.tar`: `bash install_server.sh --kit
  "ПУТЬ_К_ФЛЕШКЕ/ds_kit_….tar"`», иначе как сейчас.

- [ ] **Step 1: Write the failing tests**: `test_instruction_fast_path_first` (раздел быстрого пути раньше «ШАГ 1»,
  в нём строка запуска с подставленным именем копии); `test_instruction_manual_kit_branch`; существующие тесты
  шаблона (параметры расшифровки, ON_ERROR_STOP) остаются зелёными.
- [ ] **Step 2: Run, verify FAIL.**
- [ ] **Step 3: Implement** (стиль и тон нынешней инструкции; ширина строк ≤ 78).
- [ ] **Step 4: Run, verify PASS.**
- [ ] **Step 5: Commit** → «Инструкция в копии: быстрый путь одной командой и установка из комплекта».

### Task 11: Учения на стенде

Не код — проверка. Docker вне песочницы, с одобрения пользователя. Всё рабочее — в scratchpad; в репозиторий —
только исправления найденного (каждое — своим коммитом с тестом) и рецепт в `04_ТЕСТЫ.md` (Task 12).

**Сборка «машин»:** образ учений на основе `debian:bullseye` с `docker.io` (Engine 20.10), `openssl`, `curl`,
`sudo`, пользователь `user` с sudo без пароля; `dockerd` поднимается в привилегированном контейнере (как именно —
решает исполнитель; в отчёте записать). Модуль compose v2 для «сервера» — Linux-сборка нужной архитектуры
(`aarch64` на Mac). Внутренние тома `/var/lib/docker` — свои у каждой «машины».

- [ ] **Step 1: «Сервер»**: машина с интернетом, копия проекта (без `.env` из worktree), `bash install_server.sh`
  → итог ✅ и `KIT_SAVED`; повтор → `KIT_UNCHANGED`, время шага < 30 с. Наполнить данными через API (рекламодатель,
  ролики 3 шт, «Документы» 1, пользователь, плейлист), задать пароль на копию со спецзнаками (Review Focus 1),
  скачать через панель/API (nginx «сервера», порт наружу на loopback) копию, ролики «все» и комплект в папку
  «флешка» с пробелом и кириллицей в имени (Review Focus 2). Записать sha256/md5 эталонов.
- [ ] **Step 2: «Чистая машина без интернета»**: сеть `--internal`, Docker уже стоит, флешка подключена. Одна
  строка из инструкции → «✅ ВСЁ ВОССТАНОВЛЕНО»; числа = опись; md5 роликов/документов/OTA совпали; вход прежним
  паролем admin — 200; 7 контейнеров; `docker pull alpine` внутри падает (сети нет) — доказательство; в логе
  нет пароля на копию и значений `.env`; `~/ds_restore` удалён; `offline_kit/` нового сервера содержит комплект.
- [ ] **Step 3: Поверх старой установки** (та же машина): через API добавить ролик и пользователя → запустить
  строку, выбрать **предыдущую** копию, «ДА» → их нет, числа = опись выбранной копии; `rollback.sh` → «ВЕРНУЛ КАК
  БЫЛО», ролик и пользователь на месте, вход работает. Обрыв посередине (kill после «ДА» на шаге базы) → повтор
  строки проходит, прежняя `ds_before_restore_*` не тронута (Review Focus 5).
- [ ] **Step 4: Без комплекта**: убрать `ds_kit_*.tar` с флешки; без интернета → понятный ОСТАНОВ; с интернетом →
  сборка и восстановление проходят, строка «подходящего комплекта нет — собираю из интернета».
- [ ] **Step 5: Установка Docker скриптом**: та же основа без Docker, с доступом к репозиторию Debian → скрипт
  ставит `docker.io`, модуль compose — из комплекта, пользователь не в группе → работа через `sudo`
  (Review Focus 4); восстановление проходит.
- [ ] **Step 6: Отказы `--kit`**: подменённый `arch` в описи и правка `server/requirements.txt` в программе →
  понятные отказы и переход на сборку в `restore.sh`.
- [ ] **Step 7: Уборка**: все контейнеры, тома и образы учений удалены (список — в отчёт; удаление томов — с
  одобрения), стенд `dsdev` поднят, пароль на копию стенда сброшен, папка «флешки» с `.env` удалена.
- [ ] **Step 8: Отчёт учений** в заметках сессии (папка заметок группы): что прошло, что найдено, что исправлено,
  что учения не покрывают (спека §10).

### Task 12: Документы и числа

**Files:** `docs/RESTORE_GUIDE.md`, `docs/Инструкции/01_УСТАНОВКА_СЕРВЕР.md`, `03_РОЛЬ_АДМИНИСТРАТОР.md`,
`08_ОБНОВЛЕНИЕ_СЕРВЕРА.md`, `docs/Техдокументация/01_АРХИТЕКТУРА.md`, `02_ПОРТЫ_И_СЕРВИСЫ.md` (если меняется),
`03_RUNBOOK_ИНЦИДЕНТЫ.md`, `04_ТЕСТЫ.md`, `README.md`, `CLAUDE.md`.

- [ ] **Step 1: Обновить** по спеке §9: Docker на Astra и `--kit` (01); строка комплекта и напоминание (03);
  шаг комплекта при выкатке, ≈ +1 ГБ на диске, «скачайте новый комплект», уборка старых образов
  `<проект>-api` после перехода на постоянные имена (`docker image ls`, `docker image rm`) (08); `offline_kit/`,
  имена образов, раздел `kit` статуса, `restore.sh`/`rollback.sh`, замок (01_АРХИТЕКТУРА); инциденты: комплект
  не сохранился / не подходит / восстановление остановилось / как вернуть / замок остался (runbook); рецепт учений
  и числа тестов (04_ТЕСТЫ); структура и миграции `001`…`036`, правило «имена образов постоянные, имя проекта не
  менять» (CLAUDE.md — только стабильное).
- [ ] **Step 2: Измерить числа**: `docker exec -w /app ds_api python -m pytest tests -q` (и в `ds-test`, как в
  прошлой работе); smoke без флагов и с `SMOKE_ALLOW_BROADCAST=1 SMOKE_ALLOW_OFFSITE=1`; E2E без флагов и со всеми
  флагами — вписать измеренные значения.
- [ ] **Step 3: Проверка дрейфа**: `grep -rn "001.\{1,3\}035\|миграци.*035" docs README.md CLAUDE.md` — пусто.
- [ ] **Step 4: Commit** (явные пути) → «Документы: комплект для установки и восстановление одной командой».

### Финал

- [ ] Обзор всей ветки (свежий проверяющий, самая сильная модель) против спеки; исправления — коммитами.
- [ ] Отчёт пользователю: что сделано, измеренные числа, что учения не покрыли (спека §10), варианты слияния
  (superpowers:finishing-a-development-branch); без пуша и выкатки без согласия.
