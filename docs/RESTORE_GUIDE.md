# Восстановление сервера Digital Signage из резервных копий

Проверено на практике 2026-07-07: дамп БД восстановлен в отдельную базу
(28 таблиц, счётчики строк совпали), медиафайл выгружен из MinIO с
совпадением md5.

## Что и куда бэкапится (автоматически, Celery)

| Что            | Куда                                  | Когда           | Ротация |
|----------------|---------------------------------------|-----------------|---------|
| База данных    | том `backup_data` → `/data/backups/backup_*.sql.gz` | ежедневно 03:30 МСК | последние 14 (env `BACKUP_KEEP`) |
| Медиафайлы     | MinIO, бакет `ds-media-backup`        | ежедневно 02:30 МСК | зеркало (удалённые локально файлы в бакете остаются) |

Обе копии живут в Docker-томах на том же сервере. **Для защиты от гибели
самого сервера регулярно копируйте их наружу** (на NAS/другую машину):

```bash
# дампы БД
docker cp ds_api:/data/backups ./offsite_backups/
# медиа из MinIO (пример через mc — MinIO Client)
mc alias set ds http://10.0.119.100:9000 <MINIO_USER> <MINIO_PASSWORD>
mc mirror ds/ds-media-backup ./offsite_media/
```

## Сценарий 1: восстановление БД на работающем сервере

Например, после ошибочного массового удаления.

```bash
cd Video_miniPC_v16.2_modular

# 1. Выбрать дамп
docker exec ds_api ls -t /data/backups/

# 2. Остановить api и celery, чтобы никто не писал в базу
docker compose stop api celery

# 3. Пересоздать базу и залить дамп
docker exec ds_postgres psql -U display_user -d postgres \
  -c "DROP DATABASE display_system" \
  -c "CREATE DATABASE display_system OWNER display_user"
docker exec ds_api sh -c "gunzip -c /data/backups/ИМЯ_ДАМПА.sql.gz" \
  | docker exec -i ds_postgres psql -U display_user -d display_system -q

# 4. Запустить обратно и проверить
docker compose start api celery
docker exec ds_api curl -s http://localhost:8000/health
```

Имена базы/пользователя возьмите из своего `.env` (`POSTGRES_DB`,
`POSTGRES_USER`) — выше показаны значения по умолчанию.

## Сценарий 2: полное восстановление на новой машине

1. Установить Docker, скопировать папку проекта на новый сервер.
2. `bash install_server.sh --env-only` — создаст новый `.env`
   (или скопируйте сохранённый старый `.env` — тогда пароли и токены
   входа останутся прежними).
3. `bash install_server.sh` — поднимет пустой рабочий стек.
4. Восстановить БД по Сценарию 1 (дамп принести с собой / со внешней копии).
   ВАЖНО: токены экранов лежат в базе, поэтому после восстановления БД
   все мини-ПК продолжат работать без перенастройки — им нужен только
   прежний IP/домен сервера (он прописан в конфиге агента).
5. Восстановить медиафайлы в том `media_data`:
   ```bash
   # если старый MinIO жив (бэкапный том переехал или доступен по сети):
   docker exec ds_celery python3 - <<'EOF'
   from minio import Minio
   import os
   c = Minio(os.getenv("MINIO_ENDPOINT", "minio:9000"),
             access_key=os.getenv("MINIO_USER"),
             secret_key=os.getenv("MINIO_PASSWORD"), secure=False)
   for obj in c.list_objects("ds-media-backup", recursive=True):
       c.fget_object("ds-media-backup", obj.object_name,
                     "/data/media/" + obj.object_name)
       print("восстановлен:", obj.object_name)
   EOF
   # либо из внешней копии: docker cp ./offsite_media/. ds_api:/data/media/
   ```
6. Проверить: панель открывается, медиатека показывает ролики
   (миниатюры пересоздадутся сами при первом просмотре), экраны выходят
   на связь, `Отчёты` показывают историю.

## Регулярная проверка (раз в квартал)

Бэкап, из которого ни разу не восстанавливались — это не бэкап.
Быстрая проверка без остановки сервера (безопасно, работает во
временной базе):

```bash
LATEST=$(docker exec ds_postgres psql -U display_user -d display_system \
  -tAc "SELECT filename FROM backups ORDER BY created_at DESC LIMIT 1")
docker exec ds_postgres createdb -U display_user restore_test
docker exec ds_api sh -c "gunzip -c /data/backups/$LATEST" \
  | docker exec -i ds_postgres psql -U display_user -d restore_test -q
docker exec ds_postgres psql -U display_user -d restore_test \
  -c "SELECT COUNT(*) AS tables FROM information_schema.tables WHERE table_schema='public'"
docker exec ds_postgres psql -U display_user -d display_system -c "DROP DATABASE restore_test"
```

Ожидаемо: количество таблиц совпадает с рабочей базой (28 на момент
написания), ошибок при заливке нет.
