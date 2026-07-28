"""
Уведомления оператору через мессенджер MAX (dev.max.ru Bot API).

Отправка: POST {base_url}/messages?chat_id=<id>, заголовок Authorization: <token>
(без «Bearer» — так требует MAX), тело {"text": ..., "format": "markdown"}.
base_url и chat_id/токен настраиваются в панели (таблица notification_settings),
поэтому если MAX сменит домен (в документации встречались platform-api /
platform-api2) — правится без изменения кода.

Дедупликация: одно активное событие = одно сообщение. Пока проблема
«активна», повторных сообщений нет; когда исчезает — запись закрывается,
и по экранам шлём сообщение о восстановлении. Логика вынесена отдельно
от Celery-обёртки, чтобы её можно было тестировать без брокера.
"""
import logging
import os

import httpx
from sqlalchemy import create_engine, text

log = logging.getLogger(__name__)


def _engine():
    url = os.getenv("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL не задан")
    return create_engine(url, pool_pre_ping=True)


def send_max_message(base_url: str, token: str, chat_id: str, message: str) -> tuple[bool, str]:
    """Отправить текст в MAX. Возвращает (успех, детали). Не бросает исключений."""
    if not (base_url and token and chat_id):
        return False, "Не заполнены base_url / token / chat_id"
    url = base_url.rstrip("/") + "/messages"
    try:
        r = httpx.post(
            url,
            params={"chat_id": chat_id},
            headers={"Authorization": token, "Content-Type": "application/json"},
            json={"text": message[:4000], "format": "markdown", "notify": True},
            timeout=15,
        )
        if r.status_code // 100 == 2:
            return True, "ok"
        return False, f"HTTP {r.status_code}: {r.text[:200]}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def compute_alerts(conn, settings) -> dict:
    """Текущие активные проблемы: {alert_key: (type, message)}."""
    alerts = {}

    if settings["notify_offline"]:
        rows = conn.execute(text("""
            SELECT id, name, last_seen
            FROM screens
            WHERE last_seen IS NOT NULL
              AND last_seen < NOW() - (:mins || ' minutes')::interval
        """), {"mins": settings["offline_minutes"]}).fetchall()
        for r in rows:
            alerts[f"offline:{r.id}"] = (
                "offline",
                f"🔴 Экран «{r.name}» не выходит на связь "
                f"дольше {settings['offline_minutes']} мин. Проверьте питание и сеть.")

    if settings["notify_disk"]:
        rows = conn.execute(text("""
            SELECT id, name, disk_free_gb, disk_total_gb,
                   ROUND(100.0 * disk_free_gb / NULLIF(disk_total_gb, 0)) AS free_pct
            FROM screens
            WHERE disk_total_gb > 0
              AND 100.0 * disk_free_gb / disk_total_gb < :pct
        """), {"pct": settings["disk_free_pct"]}).fetchall()
        for r in rows:
            alerts[f"disk:{r.id}"] = (
                "disk",
                f"💾 На экране «{r.name}» мало места: свободно {r.free_pct}% "
                f"({float(r.disk_free_gb):.0f} из {float(r.disk_total_gb):.0f} ГБ).")

    # Отключённый монитор имеет смысл ловить только у живых экранов: у офлайн
    # уже есть своя тревога, а данные о видеовыходе у них устарели.
    # Пауза в 3 минуты после смены состояния гасит ложные срабатывания при
    # перезагрузке/переключении входа монитора.
    if settings.get("notify_display"):
        rows = conn.execute(text("""
            SELECT id, name, display_outputs
            FROM screens
            WHERE display_connected IS FALSE
              AND last_seen > NOW() - (:mins || ' minutes')::interval
              AND (display_changed_at IS NULL
                   OR display_changed_at < NOW() - INTERVAL '3 minutes')
        """), {"mins": settings["offline_minutes"]}).fetchall()
        for r in rows:
            outs = f" Выходы: {r.display_outputs}." if r.display_outputs else ""
            alerts[f"display:{r.id}"] = (
                "display",
                f"📺 Экран «{r.name}»: монитор не подключён к видеовыходу "
                f"(HDMI/DisplayPort). Мини-ПК работает и играет контент, но на "
                f"мониторе его не видно — проверьте кабель и питание монитора.{outs}")

    if settings["notify_broken"]:
        rows = conn.execute(text("""
            SELECT m.id, COALESCE(m.title, m.filename) AS title, a.name AS advertiser
            FROM media m LEFT JOIN advertisers a ON a.id = m.advertiser_id
            WHERE m.is_broken = TRUE
        """)).fetchall()
        for r in rows:
            adv = f" ({r.advertiser})" if r.advertiser else ""
            alerts[f"broken:{r.id}"] = (
                "broken",
                f"⚠️ Ролик «{r.title}»{adv} не воспроизводится на экранах.")

    return alerts


def run_notifications() -> dict:
    """Один проход проверки: детект → дедуп → отправка. Возвращает сводку."""
    engine = _engine()
    with engine.begin() as conn:
        s = conn.execute(text("SELECT * FROM notification_settings WHERE id = 1")).mappings().first()
        if not s or not s["enabled"]:
            return {"skipped": "выключено"}
        if not (s["max_token"] and s["max_chat_id"]):
            return {"skipped": "не заданы токен/chat_id"}

        current = compute_alerts(conn, s)
        active = {r.alert_key: r.id for r in conn.execute(text(
            "SELECT id, alert_key FROM notification_alerts WHERE is_active")).fetchall()}

        base, token, chat = s["base_url"], s["max_token"], s["max_chat_id"]
        sent, failed, resolved = 0, 0, 0

        # новые проблемы → сообщение + запись
        for key, (atype, msg) in current.items():
            if key in active:
                continue
            ok, _detail = send_max_message(base, token, chat, msg)
            conn.execute(text("""
                INSERT INTO notification_alerts (alert_key, type, message, is_active, sent_ok)
                VALUES (:k, :t, :m, TRUE, :ok)
            """), {"k": key, "t": atype, "m": msg, "ok": ok})
            sent += 1 if ok else 0
            failed += 0 if ok else 1

        # исчезнувшие проблемы → закрыть; по экранам — сообщение о восстановлении
        for key, aid in active.items():
            if key in current:
                continue
            if key.startswith(("offline:", "display:")):
                name = conn.execute(text("SELECT name FROM screens WHERE id = :id"),
                                    {"id": int(key.split(":")[1])}).scalar()
                if name:
                    recovered = ("снова онлайн" if key.startswith("offline:")
                                 else "монитор снова подключён")
                    send_max_message(base, token, chat, f"✅ Экран «{name}»: {recovered}.")
            conn.execute(text(
                "UPDATE notification_alerts SET is_active = FALSE, resolved_at = NOW() WHERE id = :id"),
                {"id": aid})
            resolved += 1

    return {"new_sent": sent, "new_failed": failed, "resolved": resolved,
            "active_total": len(current)}
