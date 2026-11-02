"""Правила бегущей строки (срочные объявления): срок, «где», строки экрана, подтверждение.

Чистая логика на stdlib и broadcast_rules, без БД и без fastapi. Все моменты —
наивное московское время (МСК), как в показах и общем эфире: срок хранится и
сравнивается в МСК из Python (now_msk), а не в UTC контейнера Postgres.

Срок строки — `until_at`. Его нет (None) — «до снятия»: строка бежит, пока её не
снимут вручную, полночь её не трогает. Срок есть — строка живёт до этого момента,
не включая: в момент until_at она уже снята. Задаётся срок тремя способами:
«до снятия», «до ЧЧ:00 сегодня» (целый час, как в общем эфире) и «на N минут»
(N от 1 до 1440, может перейти через полночь).

«Где» — `target`: все экраны, группы или выбранные экраны. Список экранов строки
получается в момент показа (resolve_screens), а не при запуске: экран, добавленный
или перенесённый в группу позже, тоже получает строку, а удалённая группа или
экран просто не попадают в результат.

Подтверждение: агент 1.4.0 в heartbeat сообщает, какие строки у него бегут.
Панель верит этому отчёту, а не расчёту сервера (часы мини-ПК могут быть сбиты),
и объясняет, почему строка на экране не подтверждена (screen_reason).
"""
import re
from datetime import date, datetime, timedelta

from broadcast_rules import STALE_WINDOW_TEXT, parse_until_hour

MAX_LEN = 200                                   # длина текста после strip()
MAX_MINUTES = 1440                              # «на N минут»: сутки
SPEEDS = ("slow", "medium", "fast")
TARGETS = ("all", "groups", "screens")
CONFIRM_VERSION = (1, 4, 0)                     # с этой версии агент шлёт ticker_state
PENDING_SECONDS = 90                            # окно «ждём подтверждения» от запуска или правки

DEFAULT_COLOR = "#ffd34d"
DEFAULT_SPEED = "medium"

REASONS = ("running", "offline", "old_agent", "stopped", "screen_off", "pending", "not_confirmed")
REASON_TEXT = {
    "running": "бежит",
    "offline": "не в сети",
    "old_agent": "старый агент — без подтверждения",
    "stopped": "остановлен («Стоп»)",
    "screen_off": "экран выключен по расписанию питания",
    "pending": "ждём подтверждения",
    "not_confirmed": "не подтвердил — проверьте экран",
}

_MAX_INT4 = 2 ** 31 - 1                         # id экранов и групп — SERIAL
_ON_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_COLOR = re.compile(r"#[0-9a-fA-F]{6}")
_NEWLINES = re.compile(r"[\r\n  ]+")
_VERSION = re.compile(r"[0-9]{1,6}(?:\.[0-9]{1,6}){0,3}")

_TERM_TEXT = "Срок — «до снятия», «до ЧЧ:00 сегодня» или «на N минут»"
_WHERE_TEXT = "Где показывать — на всех экранах, в группах или на выбранных экранах"
_HOUR_TEXT = "Час окончания — целое от 1 до 24"


# --- срок ---------------------------------------------------------------------

def _parse_on_date(value) -> date:
    """`on_date` — МСК-дата, на которую открыто окно в панели, строго ГГГГ-ММ-ДД."""
    if isinstance(value, str) and _ON_DATE.fullmatch(value):
        try:
            return date.fromisoformat(value)
        except ValueError:
            pass
    raise ValueError("Дата окна — строка вида ГГГГ-ММ-ДД")


def parse_term(term: dict | None, now: datetime) -> datetime | None:
    """Срок из тела запроса → момент окончания (наивное МСК); None — «до снятия».

    `{"kind": "cancel"}` и отсутствие срока — «до снятия»; `{"kind": "minutes", "minutes": N}` —
    `now` + N минут, N целое от 1 до 1440; `{"kind": "hour", "hour": H, "on_date": "ГГГГ-ММ-ДД"}` —
    целый час сегодня по правилам общего эфира (parse_until_hour). `on_date` необязателен: если он
    есть и уже не сегодняшний, окно в панели устарело (STALE_WINDOW_TEXT). Всё остальное — ValueError
    с русским текстом: мусор нельзя тихо превращать в «до снятия», поэтому и «час» без часа — ошибка.
    """
    if term is None:
        return None
    if not isinstance(term, dict):
        raise ValueError(_TERM_TEXT)
    kind = term.get("kind")
    if kind == "cancel":
        return None
    if kind == "minutes":
        minutes = term.get("minutes")
        if isinstance(minutes, bool) or not isinstance(minutes, int) or not 1 <= minutes <= MAX_MINUTES:
            raise ValueError(f"Минут — целое от 1 до {MAX_MINUTES}")
        return now + timedelta(minutes=minutes)
    if kind == "hour":
        if term.get("on_date") is not None and _parse_on_date(term["on_date"]) != now.date():
            raise ValueError(STALE_WINDOW_TEXT)
        if term.get("hour") is None:
            raise ValueError(_HOUR_TEXT)            # parse_until_hour(None) — это «до отмены», тут нельзя
        return parse_until_hour(term["hour"], now)
    raise ValueError(_TERM_TEXT)


def _legacy_until(duration, now: datetime) -> datetime | None:
    """Старое поле `duration` (секунды): 0 — «до снятия», больше суток и не целое — ошибка."""
    if isinstance(duration, bool) or not isinstance(duration, int) or not 0 <= duration <= MAX_MINUTES * 60:
        raise ValueError(f"Длительность — целое число секунд от 0 до {MAX_MINUTES * 60} (0 — до снятия)")
    return now + timedelta(seconds=duration) if duration else None


def ticker_live(until_at: datetime | None, now: datetime) -> bool:
    """Идёт ли строка в момент `now`: без срока — да, со сроком — строго до until_at."""
    return until_at is None or now < until_at


# --- тело запроса -------------------------------------------------------------

def _message(value) -> str:
    """Текст строки: переводы строк → пробел, 1…200 символов после strip()."""
    if not isinstance(value, str):
        raise ValueError("Текст строки не может быть пустым")
    text = _NEWLINES.sub(" ", value).strip()
    if not text:
        raise ValueError("Текст строки не может быть пустым")
    if len(text) > MAX_LEN:
        raise ValueError(f"Текст строки — не длиннее {MAX_LEN} символов")
    return text


def _id_list(value, empty_text: str, bad_text: str) -> list[int]:
    """Непустой список id из JSON → отсортированный, без повторов; всё, что не id, — ValueError."""
    if value is None:
        raise ValueError(empty_text)
    if not isinstance(value, (list, tuple)):
        raise ValueError(bad_text)
    ids = set()
    for item in value:
        if isinstance(item, bool) or not isinstance(item, int) or not 0 < item <= _MAX_INT4:
            raise ValueError(bad_text)
        ids.add(item)
    if not ids:
        raise ValueError(empty_text)
    return sorted(ids)


def parse_ticker_body(body: dict, now: datetime, keep_term: bool = False) -> dict:
    """Тело POST/PUT /tickers → {message, color, speed, target, group_ids, screen_ids, until_at, term_given}.

    Основной вид: {"message", "color", "speed", "target": all|groups|screens, "group_ids", "screen_ids",
    "term": {...}}. Понимает и старое тело: `is_all` вместо `target` (нет — значит «все»), `duration`
    в секундах вместо `term` (0 или нет — «до снятия»). `term` главнее `duration`. Цвет по умолчанию
    жёлтый #ffd34d, скорость — medium. Список id берётся только тот, что относится к `target`: для
    «все» оба пусты. Всё неверное — ValueError с русским текстом.

    `keep_term=True` — для правки: если срок в теле не задан ни `term`, ни `duration`, то
    term_given=False и until_at=None, а вызывающий оставляет прежний срок строки. Без keep_term
    срок не задан = «до снятия», term_given=True. `term: null` считается «не задан».
    """
    if not isinstance(body, dict):
        raise ValueError("Ожидается JSON-объект со строкой")
    message = _message(body.get("message"))

    color = body.get("color")
    if color is None:
        color = DEFAULT_COLOR
    elif not isinstance(color, str) or not _COLOR.fullmatch(color):
        raise ValueError("Цвет — в виде #RRGGBB")
    speed = body.get("speed")
    if speed is None:
        speed = DEFAULT_SPEED
    elif speed not in SPEEDS:
        raise ValueError("Скорость — slow, medium или fast")

    target = body.get("target")
    if target is None:
        target = "all" if body.get("is_all", True) else "screens"
    if target not in TARGETS:
        raise ValueError(_WHERE_TEXT)
    group_ids: list[int] = []
    screen_ids: list[int] = []
    if target == "groups":
        group_ids = _id_list(body.get("group_ids"), "Выберите хотя бы одну группу", "Номера групп — целые положительные числа")
    elif target == "screens":
        screen_ids = _id_list(body.get("screen_ids"), "Выберите хотя бы один экран", "Номера экранов — целые положительные числа")

    if body.get("term") is not None:
        until_at, term_given = parse_term(body["term"], now), True
    elif body.get("duration") is not None:
        until_at, term_given = _legacy_until(body["duration"], now), True
    else:
        until_at, term_given = None, not keep_term

    return {"message": message, "color": color.lower(), "speed": speed, "target": target,
            "group_ids": group_ids, "screen_ids": screen_ids, "until_at": until_at, "term_given": term_given}


# --- «где» → экраны, строки экрана --------------------------------------------

def resolve_screens(target: str, group_ids: list[int], screen_ids: list[int],
                    screens: list[tuple[int, int | None]]) -> list[int]:
    """Экраны, на которые нацелена строка. `screens` — все экраны (id, group_id).

    Результат отсортирован; id экранов и групп, которых больше нет, отбрасываются, так что
    строка на удалённую группу — это «на 0 экранах», а не ошибка.
    """
    if target == "all":
        return sorted({sid for sid, _ in screens})
    if target == "groups":
        wanted = set(group_ids)
        return sorted({sid for sid, gid in screens if gid in wanted})
    if target == "screens":
        wanted = set(screen_ids)
        return sorted({sid for sid, _ in screens if sid in wanted})
    raise ValueError(_WHERE_TEXT)


def _aimed_at(ticker: dict, screen_id: int, group_id: int | None) -> bool:
    target = ticker["target"]
    if target == "all":
        return True
    if target == "groups":
        return group_id is not None and group_id in ticker["group_ids"]
    if target == "screens":
        return screen_id in ticker["screen_ids"]
    return False


def tickers_for_screen(tickers: list[dict], screen_id: int, group_id: int | None, now: datetime) -> list[dict]:
    """Живые строки этого экрана в порядке запуска (`started_at`, затем `id`).

    Каждая строка — dict {id, message, color, speed, target, group_ids, screen_ids, until_at,
    started_at}; возвращаются те же объекты. Истёкшие и чужие отбрасываются.
    """
    mine = [t for t in tickers if ticker_live(t["until_at"], now) and _aimed_at(t, screen_id, group_id)]
    return sorted(mine, key=lambda t: (t["started_at"], t["id"]))


# --- подтверждение с экрана ---------------------------------------------------

def version_tuple(v: str | None) -> tuple[int, ...]:
    """Версия агента "1.4.0" → (1, 4, 0) для сравнения; None и мусор → ()."""
    if not isinstance(v, str):
        return ()
    text = v.strip()
    if not _VERSION.fullmatch(text):
        return ()
    return tuple(int(part) for part in text.split("."))


def screen_reason(ticker_id: int, screen: dict, changed_at: datetime, now: datetime) -> str:
    """Почему строка на экране бежит или не бежит → код из REASONS.

    `screen` — {online, agent_version, ticker_ids, ticker_stopped, ticker_screen_off}: последний
    отчёт экрана (поля None, пока агент 1.4.0 ничего не присылал). `changed_at` — запуск или
    последняя правка строки. Порядок проверок важен: сначала то, из-за чего отчёту вообще нельзя
    верить (не в сети, старый агент), затем причины, по которым строки нет на кадре (Стоп, экран
    выключен), затем сам отчёт — «бежит» только если экран сам назвал id строки. Нет в отчёте:
    90 с после запуска или правки — «ждём», дольше — «не подтвердил».
    """
    if not screen.get("online"):
        return "offline"
    if version_tuple(screen.get("agent_version")) < CONFIRM_VERSION:
        return "old_agent"
    if screen.get("ticker_stopped"):
        return "stopped"
    if screen.get("ticker_screen_off"):
        return "screen_off"
    if ticker_id in (screen.get("ticker_ids") or ()):
        return "running"
    if now - changed_at < timedelta(seconds=PENDING_SECONDS):
        return "pending"
    return "not_confirmed"


def summarize(rows: list[tuple[str, str]]) -> dict:
    """Пары (имя экрана, причина) → {total, running, problems: [{screen, reason, text}]}.

    В problems — всё, что не «бежит», в порядке входа.
    """
    problems = [{"screen": name, "reason": reason, "text": REASON_TEXT[reason]}
                for name, reason in rows if reason != "running"]
    return {"total": len(rows), "running": len(rows) - len(problems), "problems": problems}
