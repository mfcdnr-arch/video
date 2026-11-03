"""Бегущая строка в БД: строки, срок, «где», отчёты экранов, команды старым агентам, шаблоны.

Здесь только SQL. Решения «идёт ли строка сейчас», «на каких экранах», «какие строки у экрана»
принимает ticker_rules — чистая логика; хранилище отдаёт ей строки как dict
`{id, message, color, speed, target, group_ids, screen_ids, until_at, started_at, ...}`.

Все отметки времени — наивное МСК. Пишем и сравниваем их из Python (`now` приходит снаружи),
а не NOW() Postgres: контейнер БД живёт в UTC, и «до 15:00» поехало бы на три часа. Срок
`until_at = NULL` — «до снятия». Строка со сроком живёт строго до `until_at`; истёкшую строку
правка уже не находит (LookupError), а Celery-задача снимает её через expire_due
(и сообщает старым агентам, что у них осталось).

Эти же функции читает лента экрана, поэтому «кто что видит» живёт в одном месте
(tickers_for_screen), а «в сети» — только в screen_online.

Старые агенты (< 1.4.0) строку из ленты не берут, им сервер шлёт команды `ticker_show` /
`ticker_hide` (notify_screens): всегда о самой новой живой строке экрана, потому что у такого
агента строка одна, и скрытие одной из нескольких не должно гасить остальные. Это состояние, а не
события: перед новой командой ожидающие ticker_* этого экрана гасятся (queue_ticker_command), чтобы
в очереди оставалась одна — иначе устаревшая команда съест WS-будильник следующей.

Функции не делают commit — транзакцией управляет вызывающий (роутер или задача). Журнал
(`audit_log`) пишется в той же транзакции, что и изменение.
"""
import json
import math
from datetime import datetime

from sqlalchemy import text

from screen_online import offline_for_sql, online_sql
from ticker_rules import resolve_screens, ticker_live, tickers_for_screen

AUDIT_TYPE = "ticker"
TITLE_ON = "Бегущая строка запущена"
TITLE_CHANGED = "Бегущая строка изменена"
TITLE_OFF = "Бегущая строка снята"
TITLE_EXPIRED = "Бегущая строка снята по сроку"
SYSTEM_ACTOR = "система"

_NOT_FOUND = "Бегущая строка не найдена"
_TEMPLATE_NOT_FOUND = "Шаблон не найден"

# Строка вместе со списками групп и экранов (массивы, пустые — []). Условие и порядок — снаружи.
_SELECT_TICKERS = """
    SELECT t.id, t.message, t.color, t.speed, t.target, t.until_at,
           t.started_at, t.started_by, t.changed_at, t.changed_by,
           COALESCE((SELECT array_agg(g.group_id ORDER BY g.group_id)
                     FROM ticker_groups g WHERE g.ticker_id = t.id), ARRAY[]::INTEGER[]) AS group_ids,
           COALESCE((SELECT array_agg(s.screen_id ORDER BY s.screen_id)
                     FROM ticker_screens s WHERE s.ticker_id = t.id), ARRAY[]::INTEGER[]) AS screen_ids
    FROM tickers t
"""


def _audit(db, title: str, detail: str | None, actor: str) -> None:
    db.execute(text("""
        INSERT INTO audit_log (event_type, title, detail, actor)
        VALUES (:type, :t, :d, :a)
    """), {"type": AUDIT_TYPE, "t": title, "d": detail, "a": actor})


def _ticker(r) -> dict:
    return {"id": r.id, "message": r.message, "color": r.color, "speed": r.speed, "target": r.target,
            "group_ids": list(r.group_ids or []), "screen_ids": list(r.screen_ids or []),
            "until_at": r.until_at, "started_at": r.started_at, "started_by": r.started_by,
            "changed_at": r.changed_at, "changed_by": r.changed_by}


def _all_screens(db) -> list[tuple[int, int | None]]:
    """Все экраны сети (id, group_id) — вход resolve_screens и tickers_for_screen."""
    return [(r.id, r.group_id) for r in db.execute(text("SELECT id, group_id FROM screens")).fetchall()]


# --- чтение -------------------------------------------------------------------

def list_live(db, now: datetime) -> list[dict]:
    """Живые строки в порядке запуска: без срока или со сроком позже `now` (наивное МСК).

    Поля: id, message, color, speed, target, group_ids, screen_ids (списки id), until_at,
    started_at, started_by, changed_at, changed_by. Истёкшие, но ещё не снятые Celery, не попадают.
    """
    rows = db.execute(text(_SELECT_TICKERS + """
        WHERE t.until_at IS NULL OR t.until_at > :now
        ORDER BY t.started_at, t.id
    """), {"now": now}).fetchall()
    return [_ticker(r) for r in rows]


def screens_brief(db) -> list[dict]:
    """Все экраны с тем, что нужно панели, чтобы объяснить «бежит / не бежит».

    id, name, group_id, group_name, online, offline_for_seconds, agent_version и последний отчёт
    агента: ticker_ids (список id или None), ticker_stopped, ticker_screen_off (None — отчёта не было).
    «В сети» считает screen_online в SQL (порог 5 минут), своего правила здесь нет.
    """
    rows = db.execute(text(f"""
        SELECT s.id, s.name, s.group_id, g.name AS group_name,
               {online_sql("s")} AS online, {offline_for_sql("s")} AS offline_for_seconds,
               s.agent_version, s.ticker_ids, s.ticker_stopped, s.ticker_screen_off
        FROM screens s LEFT JOIN sync_groups g ON g.id = s.group_id
        ORDER BY s.id
    """)).fetchall()
    return [{"id": r.id, "name": r.name, "group_id": r.group_id, "group_name": r.group_name,
             "online": bool(r.online), "offline_for_seconds": r.offline_for_seconds,
             "agent_version": r.agent_version,
             "ticker_ids": None if r.ticker_ids is None else list(r.ticker_ids),
             "ticker_stopped": r.ticker_stopped, "ticker_screen_off": r.ticker_screen_off}
            for r in rows]


def groups_brief(db) -> list[dict]:
    """Группы экранов для выбора «где»: `[{id, name, screen_count}]` по имени; пустая группа — 0 экранов."""
    rows = db.execute(text("""
        SELECT g.id, g.name, (SELECT COUNT(*) FROM screens s WHERE s.group_id = g.id) AS screen_count
        FROM sync_groups g
        ORDER BY g.name, g.id
    """)).fetchall()
    return [{"id": r.id, "name": r.name, "screen_count": int(r.screen_count)} for r in rows]


def for_screen(db, screen_id: int, now: datetime) -> list[dict]:
    """Строки для ленты экрана: `[{id, message, color, speed, until_at}]` в порядке запуска.

    `until_at` — ISO без пояса (наивное МСК) или None («до снятия»). Список собирает
    tickers_for_screen: срок, «все / группы / экраны» и группа экрана учитываются в момент `now`.
    Неизвестный экран получает только строки «на все».
    """
    r = db.execute(text("SELECT group_id FROM screens WHERE id = :sid"), {"sid": screen_id}).fetchone()
    mine = tickers_for_screen(list_live(db, now), screen_id, r.group_id if r else None, now)
    return [{"id": t["id"], "message": t["message"], "color": t["color"], "speed": t["speed"],
             "until_at": t["until_at"].isoformat() if t["until_at"] else None} for t in mine]


def targets_of(db, ticker_id: int) -> set[int]:
    """Экраны строки сейчас: «все» / состав групп / выбранные экраны на этот момент.

    Нет такой строки — пустое множество. Срок не проверяется: это вход notify_screens
    (кому сообщить об изменении), а не ответ «видна ли строка».
    """
    rows = db.execute(text(_SELECT_TICKERS + " WHERE t.id = :id"), {"id": ticker_id}).fetchall()
    if not rows:
        return set()
    t = _ticker(rows[0])
    return set(resolve_screens(t["target"], t["group_ids"], t["screen_ids"], _all_screens(db)))


# --- запись -------------------------------------------------------------------

def _require_exist(db, table: str, ids: list[int], missing_text: str) -> None:
    """Все ли id есть в таблице (screens / sync_groups): иначе ValueError до первой записи.

    Экран или группу могли удалить, пока окно в панели было открыто; без проверки это FK-ошибка и 500.
    """
    found = {r.id for r in db.execute(text(f"SELECT id FROM {table} WHERE id = ANY(:ids)"), {"ids": ids}).fetchall()}
    if found != set(ids):
        raise ValueError(missing_text)


def _check_targets(db, data: dict) -> None:
    if data["target"] == "screens":
        _require_exist(db, "screens", data["screen_ids"], "Экран не найден — обновите страницу")
    elif data["target"] == "groups":
        _require_exist(db, "sync_groups", data["group_ids"], "Группа не найдена — обновите страницу")


def _write_links(db, ticker_id: int, data: dict, replace: bool) -> None:
    """Связи строки с экранами / группами. replace=True — сначала убрать прежние (правка)."""
    if replace:
        db.execute(text("DELETE FROM ticker_screens WHERE ticker_id = :tid"), {"tid": ticker_id})
        db.execute(text("DELETE FROM ticker_groups WHERE ticker_id = :tid"), {"tid": ticker_id})
    if data["target"] == "screens":
        for sid in data["screen_ids"]:
            db.execute(text("INSERT INTO ticker_screens (ticker_id, screen_id) VALUES (:tid, :sid)"),
                       {"tid": ticker_id, "sid": sid})
    elif data["target"] == "groups":
        for gid in data["group_ids"]:
            db.execute(text("INSERT INTO ticker_groups (ticker_id, group_id) VALUES (:tid, :gid)"),
                       {"tid": ticker_id, "gid": gid})


def create(db, data: dict, actor: str, now: datetime) -> int:
    """Запустить строку: `data` — выход ticker_rules.parse_ticker_body. Возвращает id.

    `started_at = changed_at = now`, `started_by = changed_by = actor`; срок — `data["until_at"]`.
    Выбранные экран или группа, которых уже нет, — ValueError («обновите страницу»), ничего не записано.
    Старые поля `is_all` / `created_by` заполняются для совместимости.
    """
    _check_targets(db, data)
    ticker_id = db.execute(text("""
        INSERT INTO tickers (message, color, speed, target, is_all, until_at, created_by,
                             started_at, started_by, changed_at, changed_by)
        VALUES (:msg, :color, :speed, :target, :is_all, :until, :who, :now, :who, :now, :who)
        RETURNING id
    """), {"msg": data["message"], "color": data["color"], "speed": data["speed"], "target": data["target"],
           "is_all": data["target"] == "all", "until": data["until_at"], "who": actor, "now": now}).fetchone().id
    _write_links(db, ticker_id, data, replace=False)
    _audit(db, TITLE_ON, data["message"], actor)
    return ticker_id


def update(db, ticker_id: int, data: dict, actor: str, now: datetime) -> None:
    """Поправить идущую строку: текст, вид, «где», срок. Запуск (`started_*`) не меняется.

    `term_given=False` — срок в правке не трогали, остаётся прежний `until_at`; иначе новый срок
    (он уже посчитан от момента сохранения). Нет такой строки или срок вышел — LookupError.
    Строка читается под `FOR UPDATE`: Celery, снимающая её по сроку, дождётся правки и увидит новый срок.
    """
    row = db.execute(text("SELECT id, until_at FROM tickers WHERE id = :id FOR UPDATE"), {"id": ticker_id}).fetchone()
    if row is None or not ticker_live(row.until_at, now):
        raise LookupError(_NOT_FOUND)
    _check_targets(db, data)
    db.execute(text("""
        UPDATE tickers
        SET message = :msg, color = :color, speed = :speed, target = :target, is_all = :is_all,
            until_at = :until, changed_at = :now, changed_by = :who
        WHERE id = :id
    """), {"id": ticker_id, "msg": data["message"], "color": data["color"], "speed": data["speed"],
           "target": data["target"], "is_all": data["target"] == "all",
           "until": data["until_at"] if data["term_given"] else row.until_at, "now": now, "who": actor})
    _write_links(db, ticker_id, data, replace=True)
    _audit(db, TITLE_CHANGED, data["message"], actor)


def remove(db, ticker_id: int, actor: str, now: datetime) -> None:
    """Снять строку вручную. Нет такой строки — LookupError. `now` — для единообразия вызовов."""
    r = db.execute(text("DELETE FROM tickers WHERE id = :id RETURNING message"), {"id": ticker_id}).fetchone()
    if r is None:
        raise LookupError(_NOT_FOUND)
    _audit(db, TITLE_OFF, r.message, actor)


def expire_due(db, now: datetime) -> list[dict]:
    """Снять строки, у которых срок вышел (until_at <= now); «до снятия» не трогаем.

    Возвращает снятые строки: `[{id, message, targets}]`, `targets` — экраны строки на момент снятия
    (список id). Их нужно прочитать ДО удаления: связи с группами и экранами уходят вместе со строкой.
    Строки блокируются `FOR UPDATE`, поэтому правка, успевшая продлить срок, не потеряется: снятие
    дождётся её и увидит новый срок. По каждой снятой строке — одна запись «снята по сроку» от имени системы.

    Агент знает срок и снимает строку сам, но у старого агента (< 1.4.0) строка одна: если вышел срок
    самой новой, а другая ещё живёт, ему нужен ticker_show этой другой — для этого вызывающий передаёт
    `targets` в notify_screens (before = эти экраны, after = пусто).
    """
    rows = db.execute(text(_SELECT_TICKERS + """
        WHERE t.until_at <= :now
        ORDER BY t.id
        FOR UPDATE OF t
    """), {"now": now}).fetchall()
    if not rows:
        return []
    screens = _all_screens(db)
    expired = []
    for r in rows:
        t = _ticker(r)
        expired.append({"id": t["id"], "message": t["message"],
                        "targets": resolve_screens(t["target"], t["group_ids"], t["screen_ids"], screens)})
    db.execute(text("DELETE FROM tickers WHERE id = ANY(:ids)"), {"ids": [t["id"] for t in expired]})
    for t in expired:
        _audit(db, TITLE_EXPIRED, t["message"], SYSTEM_ACTOR)
    return expired


# --- отчёт экрана -------------------------------------------------------------

def record_report(db, screen_id: int, state: dict | None) -> None:
    """Запомнить последний отчёт агента 1.4.0: `{"ids": [...], "stopped": bool, "screen_off": bool}`.

    None — отчёта нет (старый агент), экран не трогаем. `ids` должен быть уже проверенным списком int:
    ошибка здесь сломала бы heartbeat.
    """
    if state is None:
        return
    db.execute(text("""
        UPDATE screens
        SET ticker_ids = CAST(:ids AS INTEGER[]), ticker_stopped = :stopped, ticker_screen_off = :off
        WHERE id = :sid
    """), {"sid": screen_id, "ids": list(state.get("ids") or []),
           "stopped": bool(state.get("stopped")), "off": bool(state.get("screen_off"))})


# --- команды старым агентам ---------------------------------------------------

TICKER_COMMANDS = ("ticker_show", "ticker_hide")


def queue_ticker_command(db, screen_id: int, cmd_type: str, payload: dict) -> None:
    """Поставить старому агенту ticker_show / ticker_hide, предварительно погасив его ожидающие ticker_*.

    Команды строки — состояние («что на экране сейчас»), а не события: важна только последняя. Старый
    агент (< 1.4.0) по WS берёт ОДНУ команду за будильник, поэтому забытая ticker_show (срок вышел, пока
    экран был не в сети, или «Стоп» поставил ticker_hide и ничего не разбудило) съела бы будильник
    следующего запуска или «Показа», а при разборе дала бы устаревшие `duration` и текст. Поэтому
    в очереди экрана остаётся не больше одной команды строки — самая новая. Прочие команды (stop,
    resume, restart_agent, …) не трогаются. Погашенная команда помечается выполненной (executed_at), а не
    удаляется: если агент как раз получил её, его подтверждение всё ещё находит строку. Метка — часы БД,
    как у подтверждения агента (ack_command) и created_at; это учёт очереди, а не момент строки.
    """
    if cmd_type not in TICKER_COMMANDS:
        raise ValueError(f"Не команда бегущей строки: {cmd_type}")
    db.execute(text("""
        UPDATE commands SET executed_at = NOW()
        WHERE screen_id = :sid AND executed_at IS NULL AND type IN ('ticker_show', 'ticker_hide')
    """), {"sid": screen_id})
    db.execute(text("INSERT INTO commands (screen_id, type, payload) VALUES (:sid, :t, CAST(:p AS jsonb))"),
               {"sid": screen_id, "t": cmd_type, "p": json.dumps(payload)})


def _show_payload(t: dict, now: datetime) -> dict:
    """Что старому агенту показать: `duration` — оставшиеся секунды (0 у него значит «бессрочно»,
    поэтому остаток меньше секунды округляется вверх, а не в ноль)."""
    left = 0 if t["until_at"] is None else max(1, math.ceil((t["until_at"] - now).total_seconds()))
    return {"message": t["message"], "color": t["color"], "speed": t["speed"], "duration": left}


def notify_screens(db, before: set[int], after: set[int], now: datetime) -> list[int]:
    """Поставить в очередь команды старым агентам после изменения строки; вернуть затронутые экраны.

    `before` — экраны строки до изменения, `after` — после (запуск: before пусто; снятие: after пусто;
    правка: оба). Каждому экрану из объединения: если у него есть живые строки — `ticker_show` самой
    новой (снятие одной из нескольких не гасит остальные), иначе `ticker_hide`. Вызывать после изменения,
    в той же транзакции. Исчезнувшие экраны пропускаются. Возвращённые id нужны для WS-будильника.
    Прежние ожидающие ticker_show / ticker_hide экрана гасятся (queue_ticker_command): в очереди остаётся
    одна, актуальная команда строки на экран.
    """
    affected = sorted(before | after)
    if not affected:
        return []
    live = list_live(db, now)
    group_of = dict(_all_screens(db))
    notified = []
    for sid in affected:
        if sid not in group_of:
            continue
        mine = tickers_for_screen(live, sid, group_of[sid], now)
        if mine:
            queue_ticker_command(db, sid, "ticker_show", _show_payload(mine[-1], now))
        else:
            queue_ticker_command(db, sid, "ticker_hide", {})
        notified.append(sid)
    return notified


# --- шаблоны ------------------------------------------------------------------

def list_templates(db) -> list[dict]:
    """Шаблоны объявлений в порядке создания: id, message, color, speed, term_kind, term_value."""
    rows = db.execute(text("""
        SELECT id, message, color, speed, term_kind, term_value FROM ticker_templates ORDER BY id
    """)).fetchall()
    return [{"id": r.id, "message": r.message, "color": r.color, "speed": r.speed,
             "term_kind": r.term_kind, "term_value": r.term_value} for r in rows]


def create_template(db, data: dict, actor: str) -> int:
    """Новый шаблон: `data` — message, color, speed, term_kind (cancel | minutes | hour), term_value (int | None)."""
    return db.execute(text("""
        INSERT INTO ticker_templates (message, color, speed, term_kind, term_value, created_by)
        VALUES (:msg, :color, :speed, :kind, :value, :who)
        RETURNING id
    """), {"msg": data["message"], "color": data["color"], "speed": data["speed"],
           "kind": data["term_kind"], "value": data["term_value"], "who": actor}).fetchone().id


def update_template(db, template_id: int, data: dict) -> None:
    """Переписать шаблон. Нет такого — LookupError."""
    r = db.execute(text("""
        UPDATE ticker_templates
        SET message = :msg, color = :color, speed = :speed, term_kind = :kind, term_value = :value
        WHERE id = :id
        RETURNING id
    """), {"id": template_id, "msg": data["message"], "color": data["color"], "speed": data["speed"],
           "kind": data["term_kind"], "value": data["term_value"]}).fetchone()
    if r is None:
        raise LookupError(_TEMPLATE_NOT_FOUND)


def delete_template(db, template_id: int) -> None:
    """Удалить шаблон. Нет такого — LookupError."""
    r = db.execute(text("DELETE FROM ticker_templates WHERE id = :id RETURNING id"), {"id": template_id}).fetchone()
    if r is None:
        raise LookupError(_TEMPLATE_NOT_FOUND)
