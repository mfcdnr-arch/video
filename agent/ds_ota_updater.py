"""
ds_ota_updater.py — обработка OTA-обновлений агента.
Вызывается из ds_agent.py при получении команды update_agent.
"""
from __future__ import annotations
import hashlib, logging, os, shutil, subprocess, sys, time
from pathlib import Path
import requests

log = logging.getLogger(__name__)

INSTALL_DIR = Path("/opt/ds-agent")
BACKUP_DIR  = INSTALL_DIR / ".backup"


def _md5(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _py_check(path: Path) -> bool:
    """Проверить синтаксис Python-файла."""
    try:
        result = subprocess.run(
            [sys.executable, "-m", "py_compile", str(path)],
            capture_output=True, timeout=15
        )
        return result.returncode == 0
    except Exception as e:
        log.error(f"Синтаксическая проверка провалилась: {e}")
        return False


def apply_update(server_url: str, token: str, payload: dict) -> bool:
    """
    Применить OTA-обновление агента.
    payload: {"version": "1.1.0", "files": [{"name": "...", "md5": "...", "size": N}]}
    Возвращает True если успешно (после этого ds_agent.py перезапустится через systemd).
    """
    version  = payload.get("version", "unknown")
    files    = payload.get("files", [])
    changelog = payload.get("changelog", "")

    log.info(f"OTA: получено обновление v{version}, файлов: {len(files)}")
    if changelog:
        log.info(f"OTA: changelog — {changelog}")

    if not files:
        log.warning("OTA: список файлов пуст, обновление отменено")
        return False

    sess = requests.Session()
    sess.headers["X-Token"] = token

    tmp_dir = INSTALL_DIR / ".ota_tmp"
    tmp_dir.mkdir(exist_ok=True)

    try:
        # 1. Скачать все файлы во временную папку
        for finfo in files:
            fname    = finfo["name"]
            expected_md5  = finfo.get("md5", "")
            expected_size = finfo.get("size", 0)

            url = f"{server_url}/api/agent/files/download/{fname}"
            log.info(f"OTA: скачиваем {fname} ...")

            dest = tmp_dir / fname
            try:
                with sess.get(url, stream=True, timeout=60) as r:
                    if r.status_code != 200:
                        log.error(f"OTA: сервер вернул {r.status_code} для {fname}")
                        return False
                    with open(dest, "wb") as f:
                        for chunk in r.iter_content(65536):
                            f.write(chunk)
            except Exception as e:
                log.error(f"OTA: ошибка загрузки {fname}: {e}")
                return False

            # 2. Проверить MD5
            if expected_md5 and _md5(dest) != expected_md5:
                log.error(f"OTA: MD5 не совпадает для {fname}")
                return False

            # 3. Проверить синтаксис Python
            if fname.endswith(".py") and not _py_check(dest):
                log.error(f"OTA: синтаксическая ошибка в {fname} — обновление отменено")
                return False

            log.info(f"OTA: {fname} проверен ✓")

        # 4. Сделать резервную копию текущих файлов
        BACKUP_DIR.mkdir(exist_ok=True)
        backup_ts = str(int(time.time()))
        backup_path = BACKUP_DIR / backup_ts
        backup_path.mkdir()
        for finfo in files:
            fname = finfo["name"]
            src = INSTALL_DIR / fname
            if src.exists():
                shutil.copy2(str(src), str(backup_path / fname))
        log.info(f"OTA: резервная копия сохранена в {backup_path}")

        # 5. Заменить файлы
        for finfo in files:
            fname = finfo["name"]
            src = tmp_dir / fname
            dst = INSTALL_DIR / fname
            shutil.copy2(str(src), str(dst))
            log.info(f"OTA: установлен {fname}")

        # 6. Очистить временную папку
        shutil.rmtree(str(tmp_dir), ignore_errors=True)

        log.info(f"OTA: обновление v{version} установлено успешно!")
        log.info("OTA: перезапуск службы через systemd...")

        # 7. Перезапуститься через systemd (ds-agent сам себя перезапустит)
        subprocess.run(["sudo", "systemctl", "restart", "ds-agent"], timeout=10)
        # Этот код может не выполниться если systemd перезапустит нас быстро
        return True

    except Exception as e:
        log.error(f"OTA: критическая ошибка: {e}", exc_info=True)
        # Попытка восстановить из бэкапа
        _rollback(files, BACKUP_DIR)
        return False
    finally:
        shutil.rmtree(str(tmp_dir), ignore_errors=True)


def _rollback(files: list, backup_dir: Path):
    """Откатить к предыдущей версии из резервной копии."""
    backups = sorted(backup_dir.iterdir()) if backup_dir.exists() else []
    if not backups:
        log.error("OTA rollback: нет резервных копий!")
        return
    latest = backups[-1]
    log.warning(f"OTA rollback: восстанавливаем из {latest}")
    for finfo in files:
        fname = finfo["name"]
        src = latest / fname
        if src.exists():
            shutil.copy2(str(src), str(INSTALL_DIR / fname))
            log.info(f"OTA rollback: восстановлен {fname}")
