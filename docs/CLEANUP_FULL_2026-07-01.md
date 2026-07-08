# Полная очистка и переустановка Digital Signage v16.1

**Дата создания:** 1 июля 2026  
**Версия:** Digital Signage v16.1 (мини ПК + сервер)

---

## ⚠️ Критические замечания

Эта инструкция **удалит ВСЕ данные** с обоих устройств:
- Все видео и медиафайлы
- База данных PostgreSQL
- Все расписания и конфигурации
- Хранилище MinIO (S3)

**Используйте только если необходимо полное переустановкить и начать с нуля!**

---

## ЧАСТЬ 1: Очистка сервера

### Шаг 1: Остановка Docker контейнеров

```bash
# Подключитесь к серверу и перейдите в папку проекта
cd /root/Video_miniPC_v16.1_final
# или
cd /opt/ds-server

# Остановите все контейнеры
docker compose down

# Проверьте что нет запущенных контейнеров
docker ps
```

### Шаг 2: Удаление volume данных

```bash
# Удалите все volumes (базу данных, Redis, MinIO)
docker volume rm $(docker volume ls -q | grep ds_)

# Или удалите específический volume
docker volume rm ds_postgres_data
docker volume rm ds_redis_data
docker volume rm ds_minio_data
```

### Шаг 3: Очистка директорий проекта

```bash
# Удалите данные медиатеки (но не исходные файлы)
rm -rf /opt/ds-server/media/*
rm -rf /root/Video_miniPC_v16.1_final/media/*

# Очистите логи (опционально)
rm -rf /opt/ds-server/logs/*
```

### Шаг 4: Удаление Docker образов (если нужно полная переустановка)

```bash
# Удалите образы
docker rmi $(docker images -q | grep digital-signage)
docker rmi $(docker images -q | grep ds_)

# Или удалите все образы проекта
docker rmi ds_api:latest
docker rmi ds_nginx:latest
docker rmi postgres:15
docker rmi redis:7
docker rmi minio/minio:latest
```

---

## ЧАСТЬ 2: Очистка мини ПК (Astra Linux)

### Шаг 1: Остановка systemd сервиса

```bash
# Остановите агент
sudo systemctl stop ds-agent

# Отключите автозапуск
sudo systemctl disable ds-agent
```

### Шаг 2: Удаление файлов агента

```bash
# Удалите установку агента
sudo rm -rf /opt/ds-agent

# Удалите конфигурацию
sudo rm -rf /etc/ds-agent

# Удалите логи
sudo rm -rf /var/log/ds-agent
```

### Шаг 3: Удаление systemd сервиса

```bash
# Удалите файл сервиса
sudo rm -f /etc/systemd/system/ds-agent.service

# Перезагрузите systemd
sudo systemctl daemon-reload

# Проверьте что сервис удалён
systemctl list-unit-files | grep ds-agent
```

### Шаг 4: Удаление Python окружения

```bash
# Удалите виртуальное окружение (если создавали отдельно)
sudo rm -rf /opt/ds-agent/venv
```

### Шаг 5: Удаление mpv сокета

```bash
# Удалите сокет (если остался)
rm -f /tmp/ds-mpv.sock

# Убедитесь что нет процесса mpv
pkill -9 mpv
```

### Шаг 6: Удаление пользователя (опционально)

```bash
# Если создавали специального пользователя для агента
sudo deluser --remove-home toor
# или
sudo userdel -r toor
```

### Шаг 7: Очистка кэша пакетов (опционально)

```bash
# Очистите локальный кэш apt
sudo apt-get clean
sudo apt-get autoclean

# Удалите ненужные пакеты
sudo apt-get autoremove
```

---

## ЧАСТЬ 3: Полное переустановление

После очистки обоих устройств, начните с нуля:

### На сервере:

```bash
cd /root/Video_miniPC_v16.1_final

# 1. Создайте .env файл
cat > .env << 'EOF'
POSTGRES_DB=ds_user
POSTGRES_USER=ds_user
DB_PASSWORD=ds_pass_secure
MINIO_USER=minioadmin
MINIO_PASSWORD=minioadmin
POSTGRES_INITDB_ARGS=-c default_transaction_isolation=read_committed
EOF

# 2. Запустите Docker
docker compose up -d

# 3. Проверьте здоровье
sleep 10
curl http://localhost/api/health
```

### На мини ПК:

```bash
# 1. Получите свежую копию папки client
scp -r root@YOUR_SERVER_IP:/root/Video_miniPC_v16.1_final/client /tmp/

# 2. Запустите установку
sudo bash /tmp/client/install.sh http://YOUR_SERVER_IP abc123 1 toor

# 3. Проверьте статус
systemctl status ds-agent
journalctl -u ds-agent -n 50 -f
```

---

## Скрипт автоматической очистки (На сервере)

Сохраните как `cleanup-all.sh`:

```bash
#!/bin/bash
set -e

echo "🔥 Полная очистка Digital Signage..."
echo "⚠️  ВСЕ данные будут удалены!"
read -p "Введите YES для подтверждения: " confirm
if [ "$confirm" != "YES" ]; then
    echo "Отменено."
    exit 1
fi

cd /root/Video_miniPC_v16.1_final || exit 1

echo "[1/5] Остановка Docker контейнеров..."
docker compose down || true

echo "[2/5] Удаление volumes..."
docker volume rm $(docker volume ls -q | grep ds_) || true

echo "[3/5] Очистка директорий..."
rm -rf media/* logs/* || true

echo "[4/5] Удаление Docker образов..."
docker rmi $(docker images -q | grep -E "ds_|digital-signage") || true

echo "[5/5] Перезагрузка Docker daemon..."
systemctl restart docker || true

echo "✅ Очистка завершена!"
echo ""
echo "Далее на мини ПК выполните:"
echo "  sudo systemctl stop ds-agent"
echo "  sudo rm -rf /opt/ds-agent /etc/ds-agent"
echo "  sudo systemctl daemon-reload"
```

Использование:

```bash
chmod +x cleanup-all.sh
sudo ./cleanup-all.sh
```

---

## Проверка что всё удалено

### На сервере:

```bash
docker ps -a          # не должно быть контейнеров
docker volume ls      # не должно быть ds_* volumes
docker images         # не должно быть ds_* образов
ls -la /root/Video_miniPC_v16.1_final/media  # должна быть пуста
```

### На мини ПК:

```bash
ls -la /opt/ds-agent      # не должна существовать
systemctl list-units ds-agent  # не должна быть
ps aux | grep mpv         # не должно быть процесса
```

---

## Восстановление конкретных данных

Если нужно восстановить только базу данных без переустановки:

```bash
# На сервере, в папке проекта
docker compose exec postgres pg_dumpall > backup.sql
# Потом восстановите:
docker compose exec postgres psql -U ds_user < backup.sql
```

---

## Возможные проблемы при очистке

### Docker volumes не удаляются

```bash
# Перезагрузите Docker daemon
sudo systemctl restart docker
docker volume prune -f  # безопасное удаление
```

### Файлы занимают место и не удаляются

```bash
# Проверьте права доступа
ls -la /root/Video_miniPC_v16.1_final/media
# Если нужно, удалите с sudo
sudo rm -rf /root/Video_miniPC_v16.1_final/media/*
```

### На мини ПК не удаляется /opt/ds-agent

```bash
# Перезагрузитесь
sudo reboot
# После перезагрузки попробуйте:
sudo rm -rf /opt/ds-agent
```

---

## Контрольный список перед новой установкой

- [ ] Docker полностью остановлен
- [ ] Все volumes удалены
- [ ] Агент остановлен на мини ПК
- [ ] Systemd сервис удалён
- [ ] Файлы агента удалены
- [ ] mpv процесс убит
- [ ] Сокет /tmp/ds-mpv.sock удалён
- [ ] Диск очищен (df -h показывает свободное место)

---

**После выполнения этих шагов система полностью очищена и готова к переустановке с нуля.**
