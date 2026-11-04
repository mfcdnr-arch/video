# Копия на флешку — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** суперадмин скачивает из панели запертую паролем ежедневную копию сервера (база, `.env`, документы,
OTA, программа, инструкция) и архивы роликов («новые»/«все»), а дашборд каждый день напоминает это сделать.

**Architecture:** чистая логика (правило напоминания, имена файлов, проверка пароля, исключения программы,
потоковый tar, шифрование в формате OpenSSL) — в отдельных модулях без БД; SQL состояния, сборка копии,
одноразовые ссылки (Redis) — в `offsite_store.py`; HTTP — новый роутер `routers/offsite.py`. Оба скачивания
идут по одноразовой 2-минутной ссылке, выданной авторизованным POST: браузер качает прямо на диск.
Панель — новый `views/offsite.js` (блок Настроек, напоминание дашборда, окна).

**Tech Stack:** Python 3.11, FastAPI, SQLAlchemy `text()`, PostgreSQL 15, Redis 7 (redis-py 5, `getdel`),
`cryptography` (уже стоит через `python-jose[cryptography]`), `tarfile` (PAX), vanilla JS, nginx, puppeteer (E2E).

**Spec:** `docs/superpowers/specs/2026-11-03-offsite-copy-design.md`; макеты —
`docs/superpowers/specs/2026-11-03-offsite-copy-mockups.html`. Исполнитель читает оба.

## Global Constraints

- Ветка `claude/offsite-copy` (worktree `.claude/worktrees/offsite-copy`), основана на `claude/secure-downloads`
  (f6901ff). В копию `nice-khayyam-367ec6` не писать. Не пушить, не выкатывать на 10.0.119.100.
- Всё, что видит пользователь (панель, ошибки API, инструкция, журнал), — по-русски.
- Все моменты — наивное МСК из Python: `from placements_store import now_msk`; не `NOW()` Postgres.
- Доступ ко всем `/offsite/*`, кроме `GET /offsite/download`, — только `require_superadmin` (`deps.py`).
- Пароли (входа, на копию) никогда не пишутся в журнал, логи, ответы API, адрес запроса.
- Пароль на копию: не короче 10 символов, только печатные ASCII (латиница, цифры, знаки `!`…`~`) —
  его должно быть можно набрать в консоли любой новой машины. Хранится зашифрованным ключом из `SECRET_KEY`.
- Шифрование копии — формат `openssl enc -aes-256-cbc -pbkdf2 -md sha256 -iter 200000` (`Salted__` + соль 8 байт).
- Одноразовая ссылка: живёт 120 секунд, срабатывает один раз (`GETDEL`), путь `/api/offsite/download?t=<токен>`
  (токен — `secrets.token_urlsafe(32)`).
- «Скачано» (копия, водяной знак роликов) отмечается только после того, как поток отдан целиком.
- Копии для флешки на сервере не хранятся: временная папка `/tmp/ds_offsite/` удаляется после отдачи,
  неоткрытые — через 15 минут.
- Ролики «Документы» (`media.status='document'`) — только в запертой ежедневной копии, не в архивах роликов.
- Новая миграция `035_offsite_copy.sql` — и в список initdb `docker-compose.yml` (`35_offsite_copy.sql`).
- Комментарии в коде — по-русски, в духе соседнего кода (зачем, а не что).
- pytest: `docker run --rm -v "<wt>/server":/app -v "<wt>/agent":/agent:ro -w /app ds-test:py311 python -m pytest tests -q -p no:cacheprovider`
  (`<wt>` — абсолютный путь worktree; переменные в команде docker не использовать, писать путь целиком).
  Исходно 894 passed.
- Стенд `dsdev` общий для сессий (контейнеры `ds_*`). Перед `bash dev_stand.sh reset|up` из этого worktree
  убедиться у управляющей сессии, что стенд никто не занял. Пароль admin — только как
  `ADMIN_PASS=$(cat .dev_admin_pass)` внутри команды, никогда не печатать.

## Review Focus

1. Скачивание роликов оборвали на середине (закрыли вкладку) → водяной знак и «последнее скачивание»
   не меняются, в следующий раз эти ролики снова «новые». Тест — Задача 5.
2. Ролик загрузили, пока шло скачивание (его `id` > `upto_id`) → он остаётся «новым» после завершения.
   Тест — Задача 3 (`mark_media_done`) и Задача 5.
3. У строки `media` нет файла на диске → архив всё равно собирается, `Content-Length` совпадает с телом,
   в манифесте «нет файла». Тест — Задача 5.
4. Сменили `SECRET_KEY` → сохранённый пароль не расшифровать → статус `needs_reset`, копия — 409,
   задать новый можно без текущего. Тест — Задача 5.
5. Длинные (>100 байт) и кириллические имена файлов в tar → GNU tar / `tarfile` читают их без потерь.
   Тест — Задача 2; на настоящем `tar` — учения в Задаче 9.

---

### Task 1: Шифрование копии и хранение пароля на копию

**Files:**
- Create: `server/offsite_crypto.py`
- Test: `server/tests/test_offsite_crypto.py`

**Interfaces:**
- Produces:
  - `OPENSSL_ITER: int = 200_000`
  - `OPENSSL_DECRYPT_CMD: str` — ровно
    `"openssl enc -d -aes-256-cbc -pbkdf2 -md sha256 -iter 200000 -in copy.tar.gz.enc -out copy.tar.gz"`
  - `encrypt_file_openssl(src_path: str, dst_path: str, password: str, *, salt: bytes | None = None) -> int` —
    пишет `b"Salted__" + salt + шифротекст` (PBKDF2-HMAC-SHA256, 48 байт → ключ 32 + IV 16, AES-256-CBC,
    PKCS7), читает и пишет кусками по 1 МиБ, возвращает размер результата.
  - `seal_secret(plain: str, secret_key: str) -> str` и `open_secret(token: str, secret_key: str) -> str | None` —
    Fernet, ключ `urlsafe_b64encode(sha256(b"ds-offsite-copy:" + secret_key.encode()).digest())`;
    `open_secret` на чужом ключе или мусоре возвращает `None`, не бросает.

- [ ] **Step 1: Написать падающие тесты**

```python
def test_openssl_decrypts_our_file(tmp_path):        # 3 МБ случайных байт + хвост не кратный 16
    # encrypt_file_openssl(src, enc, "Copy-pass-2026!") → subprocess: openssl enc -d … -pass env:PW
    assert decrypted == original
def test_wrong_password_does_not_decrypt(tmp_path):  # openssl код ≠ 0 или байты ≠ original
def test_header_and_size(tmp_path):                  # начинается с b"Salted__"; size == 16 + (n // 16 + 1) * 16
def test_fixed_salt_is_deterministic(tmp_path):      # одинаковая соль → одинаковый файл
def test_seal_open_roundtrip():                      # open_secret(seal_secret("Abc-123-xyz", K), K) == "Abc-123-xyz"
def test_open_with_other_key_is_none():              # другой SECRET_KEY → None
def test_open_garbage_is_none():                     # open_secret("not-a-token", K) is None
```

Расшифровка — настоящим `openssl` из образа `ds-test:py311` (OpenSSL 3.5) командой из `OPENSSL_DECRYPT_CMD`
с `-pass env:PW`. Если `shutil.which("openssl")` пуст — `pytest.skip` с пояснением.

- [ ] **Step 2: Запустить — тесты падают (`ModuleNotFoundError: offsite_crypto`)**
- [ ] **Step 3: Реализовать `server/offsite_crypto.py`** (`cryptography.hazmat`: `PBKDF2HMAC`, `Cipher(AES, CBC)`,
  `padding.PKCS7(128)`; `cryptography.fernet.Fernet`)
- [ ] **Step 4: Запустить pytest целиком — зелёный**
- [ ] **Step 5: Commit** — `git add server/offsite_crypto.py server/tests/test_offsite_crypto.py`,
  сообщение «Сервер: шифрование копии на флешку (формат openssl) и хранение пароля на копию»

---

### Task 2: Чистые правила и потоковый tar

**Files:**
- Create: `server/offsite_rules.py`, `server/offsite_tar.py`
- Test: `server/tests/test_offsite_rules.py`, `server/tests/test_offsite_tar.py`

**Interfaces:**
- Produces (`offsite_rules.py`):
  - `COPY_PASSWORD_MIN = 10`
  - `copy_password_problem(pw: str) -> str | None` — `None`, если подходит; иначе ровно
    `"Пароль на копию — не короче 10 символов"` или
    `"Только латинские буквы, цифры и знаки: такой пароль можно набрать в консоли любой машины"`.
  - `reminder_state(last_copy_at: datetime | None, new_media_count: int, now: datetime) -> str` —
    `"ok"`, если `last_copy_at` есть, `last_copy_at.date() == now.date()` и `new_media_count == 0`; иначе `"warn"`.
  - `is_program_excluded(relpath: str) -> bool` — `relpath` относительно корня проекта, разделитель `/`.
    Исключено, если любой компонент пути в `{".git", ".claude", ".codex", ".superpowers", "backups",
    "__pycache__", "node_modules"}`, имя файла в `{".dev_admin_pass", ".DS_Store"}` или оканчивается на `.log`,
    либо это файл в корне (без `/`) с окончанием `.tar`, `.tar.gz`, `.tgz`, `.zip`. `.env` НЕ исключается.
  - `copy_filename(now: datetime) -> str` → `"ds_copy_2026-11-03_0915.tar"`.
  - `media_filename(scope: str, now: datetime, after_id: int, upto_id: int) -> str` →
    `"ds_media_new_2026-11-03_0915_id143-160.tar"` (от `after_id + 1` до `upto_id`) или
    `"ds_media_all_2026-11-03_0915.tar"`.
  - `safe_host(host: str | None) -> str` — оставляет `[A-Za-z0-9.-]`, отрезает порт; пусто → `"адрес сервера"`.
- Produces (`offsite_tar.py`):
  - `@dataclass class TarItem: arcname: str; size: int; path: str | None = None; data: bytes | None = None;
    mtime: int = 0; mode: int = 0o644`
  - `tar_size(items: list[TarItem]) -> int` — точный размер того, что отдаст `iter_tar`.
  - `iter_tar(items: list[TarItem], chunk: int = 1 << 20) -> Iterator[bytes]` — заголовки
    `tarfile.TarInfo.tobuf(tarfile.PAX_FORMAT, "utf-8", "surrogateescape")`, данные из `path` или `data`,
    добивка до 512, в конце два нулевых блока и добивка до `tarfile.RECORDSIZE`. Если из `path` прочиталось
    не `size` байт — `RuntimeError` (иначе архив тихо битый).
  - `write_tar(items: list[TarItem], dst_path: str) -> int` — то же в файл, возвращает размер.

- [ ] **Step 1: Написать падающие тесты**

```python
# test_offsite_rules.py
def test_password_too_short():         assert copy_password_problem("Abc-12345") == "Пароль на копию — не короче 10 символов"
def test_password_cyrillic_refused():  assert copy_password_problem("пароль-на-копию") .startswith("Только латинские")
def test_password_space_refused():     # пробел и табуляция — не печатные ASCII-знаки для консоли → отказ
def test_password_ok():                assert copy_password_problem("Copy-pass-2026!") is None
def test_reminder_ok_today_no_media(): assert reminder_state(datetime(2026,11,3,9,15), 0, datetime(2026,11,3,18,0)) == "ok"
def test_reminder_warn_yesterday():    # 2026-11-02 23:59 при now 2026-11-03 00:01 → "warn"
def test_reminder_warn_new_media():    # сегодня, но new_media_count=3 → "warn"
def test_reminder_warn_never():        assert reminder_state(None, 0, datetime(2026,11,3,9,0)) == "warn"
@pytest.mark.parametrize("p,ex", [(".env", False), ("server/main.py", False), (".git", True),
    (".git/config", True), (".claude/settings.json", True), ("server/__pycache__/x.pyc", True),
    ("install_server.log", True), ("ui_tests/node_modules/a.js", True), ("ds_server_V2_x.tar.gz", True),
    ("docs/a.zip", False), (".dev_admin_pass", True), ("backups/b.sql.gz", True)])
def test_program_excluded(p, ex):      assert is_program_excluded(p) is ex
def test_copy_filename():              assert copy_filename(datetime(2026,11,3,9,15)) == "ds_copy_2026-11-03_0915.tar"
def test_media_filename_new_all():     # "ds_media_new_2026-11-03_0915_id143-160.tar" / "ds_media_all_2026-11-03_0915.tar"
def test_safe_host():                  # "10.0.119.100:80" → "10.0.119.100"; "<script>" → "script"; None → "адрес сервера"

# test_offsite_tar.py
def test_size_matches_stream_and_tarfile_reads(tmp_path):  # 3 файла (0 байт, 511, 1 МБ+1) + data-элемент
    # len(b"".join(iter_tar(items))) == tar_size(items); tarfile.open(fileobj=…) → имена, содержимое, mode
def test_long_and_cyrillic_names(tmp_path):  # "media/" + "Ролик_" * 30 + ".mp4" (>100 байт) читается тем же именем
def test_write_tar_equals_iter(tmp_path):    # write_tar → файл == b"".join(iter_tar)
def test_shrunk_file_raises(tmp_path):       # size больше реального файла → RuntimeError
```

- [ ] **Step 2: Запустить — тесты падают**
- [ ] **Step 3: Реализовать оба модуля** (никакого SQL и `os.environ` в них)
- [ ] **Step 4: pytest целиком — зелёный**
- [ ] **Step 5: Commit** — «Сервер: правила копии на флешку и потоковый tar»

---

### Task 3: Состояние на сервере, ролики, журнал, одноразовые ссылки

**Files:**
- Create: `server/migrations/035_offsite_copy.sql`, `server/offsite_store.py`
- Modify: `docker-compose.yml` (initdb: строка `- ./server/migrations/035_offsite_copy.sql:/docker-entrypoint-initdb.d/35_offsite_copy.sql:ro`
  после 034)
- Test: `server/tests/test_offsite_store.py`

**Interfaces:**
- Consumes: `now_msk` (`placements_store`).
- Produces (`offsite_store.py`):
  - `load_state(db) -> dict` — ключи ровно как колонки миграции (ниже), без `id`; нет строки → все `None`,
    `media_watermark = 0`.
  - `save_password(db, enc: str, who: str, now: datetime) -> None`; `clear_password(db) -> None`.
  - `mark_copy_done(db, who: str, filename: str, size: int, now: datetime) -> None`.
  - `mark_media_done(db, who: str, scope: str, upto_id: int, count: int, size: int, now: datetime) -> None` —
    `media_watermark = GREATEST(media_watermark, :upto)`.
  - `media_rows(db, after_id: int, upto_id: int) -> list` — строки `id, title, filename, filesize, md5_hash`
    из `media` с `after_id < id <= upto_id` и `status <> 'document'`, по `id`.
  - `media_totals(db, after_id: int) -> dict` — `{"new_count", "new_bytes", "all_count", "all_bytes", "max_id"}`
    (без `status='document'`; `filesize` NULL считать 0).
  - `document_rows(db) -> list` — `id, title, filename, filesize` с `status='document'`.
  - `audit(db, title: str, detail: str, who: str) -> None` — `INSERT INTO audit_log (event_type, title, detail, actor)`
    с `event_type='backup'`.
  - `check_login_password(db, username: str, password: str) -> bool` — `pwd_context.verify` по `users.password_hash`;
    нет пользователя / пустой пароль → `False`.
  - `issue_ticket(payload: dict, ttl: int = 120) -> str` и `take_ticket(token: str) -> dict | None` — Redis
    (`REDIS_URL`, ключ `offsite:ticket:<token>`, JSON, `set(ex=ttl)` / `getdel`). Клиент — через
    модульную функцию `_redis()`, чтобы тесты подменяли её словарём-заглушкой.

Миграция (решение плана, писать как есть):

```sql
-- 035: «Копия на флешку» — что и когда скачивали, пароль на копию (зашифрован ключом из SECRET_KEY).
CREATE TABLE IF NOT EXISTS offsite_copy_state (
    id               SMALLINT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    password_enc     TEXT,
    password_set_at  TIMESTAMP,
    password_set_by  VARCHAR(64),
    last_copy_at     TIMESTAMP,
    last_copy_by     VARCHAR(64),
    last_copy_file   VARCHAR(128),
    last_copy_bytes  BIGINT,
    media_watermark  INTEGER NOT NULL DEFAULT 0,
    last_media_at    TIMESTAMP,
    last_media_by    VARCHAR(64),
    last_media_scope VARCHAR(8),
    last_media_count INTEGER,
    last_media_bytes BIGINT
);
INSERT INTO offsite_copy_state (id) VALUES (1) ON CONFLICT (id) DO NOTHING;
```

- [ ] **Step 1: Написать падающие тесты** (сессия БД — мок по образцу `FilesDB` из `tests/test_downloads_auth.py`:
  разбирает текст запроса и хранит состояние в словаре; проверяет и параметры)

```python
def test_load_state_defaults_when_row_missing():    # media_watermark == 0, last_copy_at is None
def test_mark_media_done_never_moves_back():        # SQL содержит GREATEST, параметр upto передан
def test_media_rows_excludes_documents_and_bounds(): # параметры after/upto переданы, в SQL "status <> 'document'"
def test_media_totals_shape():                       # ровно ключи new_count,new_bytes,all_count,all_bytes,max_id
def test_audit_writes_backup_event():               # event_type 'backup', title/detail/actor переданы как есть
def test_check_login_password(fake users):           # верный → True, неверный → False, нет пользователя → False
def test_ticket_single_use(monkeypatch):             # take_ticket(t) → payload, второй раз → None
def test_ticket_ttl_passed_to_redis(monkeypatch):    # set вызван с ex=120
```

- [ ] **Step 2: Запустить — падают**
- [ ] **Step 3: Реализовать миграцию, строку initdb, `offsite_store.py` (эта часть)**
- [ ] **Step 4: pytest целиком — зелёный; `grep -c 035_offsite_copy docker-compose.yml` → 1**
- [ ] **Step 5: Commit** — «Сервер: состояние копии на флешку (миграция 035), ролики, журнал, одноразовые ссылки»

---

### Task 4: Сборка ежедневной копии и инструкция

**Files:**
- Modify: `server/offsite_store.py` (сборка)
- Create: `server/offsite_restore.txt` (шаблон `ВОССТАНОВЛЕНИЕ.txt`)
- Modify: `docker-compose.yml` (у `api` в `volumes:` строка `- ./:/srv/project:ro` с комментарием «программа
  сервера и .env для копии на флешку — только чтение»)
- Test: `server/tests/test_offsite_copy_build.py`

**Interfaces:**
- Consumes: `encrypt_file_openssl`, `OPENSSL_ITER`, `OPENSSL_DECRYPT_CMD` (Задача 1); `is_program_excluded`,
  `copy_filename`, `safe_host` (Задача 2); `TarItem`, `write_tar` (Задача 2); `document_rows` (Задача 3).
- Produces:
  - `OFFSITE_TMP = "/tmp/ds_offsite"`, `PROJECT_DIR = os.getenv("OFFSITE_PROJECT_DIR", "/srv/project")`.
  - `class ProjectNotMounted(Exception)` — нет `PROJECT_DIR/.env` или `PROJECT_DIR/docker-compose.yml`.
  - `@dataclass class BuiltCopy: path: str; filename: str; size: int; workdir: str`
  - `build_copy(db, *, who: str, host: str, password: str, now: datetime) -> BuiltCopy` — во временной папке
    `OFFSITE_TMP/<uuid>/`: свежий дамп (`_pg_dump_gz(dst_path)` — `pg_dump "$DATABASE_URL" | gzip`,
    модульная функция, тесты её подменяют; ошибка → `RuntimeError` с текстом stderr до 300 символов);
    `copy.tar.gz` (`tarfile`, `w:gz`, `PAX_FORMAT`) с разделами `program/` (файлы `PROJECT_DIR`, кроме
    `is_program_excluded`, с их правами; `.env` — 0o600; симлинки не идти), `db/backup_<ГГГГММДД_ЧЧММСС>.sql.gz`,
    `documents/` (`BACKUP_DIR/documents`), `media_documents/` (файлы `document_rows` из `MEDIA_PATH`),
    `agent_updates/` (`AGENT_UPDATE_PATH`), `MANIFEST.txt`; затем `copy.tar.gz.enc`
    (`encrypt_file_openssl`), `copy.tar.gz.enc.sha256` (строка `<sha256>  copy.tar.gz.enc\n`, как у `sha256sum`),
    `ВОССТАНОВЛЕНИЕ.txt` из шаблона; внешний tar `copy_filename(now)` (`write_tar`, без сжатия) из этих трёх.
    Промежуточные файлы удаляются, остаётся только внешний tar.
  - `cleanup_stale(older_than_s: int = 900) -> int` — удаляет папки в `OFFSITE_TMP` старше срока, возвращает число.
  - `MANIFEST.txt`: когда (МСК), кем, адрес (`safe_host`), номер последней миграции (наибольший `NNN_` в
    `PROJECT_DIR/server/migrations`), контрольные числа `SELECT count(*)` по `screens`, `media` (без документов),
    `media` (документы), `playlists`, `users`, `advertisers`, `advertiser_documents`; список файлов:
    `<sha256>  <размер>  <путь>` для каждого файла копии, кроме самого манифеста.
  - Шаблон: плейсхолдеры `{{created_at}}`, `{{created_by}}`, `{{host}}`, `{{copy_file}}`, `{{decrypt_cmd}}`,
    `{{migration}}`, `{{pg_user}}`, `{{pg_db}}` (последние два — `POSTGRES_USER` / `POSTGRES_DB` из окружения,
    по умолчанию `display_user` / `display_system`), подстановка простым `str.replace`. Текст — шаги §5.8 спецификации,
    для человека, который системы не знает, команды готовы к копированию (имена контейнеров `ds_*`).
    Секретов в шаблоне нет.

- [ ] **Step 1: Написать падающие тесты** (`tmp_path` как `PROJECT_DIR`, `BACKUP_DIR`, `MEDIA_PATH`,
  `AGENT_UPDATE_PATH`, `OFFSITE_TMP` через `monkeypatch.setattr`; `_pg_dump_gz` пишет gzip с `b"-- dump"`)

```python
def test_outer_tar_has_three_open_files(…):     # имена ровно: ВОССТАНОВЛЕНИЕ.txt, copy.tar.gz.enc, copy.tar.gz.enc.sha256
def test_instruction_has_no_secrets(…):         # в ВОССТАНОВЛЕНИЕ.txt нет DB_PASSWORD, SECRET_KEY, пароля на копию; есть host и decrypt_cmd
def test_sha256_file_matches(…):                # hashlib.sha256(enc) == значение из .sha256
def test_inner_after_openssl(…):                # openssl → copy.tar.gz: program/.env (байт в байт, mode 0o600),
                                                 # program/server/main.py, db/backup_*.sql.gz, documents/…, media_documents/…, agent_updates/…, MANIFEST.txt
def test_program_exclusions_applied(…):          # нет program/.git, program/.claude/…, program/install_server.log, program/ds_server_V2_x.tar.gz
def test_manifest_counts_and_hashes(…):          # контрольные числа из мока БД; sha256 файла program/.env в манифесте верный
def test_not_mounted_raises(…):                  # нет PROJECT_DIR/.env → ProjectNotMounted
def test_pg_dump_failure_cleans_up(…):           # _pg_dump_gz бросает RuntimeError → в OFFSITE_TMP пусто
def test_cleanup_stale(…):                       # папка с mtime 20 мин назад удалена, свежая — нет
```

- [ ] **Step 2: Запустить — падают**
- [ ] **Step 3: Реализовать `build_copy`, `cleanup_stale`, шаблон, монтирование в compose**
- [ ] **Step 4: pytest целиком — зелёный**
- [ ] **Step 5: Commit** — «Сервер: сборка копии на флешку (запертая часть, инструкция, манифест)»

---

### Task 5: Роутер `/offsite/*`, сброс пароля на сервере, nginx

**Files:**
- Create: `server/routers/offsite.py`, `server/reset_offsite_password.py`
- Modify: `server/main.py` (импорт и `app.include_router(offsite.router)` рядом с `backups`),
  `nginx.conf` (новый `location /api/offsite/` перед `location /api/`: те же `rewrite`, `proxy_pass $ds_api`,
  заголовки и `proxy_read_timeout 300s`, что у `/api/`, плюс `proxy_buffering off;` с комментарием, зачем)
- Test: `server/tests/test_offsite_api.py`

**Interfaces:**
- Consumes: всё из Задач 1–4.
- Produces (HTTP; панель и smoke опираются на это ровно):
  - `GET /offsite/status` →
    `{"password": {"set": bool, "set_at": str|null, "set_by": str|null, "needs_reset": bool},
      "copy": {"last_at": str|null, "last_by": str|null, "last_file": str|null, "last_bytes": int|null, "today": bool},
      "media": {"new_count": int, "new_bytes": int, "all_count": int, "all_bytes": int, "last_at": str|null, "last_by": str|null},
      "state": "ok"|"warn", "project_mounted": bool}`
    (время — наивное МСК ISO `YYYY-MM-DDTHH:MM:SS`; `needs_reset` — `password_enc` есть, но `open_secret` → `None`;
    `set` — пароль есть и читается).
  - `POST /offsite/password` `{"new_password", "new_password2", "login_password", "current_password"?}` → `{"status": "saved"}`.
    Порядок проверок и ответы: неверный пароль входа → 403 `"Неверный пароль входа"` + журнал
    «Неверный пароль при смене пароля на копию»; пароль на копию `set` и `current_password` не совпал → 403
    `"Неверный текущий пароль на копию"` + та же запись; `new_password != new_password2` → 400 `"Пароли не совпадают"`;
    `copy_password_problem` → 400 с его текстом. Успех → журнал «Пароль на копию задан» (не был `set`) или
    «Пароль на копию изменён».
  - `POST /offsite/copy` `{"login_password"}` → `{"url", "filename", "bytes"}`. Неверный пароль → 403
    `"Неверный пароль входа"` + журнал «Неверный пароль при скачивании копии»; пароль на копию не `set` → 409
    `"Сначала задайте пароль на копию"`; `ProjectNotMounted` → 503
    `"Сервер не обновлён для копии на флешку: запустите install_server.sh"`; `RuntimeError` сборки → 500 с текстом.
    Перед сборкой `cleanup_stale()`. Хост — заголовок `Host`.
  - `POST /offsite/media` `{"scope": "new"|"all"}` → `{"url", "filename", "count", "bytes"}`; иное → 400;
    `scope=new` и новых нет → 409 `"Новых роликов нет"`. `upto_id = max_id` из `media_totals`.
  - `GET /offsite/download?t=<токен>` (без входа: токен и есть право) → поток `application/x-tar`,
    `Content-Disposition: attachment; filename="<имя>"`, `Content-Length`, `Cache-Control: no-store`.
    Неизвестный/использованный/просроченный токен → 404 `"Ссылка недействительна или устарела — запросите скачивание заново"`.
    Копия: читается из `BuiltCopy.path` кусками; ролики: `media_rows(after, upto)` → `TarItem` для каждого
    существующего файла `media/<filename>` + `media/MANIFEST_media.txt` (строки `id\tназвание\tфайл\tразмер\tmd5`,
    отсутствующие — с пометкой `нет файла`), размер — `tar_size`.
    После последнего куска генератор открывает новую сессию (`SessionLocal()` из `deps`) и вызывает
    `mark_copy_done` + журнал «Копия на флешку скачана» (`<имя>, <МБ>`) либо `mark_media_done` + журнал
    «Ролики скачаны на флешку» (`новые|все, N шт, X МБ, id A–B`); папка копии удаляется в `finally` (и при обрыве).
- `reset_offsite_password.py` — запуск `docker compose exec -T api python /app/reset_offsite_password.py`:
  `clear_password` + журнал «Пароль на копию сброшен на сервере» (actor `server`), печатает
  `Пароль на копию сброшен. Задайте новый в панели: Настройки → Копия на флешку.`

- [ ] **Step 1: Написать падающие тесты** (TestClient + настоящий JWT, как `tests/test_downloads_auth.py`;
  `get_db` и `SessionLocal` — общий мок с состоянием; `_redis`, `_pg_dump_gz`, каталоги — подменены)

```python
@pytest.mark.parametrize("user", ["admin", "audit", "mod", "client"])
def test_non_superadmin_forbidden(user):           # все POST/GET /offsite/* кроме download → 403
def test_without_login_401():                       # status/password/copy/media без токена → 401
def test_status_shape_and_warn():                   # ключи ровно как в Interfaces; state == "warn" без копии
def test_set_password_first_time_then_change_needs_current():  # без current → 200; повторно без current → 403
def test_set_password_wrong_login_audited_without_secrets():   # 403; запись в журнале; ни один пароль не в detail/title
def test_password_rules_400():                      # несовпадение, короткий, кириллица
def test_copy_needs_password_409():
def test_copy_wrong_login_403_audited():
def test_copy_not_mounted_503():
def test_copy_link_downloads_once_and_marks_done(): # POST → url; GET → 200, Content-Length == len(body), tar читается;
                                                     # status.copy.today == True; второй GET → 404; папка копии удалена
def test_media_new_flow_moves_watermark():          # 3 ролика + 1 документ; new_count == 3; скачали → new_count == 0, state "ok" при сегодняшней копии
def test_media_interrupted_keeps_watermark():       # прочитать часть потока и закрыть → watermark прежний, last_media_at None
def test_media_uploaded_during_download_stays_new(): # после POST добавить строку с id > upto; после GET new_count == 1
def test_media_missing_file_skipped():              # у строки нет файла → 200, Content-Length == len(body), в MANIFEST_media "нет файла"
def test_media_new_none_409():
def test_secret_key_changed_needs_reset():          # password_enc от другого ключа → needs_reset True, copy → 409, set без current → 200
def test_reset_offsite_password_script(monkeypatch): # clear_password вызван, запись «сброшен на сервере»
```

- [ ] **Step 2: Запустить — падают**
- [ ] **Step 3: Реализовать роутер, подключение в `main.py`, скрипт сброса, `location /api/offsite/`**
- [ ] **Step 4: pytest целиком — зелёный**; `docker run --rm -v "<wt>/nginx.conf":/etc/nginx/nginx.conf:ro nginx:alpine nginx -t`
  → `syntax is ok` (если `$ds_api`/resolver требует сеть стека — проверить на стенде в Задаче 7)
- [ ] **Step 5: Commit** — «Сервер: API копии на флешку — статус, пароль на копию, одноразовые ссылки, сброс на сервере»

---

### Task 6: Панель — блок в Настройках, напоминание на дашборде, окна

**Files:**
- Create: `server/static/js/views/offsite.js`
- Modify: `server/static/js/views/settings.js` (у суперадмина над «Резервным копированием»
  `<div class="sec">Копия на флешку</div><div id="offsite-block" class="muted">Загрузка…</div>` и вызов
  `loadOffsiteBlock()`), `server/static/js/views/dashboard.js` (у суперадмина самым верхним
  `<div id="offsite-banner"></div>` и `loadOffsiteBanner()` после отрисовки), `server/static/css/style.css`
  (`.banner.warn{background:#2a2410;border:1px solid #b8901f;}` рядом с `.banner.on/.off`),
  `server/static/index.html` (подключить `js/views/offsite.js?v=20261104` после `dashboard.js`; поднять `?v=`
  у `settings.js`, `dashboard.js`, `style.css`)

**Interfaces:**
- Consumes: HTTP из Задачи 5; `api`, `esc`, `toast`, `isSuper`, `fmtServerTS`, `openModal` (core).
- Produces (глобальные функции, их зовут settings.js и dashboard.js):
  `loadOffsiteBlock(): Promise<void>`, `loadOffsiteBanner(): Promise<void>`.
- Поведение ровно по макетам и §5.6 спецификации:
  - Баннер: `state=warn` — жёлтый `.banner.warn`, заголовок «Копия на флешку»; текст «Сегодня копию ещё не скачивали»
    либо (копия сегодня есть, но новые ролики) «Копия сегодня скачана, а новые ролики — нет»; строка
    «Последняя — <когда> (<кто>) · новых роликов с тех пор: N, X МБ»; кнопка «⤓ Скачать копию»
    (или «Задать пароль на копию», если `!password.set`) либо «⤓ Скачать новые ролики». `state=ok` — `.banner.on`,
    «Сегодня скачана в ЧЧ:ММ (кто)» · «Новые ролики тоже скачаны», без кнопки. Не суперадмину — ничего, запрос не шлётся.
  - Блок Настроек: карточка «Ежедневная копия» (что внутри, последнее скачивание, «Пароль на копию: задан ДД.ММ.ГГГГ
    (кто) · Сменить» / «не задан» / «нужно задать заново»; кнопка «⤓ Скачать копию · ≈ N МБ» по `last_bytes` или
    без размера; без пароля — «Задать пароль на копию»); если `!project_mounted` — строка-предупреждение
    «Сервер не обновлён для копии на флешку: запустите install_server.sh». Карточка «Ролики»: «⤓ Скачать новые ·
    N шт, X МБ» (неактивна при 0) и «⤓ Скачать все · N шт, X ГБ»; «Новые — это добавленные после прошлого
    скачивания (<когда>)».
  - Окно «Пароль на копию»: поля «Текущий пароль на копию» (только если `password.set`), «Новый пароль», «Ещё раз»,
    «Ваш пароль для входа в панель»; текст-предупреждение из §5.2 спецификации. Ошибки сервера — в окне.
  - Окно «Скачать копию на флешку» (макет 3): поле пароля входа, подсказка про «Всегда спрашивать, куда сохранять
    файлы» с именем файла; на время POST — «Собираю копию…», кнопка неактивна. Успех → скачивание по `url`
    (создать `<a href=url download>` и `click()`), окно закрыть, через 3 с перерисовать баннер/блок.
  - «Скачать все» — `confirm` с числом и размером; ролики — без окна с паролем; после POST — скачивание по `url`
    и `toast("Скачивание началось. Отметка появится, когда файл скачается целиком")`.
  - Размеры: МБ с одним знаком, от 1024 МБ — ГБ с одним знаком; время — `fmtServerTS`.
  - Свои действия — `data-action="offsite-*"` и свой обработчик кликов на `document`, по образцу settings.js.

- [ ] **Step 1: Реализовать `offsite.js`, правки settings.js / dashboard.js / style.css / index.html**
- [ ] **Step 2: Проверка в браузере на стенде** (Задача 7 поднимает стенд из этого worktree; здесь — `node --check
  server/static/js/views/offsite.js` и `grep -n "offsite.js" server/static/index.html` → 1 строка)
- [ ] **Step 3: Commit** — «Панель: копия на флешку — блок в Настройках, напоминание на дашборде, окна»

---

### Task 7: E2E и smoke на стенде

**Files:**
- Modify: `ui_tests/ui_test.js`, `ui_test.sh` (флаг `E2E_ALLOW_OFFSITE=1` в шапке и передача в контейнер),
  `smoke_test.sh` (флаг `SMOKE_ALLOW_OFFSITE=1` в шапке)

**Interfaces:**
- Consumes: HTTP из Задачи 5, разметка и `data-action` из Задачи 6.
- Решения:
  - Стенд поднимается из ЭТОГО worktree: `bash dev_stand.sh reset` (после согласования, см. Global Constraints).
  - Без флага (можно и на боевом): smoke — `GET /offsite/status` без входа 401, по входу 200 и ключи;
    `GET /offsite/download?t=xxx` → 404; E2E — у admin (суперадмин) есть `#offsite-banner` на дашборде и
    `#offsite-block` в Настройках, ошибок консоли нет.
  - С флагом (только стенд; меняет пароль на копию): перед шагом сбросить пароль на копию командой
    `docker exec ds_api python /app/reset_offsite_password.py`; задать тестовый пароль `E2E-copy-pass-1`
    (smoke — `SMOKE-copy-pass-1`); скачать копию (smoke — POST + GET по `url` без `/api`, проверить, что это tar
    с тремя файлами; E2E — через окно с паролем, файл скачался), скачать новые ролики, баннер зелёный.
    Неверный пароль входа → 403 (smoke). В конце пароль на копию снова сбросить (стенд остаётся чистым).
- [ ] **Step 1: Дописать шаги smoke и E2E**
- [ ] **Step 2: Прогнать** `ADMIN_PASS=$(cat .dev_admin_pass) SMOKE_ALLOW_OFFSITE=1 bash smoke_test.sh` и
  `ADMIN_PASS=$(cat .dev_admin_pass) DS_URL=http://nginx E2E_ALLOW_OFFSITE=1 bash ui_test.sh` — 0 провалов;
  затем оба без флагов — 0 провалов. Записать итоговые числа (пройдено/провалено) для документации.
- [ ] **Step 3: Commit** — «Тесты: копия на флешку в smoke и E2E»

---

### Task 8: Документация

**Files:**
- Modify: `docs/RESTORE_GUIDE.md` (новый первый сценарий «Восстановление с флешки» по `ВОССТАНОВЛЕНИЕ.txt`;
  раздел «копируйте наружу» → «Копия на флешку» в панели), `docs/Инструкции/03_РОЛЬ_АДМИНИСТРАТОР.md` (блок
  «Копия на флешку», пароль на копию и его сброс, напоминание, «Всегда спрашивать, куда сохранять», флешка exFAT/NTFS),
  `docs/Техдокументация/04_ТЕСТЫ.md` (точные числа pytest / smoke / E2E с флагами и без), `README.md`
  (роутер `offsite`, миграции `001`…`035`), `CLAUDE.md` (структура: `routers/offsite.py`, `offsite_rules.py`,
  `offsite_tar.py`, `offsite_crypto.py`, `offsite_store.py`; миграции `001`…`035`; соглашение «копии для флешки
  на сервере не хранятся, скачивание — одноразовая ссылка»), runbook в `docs/Техдокументация/` (сброс пароля
  на копию, «Сервер не обновлён для копии» → `install_server.sh`)
- [ ] **Step 1: Обновить документы; числа — из прогонов Задачи 7 и итогового pytest**
- [ ] **Step 2: `grep -rn "001…034\|001-034\|001–034" README.md CLAUDE.md docs/` — пусто**
- [ ] **Step 3: Commit** — «Документы: копия на флешку — восстановление, инструкция администратора, тесты»

---

### Task 9: Учения по восстановлению (выполняет управляющая сессия)

Не код, а проверка §8 спецификации; исполняет сессия-контроллер, не исполнитель задачи.

- [ ] **Step 1:** На стенде из worktree с демо-данными (экран, 3 ролика, документ в «Документах», документ
  рекламодателя, OTA-файл) задать пароль на копию, скачать копию и «все ролики» через API в папку
  `scratchpad/drill/flash/`. Снять контрольные числа и md5 роликов исходного стенда.
- [ ] **Step 2:** Строго по `ВОССТАНОВЛЕНИЕ.txt`: распаковать, `sha256sum -c`, расшифровать в `debian:buster`
  (OpenSSL 1.1.1, как Astra 1.7) командой из инструкции, распаковать `copy.tar.gz`.
- [ ] **Step 3:** `bash dev_stand.sh down` (тома стенда не трогать); из `program/` поднять стек проектом
  `dsrestore` с надстройкой `docker-compose.dev.yml` (`COMPOSE_PROJECT_NAME=dsrestore
  COMPOSE_FILE=docker-compose.yml:docker-compose.dev.yml bash install_server.sh`), восстановить по шагам инструкции.
- [ ] **Step 4:** Проверить: вход прежним паролем admin, контрольные числа = `MANIFEST.txt`, md5 роликов и
  документов совпадают, `/health` зелёный. Каждое расхождение инструкции с жизнью — правка шаблона
  (Задача 4) и повтор учений.
- [ ] **Step 5:** `docker compose -p dsrestore down -v`, `bash dev_stand.sh up`; итог учений — в отчёт и в
  `docs/RESTORE_GUIDE.md` («проверено на стенде <дата>»). Commit правок, если были.
