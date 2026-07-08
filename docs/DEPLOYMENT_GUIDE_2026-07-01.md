# Руководство развёртывания Digital Signage v16.1 (Final)

**Дата:** 1 июля 2026  
**Версия:** v16.1 FINAL WITH FIXES  
**Автор:** Digital Signage Engineering Team

---

## 🎯 Что исправлено в этой версии

✅ **mpv вместо VLC** — современный проигрыватель через IPC Unix-сокет  
✅ **Правильный systemd сервис** — использует graphical.target вместо graphical-session.target  
✅ **API endpoints** — полный набор для управления рекламодателями, папками, плейлистами  
✅ **Веб-панель** — управление рекламодателями и расписанием  
✅ **SQL миграции** — консистентный формат со всеми необходимыми таблицами  
✅ **Автоматическая установка** — скрипт install.sh готов к использованию  

---

## 📋 Что вам понадобится

### Сервер:
- Linux (Ubuntu 20.04+, Debian 11+) или Windows с WSL
- Docker и Docker Compose
- 4+ ГБ RAM
- 20+ ГБ свободного место на диске
- Сетевой доступ (порты 80, 443 для nginx)

### Мини ПК (рекламный экран):
- Astra Linux 1.8 SE
- Подключение к сетевому кабелю или WiFi
- Подключение видеовыхода к экрану
- Правильные учётные данные для ssh (root или пользователь с sudo)

---

## ЧАСТЬ 1: Развёртывание сервера

### Шаг 1: Подготовка сервера

```bash
# На Linux сервере
sudo apt-get update
sudo apt-get install -y docker.io docker-compose git

# Добавьте вашего пользователя в группу docker (опционально)
sudo usermod -aG docker $USER
newgrp docker
```

### Шаг 2: Скопируйте проект на сервер

```bash
# На вашем компьютере (если скачиваете архив)
# Распакуйте архив Video_miniPC_v16.1_FINAL_2026-07-01_WITH_FIXES.zip

# Или скопируйте папку
mkdir -p /root/
cp -r Video_miniPC_v16.1_final /root/

cd /root/Video_miniPC_v16.1_final
```

### Шаг 3: Создайте .env файл

```bash
# Создайте файл конфигурации для Docker
cat > .env << 'EOF'
POSTGRES_DB=ds_user
POSTGRES_USER=ds_user
DB_PASSWORD=ds_pass_secure
MINIO_USER=minioadmin
MINIO_PASSWORD=minioadmin
POSTGRES_INITDB_ARGS=-c default_transaction_isolation=read_committed
EOF

# Проверьте файл
cat .env
```

### Шаг 4: Запустите Docker контейнеры

```bash
# Убедитесь что вы в папке проекта
pwd  # должно быть /root/Video_miniPC_v16.1_final

# Создайте и запустите контейнеры
docker compose up -d

# Дождитесь инициализации (5-10 секунд)
sleep 10

# Проверьте статус контейнеров
docker compose ps
# Все 5 контейнеров должны быть в статусе "Up"
```

### Шаг 5: Проверьте здоровье API

```bash
# Проверьте что API доступен
curl http://localhost/api/health

# Должен вывести:
# {"status":"ok"}

# Если ошибка, проверьте логи
docker compose logs api
```

### Шаг 6: Откройте веб-панель

Перейдите в браузере на:
```
http://YOUR_SERVER_IP/
```

Должны видеть интерфейс Digital Signage с секциями:
- Рекламодатели
- Папки медиатеки
- Плейлисты
- Расписание
- Экраны/мини ПК

---

## ЧАСТЬ 2: Установка на мини ПК

### Шаг 1: Подготовка мини ПК

```bash
# На мини ПК (Astra Linux)
# Подключитесь по ssh или откройте терминал

# Обновите систему
sudo apt-get update
sudo apt-get upgrade -y

# Проверьте подключение к сети
ping 8.8.8.8
# Если интернета нет, используйте offline installation (см. ниже)
```

### Шаг 2: Скопируйте скрипт установки на мини ПК

```bash
# На вашем компьютере
scp -r Video_miniPC_v16.1_final/client root@YOUR_MINIPC_IP:/root/

# Или если используете WinSCP:
# 1. Подключитесь к мини ПК
# 2. Откройте обозреватель локальных файлов
# 3. Найдите папку Video_miniPC_v16.1_final/client
# 4. Перетащите в /root/ на мини ПК
```

### Шаг 3: Запустите установщик

```bash
# На мини ПК
sudo bash /root/client/install.sh \
  http://YOUR_SERVER_IP \
  abc123 \
  1 \
  toor

# Где:
# - http://YOUR_SERVER_IP — IP/доменное имя сервера (например http://192.168.1.100)
# - abc123 — токен устройства (может быть любым, используется для идентификации)
# - 1 — ID экрана (уникальный номер этого мини ПК, можно использовать 1, 2, 3...)
# - toor — имя пользователя для X-сессии (обычно toor или root)
```

**Ожидайте:** Скрипт выполняется 5-10 минут.

### Шаг 4: Проверьте установку

```bash
# На мини ПК
# Проверьте статус сервиса
systemctl status ds-agent

# Должно быть:
# ● ds-agent.service - Digital Signage Agent
#   Loaded: loaded (/etc/systemd/system/ds-agent.service; enabled; preset: enabled)
#   Active: active (running) since ...

# Если ошибка, посмотрите логи
journalctl -u ds-agent -n 50 -f

# Выход из логов: Ctrl+C
```

### Шаг 5: Проверьте что mpv запущен

```bash
# На мини ПК
ps aux | grep mpv

# Должна быть строка типа:
# /usr/bin/mpv --fullscreen ... --input-ipc-server=/tmp/ds-mpv.sock

# Проверьте что сокет создан
ls -la /tmp/ds-mpv.sock

# Если сокета нет — перезагрузите сервис
sudo systemctl restart ds-agent
```

---

## ЧАСТЬ 3: Тестирование системы

### Тест 1: Регистрация экрана

1. Откройте веб-панель: `http://YOUR_SERVER_IP/`
2. Перейдите на вкладку "Экраны"
3. Должен появиться новый экран с ID=1
4. Проверьте статус "Online"

### Тест 2: Загрузка и показ видео

1. Перейдите на вкладку "Медиа"
2. Загрузите видеофайл (MP4, MKV и т.д.)
3. Проверьте что видео загружено
4. Создайте новый плейлист
5. Добавьте видео в плейлист
6. Создайте расписание для этого видео на экран ID=1
7. **На мини ПК должно начать воспроизводиться видео**

### Тест 3: Проверка mpv (вместо vlc)

1. На мини ПК откройте `dmesg` или логи:
```bash
journalctl -u ds-agent -n 100 | grep -i "mpv\|vlc"
# Должны быть сообщения про mpv, не про vlc
```

2. Проверьте процесс:
```bash
ps aux | grep mpv
# Должны видеть процесс mpv с --input-ipc-server
```

### Тест 4: Перезапуск при ошибке

1. Остановите агент: `sudo systemctl stop ds-agent`
2. Проверьте статус: `systemctl status ds-agent` (будет "inactive")
3. Подождите 15 секунд
4. Проверьте что агент перезагрузился сам: `systemctl status ds-agent`
5. Статус должен вернуться в "active"

---

## 🐛 Решение проблем

### Проблема: API недоступен

```bash
# На сервере
docker compose logs api | tail -20
# Проверьте что контейнер запустился без ошибок

# Перезагрузите контейнер
docker compose restart api

# Проверьте port binding
docker ps | grep api
# Должен быть порт :80
```

### Проблема: Мини ПК не подключается к серверу

```bash
# На мини ПК проверьте сетевое подключение
ping YOUR_SERVER_IP

# Проверьте что агент видит сервер
journalctl -u ds-agent -n 50 -f
# Должны быть сообщения о подключении

# Если нет, проверьте IP адрес сервера
# Отредактируйте /etc/ds-agent/config.ini:
# sudo nano /etc/ds-agent/config.ini
# [server]
# url = http://192.168.1.100
```

### Проблема: Видео не показывается

```bash
# На мини ПК проверьте логи агента
journalctl -u ds-agent -n 100 -f

# Ищите ошибки типа:
# - "Connection refused" — сокет не доступен
# - "Timeout" — mpv не отвечает
# - "File not found" — видеофайл не загружен

# Перезагрузите сервис
sudo systemctl restart ds-agent
```

### Проблема: Ошибка "Unit graphical-session.target not found"

**Это означает что используется старая версия ds-agent.service!**

```bash
# На мини ПК
# Проверьте содержимое файла
grep -i "graphical-session" /etc/systemd/system/ds-agent.service

# Если есть graphical-session.target — отредактируйте файл:
sudo nano /etc/systemd/system/ds-agent.service

# Найдите и УДАЛИТЕ строку:
# BindsTo=graphical-session.target

# Измените:
# After=network-online.target chrony.service graphical.target
# Wants=network-online.target graphical.target
# WantedBy=graphical.target

# Сохраните и перезагрузите
sudo systemctl daemon-reload
sudo systemctl restart ds-agent
```

### Проблема: Интернет на мини ПК отсутствует

```bash
# Offline installation

# 1. На компьютере с интернетом скачайте зависимости:
pip download -r client/requirements.txt -d /tmp/wheels/

# 2. Скопируйте папку wheels на мини ПК:
scp -r /tmp/wheels root@YOUR_MINIPC_IP:/tmp/

# 3. На мини ПК установите offline:
pip install -r /root/client/requirements.txt \
  --break-system-packages \
  -f /tmp/wheels/
```

---

## 📁 Важные файлы и пути

### На сервере:
- `/root/Video_miniPC_v16.1_final/docker-compose.yml` — конфигурация контейнеров
- `/root/Video_miniPC_v16.1_final/.env` — переменные окружения
- `/root/Video_miniPC_v16.1_final/app/main.py` — API сервер
- `/root/Video_miniPC_v16.1_final/app/static/index.html` — веб-интерфейс

### На мини ПК:
- `/opt/ds-agent/` — установленный агент
- `/etc/systemd/system/ds-agent.service` — systemd сервис
- `/etc/ds-agent/config.ini` — конфигурация агента
- `/var/log/ds-agent/` — логи агента
- `/tmp/ds-mpv.sock` — IPC сокет mpv

---

## 🔐 Безопасность

### Рекомендуемые действия:

1. **Измените пароли по умолчанию:**
```bash
# Пароль PostgreSQL
docker compose exec postgres psql -U ds_user -d ds_user -c "ALTER USER ds_user PASSWORD 'your_strong_password';"

# Пароли MinIO
# Отредактируйте .env и перезагрузите контейнеры
```

2. **Используйте HTTPS (если нужна защита):**
```bash
# Отредактируйте nginx.conf и добавьте SSL сертификат
# Используйте Let's Encrypt:
sudo apt-get install certbot python3-certbot-nginx
```

3. **Установите firewall:**
```bash
sudo ufw enable
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow 22/tcp    # SSH
sudo ufw allow 80/tcp    # HTTP
sudo ufw allow 443/tcp   # HTTPS
```

---

## 🔄 Обновление и поддержка

### Обновление из предыдущей версии:

```bash
# Если уже установлена старая версия, используйте:
cd /root/Video_miniPC_v16.1_final

# Скопируйте .env из старой установки (если есть)
cp ../old_version/.env ./

# Перезагрузите контейнеры
docker compose down
docker compose up -d
```

### Полная переустановка (если есть проблемы):

Используйте документ `CLEANUP_FULL_2026-07-01.md` для полной очистки.

---

## 📞 Получение помощи

1. **Проверьте логи:**
   - Сервер: `docker compose logs -f api`
   - Мини ПК: `journalctl -u ds-agent -n 100 -f`

2. **Проверьте документацию:**
   - `CHANGES_2026-07-01.md` — что изменилось
   - `CLEANUP_FULL_2026-07-01.md` — как очистить и начать заново

3. **Проверьте статус:**
   - API: `curl http://YOUR_SERVER_IP/api/health`
   - Экран: `curl http://YOUR_SERVER_IP/api/minipc`

---

## ✅ Финальный чек-лист

- [ ] Сервер развёрнут (docker compose up)
- [ ] API доступен (curl http://localhost/api/health)
- [ ] Веб-панель открывается (http://YOUR_SERVER_IP/)
- [ ] Мини ПК установлен (install.sh выполнен)
- [ ] Агент запущен (systemctl status ds-agent)
- [ ] mpv запущен (ps aux | grep mpv)
- [ ] Видео загружено и показывается
- [ ] Расписание работает

---

**Поздравляем! Система Digital Signage v16.1 полностью установлена и готова к работе.**
