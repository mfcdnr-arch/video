"""Логика показов: даты, часы, статусы, пересечения, выбор плейлистов.

Чистая логика на stdlib, без БД и без fastapi: на вход — простые dict,
на выход — списки и кортежи. Её используют API показов (создание/правка,
проверка пересечений) и лента экрана (какой плейлист играть в этот час).

Все даты и часы — наивное московское время (МСК). Показ действует на днях
[date_from .. date_to] ВКЛЮЧИТЕЛЬНО и в часах [hour_from, hour_to) —
полуинтервал, hour_to может быть 24.

Структуры (dict):
  показ          id, playlist_id, playlist_name, date_from, date_to,
                 hour_from, hour_to, stopped_at (datetime | None), screens (set[int]),
                 saved_at (datetime, наивное МСК — когда показ создан или правился
                 последний раз; нужен для приоритета, см. choose_playlists)
  переопределение id, screen_id, on_date, playlist_id, playlist_name, is_off
                 (групповые переопределения уже развёрнуты по экранам)
  решения        {(placement_id, screen_id, other_kind, other_id): "replace" | "alternate"},
                 other_kind — "placement" или "override"

Пересечение показов админ разрешает отдельно для каждого экрана:
«replace» — последний сохранённый показ вытесняет остальные, «alternate» —
играют по очереди в общем часу. Недельный шаблон пересечением не считается.
Решения хранятся под id показа, который админ сохранял (создавал или правил),
— поэтому приоритет у показа с самым поздним saved_at, а не с наибольшим id.
"""
from datetime import date, datetime, time, timedelta

REPLACE = "replace"
ALTERNATE = "alternate"

KIND_PLACEMENT = "placement"
KIND_OVERRIDE = "override"

_HOUR = timedelta(hours=1)


# --- даты, часы, действие показа ---------------------------------------------

def dates_overlap(a_from: date, a_to: date, b_from: date, b_to: date) -> tuple[date, date] | None:
    """Общие дни двух диапазонов (границы включительно) или None."""
    lo, hi = max(a_from, b_from), min(a_to, b_to)
    return (lo, hi) if lo <= hi else None


def hours_overlap(a_from: int, a_to: int, b_from: int, b_to: int) -> tuple[int, int] | None:
    """Общие часы двух полуинтервалов [from, to) или None; (9,13) и (13,15) не пересекаются."""
    lo, hi = max(a_from, b_from), min(a_to, b_to)
    return (lo, hi) if lo < hi else None


def applies_at(p: dict, day: date, hour: int) -> bool:
    """Играет ли показ p в час `hour` дня `day` (МСК).

    У остановленного показа остаются только часы, которые закончились не
    позже stopped_at: остановка в 10:37 гасит час 10–11, а 9–10 остаётся.
    """
    if not (p["date_from"] <= day <= p["date_to"]):
        return False
    if not (p["hour_from"] <= hour < p["hour_to"]):
        return False
    stopped_at = p.get("stopped_at")
    if stopped_at is not None:
        return datetime.combine(day, time(hour)) + _HOUR <= stopped_at
    return True


def placement_status(p: dict, now: datetime) -> str:
    """"stopped" | "done" | "planned" | "active" на момент now (МСК)."""
    if p.get("stopped_at") is not None:
        return "stopped"
    today = now.date()
    if today > p["date_to"] or (today == p["date_to"] and now.hour >= p["hour_to"]):
        return "done"
    if today < p["date_from"]:
        return "planned"
    return "active"


# --- пересечения -------------------------------------------------------------

def find_conflicts(new: dict, others: list[dict], overrides: list[dict]) -> tuple[list[dict], list[dict]]:
    """Что пересекается с новым показом `new`.

    Возвращает (пересечения, выключенные дни):
      пересечения — по одному на экран и на пересекающийся показ/переопределение:
        {"screen_id", "other_kind", "other_id", "other_playlist_id",
         "other_playlist_name", "date_from", "date_to", "hour_from", "hour_to"}
        даты и часы — само пересечение; переопределение с плейлистом занимает
        весь день, поэтому его часы равны часам нового показа;
      выключенные дни — переопределения is_off в днях нового показа:
        {"screen_id", "on_date"} (это не выбор replace/alternate, а предупреждение).

    Пропускаются остановленные показы и сам редактируемый показ (тот же id).
    Экраны в `screens` уже развёрнуты вызывающим (в т.ч. для «всех экранов»).
    Порядок: по screen_id, затем по дате.
    """
    new_id = new.get("id")
    new_screens = new["screens"]
    conflicts: list[dict] = []

    for other in others:
        if other.get("stopped_at") is not None:
            continue
        if new_id is not None and other.get("id") == new_id:
            continue
        common = new_screens & other["screens"]
        if not common:
            continue
        dates = dates_overlap(new["date_from"], new["date_to"], other["date_from"], other["date_to"])
        hours = hours_overlap(new["hour_from"], new["hour_to"], other["hour_from"], other["hour_to"])
        if dates is None or hours is None:
            continue
        for screen_id in common:
            conflicts.append({
                "screen_id": screen_id, "other_kind": KIND_PLACEMENT, "other_id": other["id"],
                "other_playlist_id": other["playlist_id"], "other_playlist_name": other["playlist_name"],
                "date_from": dates[0], "date_to": dates[1],
                "hour_from": hours[0], "hour_to": hours[1],
            })

    off_days: list[dict] = []
    for ov in overrides:
        if ov["screen_id"] not in new_screens:
            continue
        if not (new["date_from"] <= ov["on_date"] <= new["date_to"]):
            continue
        if ov.get("is_off"):
            off_days.append({"screen_id": ov["screen_id"], "on_date": ov["on_date"]})
        elif ov.get("playlist_id") is not None:
            conflicts.append({
                "screen_id": ov["screen_id"], "other_kind": KIND_OVERRIDE, "other_id": ov["id"],
                "other_playlist_id": ov["playlist_id"], "other_playlist_name": ov.get("playlist_name"),
                "date_from": ov["on_date"], "date_to": ov["on_date"],
                "hour_from": new["hour_from"], "hour_to": new["hour_to"],
            })

    conflicts.sort(key=lambda c: (c["screen_id"], c["date_from"]))
    off_days.sort(key=lambda d: (d["screen_id"], d["on_date"]))
    return conflicts, off_days


# --- выбор плейлистов --------------------------------------------------------

def _recency(p: dict) -> tuple[datetime, int]:
    """Ключ «новизны» показа: когда сохранён, при равенстве/отсутствии — id."""
    return (p.get("saved_at") or datetime.min, p["id"])


def choose_playlists(applicable: list[dict], decisions: dict, override: dict | None, screen_id: int) -> list[int]:
    """Какие плейлисты играют на экране `screen_id` в конкретный час.

    applicable — показы, действующие в этот час на этом экране;
    override   — переопределение экрана на этот день (или None).

    «Новейший» — последний сохранённый показ: максимум по (saved_at, id).
    Правка старого показа делает его новейшим, а решения при сохранении
    записываются под его id — значит, они лежат именно у новейшего.
    Без saved_at (или при равных) порядок по id: больший id новее.

    Новейший показ идёт первым. Старшие показы (в том же порядке, от новых
    к старым) и переопределение с плейлистом (не is_off) добавляются только
    если у новейшего на них стоит решение "alternate" — по очереди после
    него. Без решения или при "replace" они вытеснены. Без повторов
    playlist_id. Пусто, если applicable пуст.
    """
    if not applicable:
        return []
    ordered = sorted(applicable, key=_recency, reverse=True)   # новейший первым
    newest = ordered[0]

    def alternates(kind: str, other_id: int) -> bool:
        return decisions.get((newest["id"], screen_id, kind, other_id)) == ALTERNATE

    chosen = [newest["playlist_id"]]
    for older in ordered[1:]:
        if alternates(KIND_PLACEMENT, older["id"]):
            chosen.append(older["playlist_id"])
    if override and not override.get("is_off") and override.get("playlist_id") is not None \
            and alternates(KIND_OVERRIDE, override["id"]):
        chosen.append(override["playlist_id"])

    return list(dict.fromkeys(chosen))   # без повторов, порядок сохраняется


# --- недельный шаблон --------------------------------------------------------

def template_playlist(slots: list[dict], dow: int, hour: int) -> int | None:
    """Плейлист недельного шаблона для дня недели `dow` и часа `hour`.

    slots: {"day_of_week", "hour" (None — весь день), "playlist_id", "prio"}.
    Почасовой слот приоритетнее «весь день»; внутри группы — больший prio.
    """
    day_slots = [s for s in slots if s["day_of_week"] == dow]
    for group in ([s for s in day_slots if s.get("hour") == hour],
                  [s for s in day_slots if s.get("hour") is None]):
        if group:
            return max(group, key=lambda s: s.get("prio") or 0)["playlist_id"]
    return None


# --- недельная сетка и «по очереди» -------------------------------------------

KIND_OFF = "off"
KIND_ALTERNATE = "alternate"
KIND_TEMPLATE = "template"
KIND_EMPTY = "empty"


def week_cells(days: list[date], screen_id: int, placements: list[dict], decisions: dict,
               overrides: list[dict], slots: list[dict], names: dict[int, str]) -> list[dict]:
    """Что играет на экране `screen_id` в каждый час каждого из `days` (МСК).

    Источники по убыванию приоритета (спека §4.7, без «Эфира сети» — он живёт
    «сейчас», а не по датам): переопределение «выключен» → показы по датам →
    переопределение с плейлистом → недельный шаблон → пусто.

    placements — показы (любых экранов; берутся только этого экрана),
    overrides — переопределения (берутся только этого экрана),
    slots — слоты шаблона, уже выбранные для этого экрана (см. template_playlist),
    names — {playlist_id: название}; недостающее берётся из самих показов и
    переопределений.

    Клетка: {"date", "hour", "kind", "playlists": [названия], "playlist_ids": [id],
    "placement_id"}. kind: "off" | "placement" | "alternate" (в клетке ≥2
    плейлиста, они чередуются) | "override" | "template" | "empty".
    placement_id — новейший из показов, действующих в этот час (иначе None).
    Порядок клеток: по дням, внутри дня — по часам.
    """
    mine = [p for p in placements if screen_id in p["screens"]]
    override_on = {o["on_date"]: o for o in overrides if o["screen_id"] == screen_id}

    known: dict[int, str | None] = {p["playlist_id"]: p.get("playlist_name") for p in mine}
    known.update({o["playlist_id"]: o.get("playlist_name") for o in override_on.values()
                  if o.get("playlist_id") is not None})

    def name_of(playlist_id: int) -> str:
        return names.get(playlist_id) or known.get(playlist_id) or f"Плейлист #{playlist_id}"

    cells = []
    for day in days:
        ov = override_on.get(day)
        is_off = bool(ov and ov.get("is_off"))
        for hour in range(24):
            kind, ids, placement_id = KIND_EMPTY, [], None
            applicable = [] if is_off else [p for p in mine if applies_at(p, day, hour)]
            if is_off:
                kind = KIND_OFF
            elif applicable:
                ids = choose_playlists(applicable, decisions, ov, screen_id)
                placement_id = max(applicable, key=_recency)["id"]
                kind = KIND_ALTERNATE if len(ids) >= 2 else KIND_PLACEMENT
            elif ov and ov.get("playlist_id") is not None:
                kind, ids = KIND_OVERRIDE, [ov["playlist_id"]]
            else:
                template_id = template_playlist(slots, day.weekday(), hour)
                if template_id is not None:
                    kind, ids = KIND_TEMPLATE, [template_id]
            cells.append({"date": day, "hour": hour, "kind": kind,
                          "playlists": [name_of(i) for i in ids], "playlist_ids": ids,
                          "placement_id": placement_id})
    return cells


def alternate_overlaps(p: dict, others: list[dict], overrides: list[dict], decisions: dict) -> list[dict]:
    """Пересечения показа `p`, которые админ решил «крутить по очереди».

    Возвращает элементы find_conflicts (экран, другой плейлист, даты и часы
    пересечения). Решение берётся у того из пары, кто сохранён позже, — как в
    choose_playlists, где решает новейший показ; поэтому «по очереди» видно в
    карточках обоих показов. Переопределение «новее» быть не может — его
    решение лежит у самого p. Пересечение с тем же плейлистом пропускается:
    сам с собой он не чередуется. `others` может содержать и сам p.
    """
    conflicts, _ = find_conflicts(p, others, overrides)
    by_id = {o["id"]: o for o in others}
    out = []
    for c in conflicts:
        if c["other_playlist_id"] == p["playlist_id"]:
            continue
        if c["other_kind"] == KIND_PLACEMENT:
            other = by_id[c["other_id"]]
            owner, rest = (p, other) if _recency(p) >= _recency(other) else (other, p)
            key = (owner["id"], c["screen_id"], KIND_PLACEMENT, rest["id"])
        else:
            key = (p["id"], c["screen_id"], KIND_OVERRIDE, c["other_id"])
        if decisions.get(key) == ALTERNATE:
            out.append(c)
    return out
