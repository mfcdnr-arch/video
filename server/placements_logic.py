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
  решения        {(placement_id, screen_id, other_kind, other_id): "replace" | "alternate" | "yield"},
                 other_kind — "placement" или "override"

Пересечение показов админ разрешает отдельно для каждого экрана:
«replace» — последний сохранённый показ вытесняет остальные, «alternate» —
играют по очереди в общем часу, «yield» — сохранённый показ уступает: в часы
пересечения он не идёт, идёт другой показ или переопределение («Оставить «X»
на это время»). Недельный шаблон пересечением не считается.
Решения хранятся под id показа, который админ сохранял (создавал или правил),
— поэтому приоритет у показа с самым поздним saved_at, а не с наибольшим id.
"""
from datetime import date, datetime, time, timedelta

REPLACE = "replace"
ALTERNATE = "alternate"
YIELD = "yield"

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


def _top(ordered: list[dict], decisions: dict, screen_id: int) -> int:
    """Индекс вершины часа в ordered.

    ordered — применимые показы от новейшего к старшему (_recency), не пуст.
    Начинаем с новейшего. Если у вершины есть решение "yield" против показа
    СТАРШЕ неё (дальше по ordered), вершиной становится тот (из нескольких —
    новейший), и так дальше. Решения вершины о показах новее неё устарели:
    их сохраняли раньше, чем те показы решили о ней, — поэтому они не
    действуют. Путь идёт только к старшим, циклов не бывает.
    """
    i = 0
    while True:
        top_id = ordered[i]["id"]
        nxt = next((j for j in range(i + 1, len(ordered))
                    if decisions.get((top_id, screen_id, KIND_PLACEMENT, ordered[j]["id"])) == YIELD), None)
        if nxt is None:
            return i
        i = nxt


def _choose(applicable: list[dict], decisions: dict, override: dict | None,
            screen_id: int) -> tuple[list[int], int | None]:
    """choose_playlists + id показа-вершины (None, если показов в этот час нет)."""
    if not applicable:
        return [], None
    ordered = sorted(applicable, key=_recency, reverse=True)   # новейший первым
    i = _top(ordered, decisions, screen_id)
    top = ordered[i]

    def decided(kind: str, other_id: int) -> str | None:
        return decisions.get((top["id"], screen_id, kind, other_id))

    has_override = bool(override) and not override.get("is_off") and override.get("playlist_id") is not None
    if has_override and decided(KIND_OVERRIDE, override["id"]) == YIELD:
        return [], None

    chosen = [top["playlist_id"]]
    for older in ordered[i + 1:]:                 # только показы старше вершины, от новых к старым
        if decided(KIND_PLACEMENT, older["id"]) == ALTERNATE:
            chosen.append(older["playlist_id"])
    if has_override and decided(KIND_OVERRIDE, override["id"]) == ALTERNATE:
        chosen.append(override["playlist_id"])

    return list(dict.fromkeys(chosen)), top["id"]   # без повторов, порядок сохраняется


def choose_playlists(applicable: list[dict], decisions: dict, override: dict | None, screen_id: int) -> list[int]:
    """Какие плейлисты играют на экране `screen_id` в конкретный час.

    applicable — показы, действующие в этот час на этом экране;
    override   — переопределение экрана на этот день (или None).

    «Новейший» — последний сохранённый показ: максимум по (saved_at, id).
    Правка старого показа делает его новейшим, а решения при сохранении
    записываются под его id — значит, они лежат именно у новейшего.
    Без saved_at (или при равных) порядок по id: больший id новее.
    Из пары показов действует решение того, кто сохранён позже.

    «Вершина» — новейший, если он никому не уступил. Решение "yield"
    вершины против показа старше неё отдаёт вершину тому (см. _top: из
    нескольких — новейший, путь только к старшим). Показы новее вершины
    (уступившие по пути и прочие) в результат не попадают: их решения о
    вершине новее её решений о них. Если вершина уступает переопределению с
    плейлистом (не is_off) — [] : показов в этот час нет, лента берёт
    следующий шаг (переопределение с плейлистом, спека §4.7).

    Вершина идёт первой. Показы старше неё (от новых к старым) и
    переопределение с плейлистом добавляются только если у вершины на них
    стоит решение "alternate" — по очереди после неё. Без решения или при
    "replace" они вытеснены. Без повторов playlist_id. Пусто, если
    applicable пуст.
    """
    return _choose(applicable, decisions, override, screen_id)[0]


# Решение другой стороны пары, увиденное со стороны правленого показа:
# он «заменяет» нас → мы «уступаем» ему, и наоборот; «по очереди» — взаимно.
_REVERSE = {REPLACE: YIELD, YIELD: REPLACE, ALTERNATE: ALTERNATE}


def prefill_decisions(p: dict, conflicts: list[dict], others: list[dict], decisions: dict) -> list[dict]:
    """Решения, которые подставить в окно правки показа `p` по его нынешним пересечениям.

    p          — правленый показ как он сохранён (id, saved_at до правки);
    conflicts  — find_conflicts для новых значений показа;
    others     — показы, с которыми он может пересекаться (нужны saved_at);
    decisions  — решения p и этих показов (load_decisions).

    Подставляется то, что действует сейчас (choose_playlists: из пары решает
    сохранённый позже). Другой показ новее p — его решение о p, перевёрнутое
    на сторону p: «заменить» → "yield", "yield" → «заменить», «по очереди» →
    «по очереди»; о p он не решал — ничего (админ решит заново). Показ старше
    p и переопределение — собственное сохранённое решение p. Только по
    conflicts, без повторов; [{"screen_id", "other_kind", "other_id", "mode"}]
    в порядке conflicts.
    """
    by_id = {o["id"]: o for o in others}
    out, seen = [], set()
    for c in conflicts:
        key = (c["screen_id"], c["other_kind"], c["other_id"])
        if key in seen:
            continue
        seen.add(key)
        other = by_id.get(c["other_id"]) if c["other_kind"] == KIND_PLACEMENT else None
        if other is not None and _recency(other) > _recency(p):
            mode = _REVERSE.get(decisions.get((other["id"], c["screen_id"], KIND_PLACEMENT, p["id"])))
        else:
            mode = decisions.get((p["id"], *key))
        if mode is not None:
            out.append({"screen_id": key[0], "other_kind": key[1], "other_id": key[2], "mode": mode})
    return out


def pick_active(playlist_ids: list[int], playable: dict[int, list]) -> list[int]:
    """Оставить из playlist_ids (порядок сохраняется) те, у кого есть что играть.

    playable — {playlist_id: проигрываемые ролики после фильтров модерации,
    площадки и срока}; плейлист без ключа считается пустым. Пустой плейлист
    показа не должен держать экран: если не осталось никого — показа нет и
    экран берёт следующий источник (переопределение, шаблон, заглушки).
    """
    return [pid for pid in playlist_ids if playable.get(pid)]


def playlists_for_screen(placements: list[dict], overrides: list[dict], screen_id: int,
                         d_from: date, d_to: date) -> set[int]:
    """Плейлисты, ролики которых экрану `screen_id` надо иметь на диске заранее.

    Окно — дни [d_from .. d_to] включительно. Берутся показы этого экрана,
    у которых хоть один день в окне и которые не остановлены (у остановленного
    будущих часов нет), и переопределения этого экрана с плейлистом (не
    «выключен») на даты окна. Решения «заменить/по очереди» не учитываются:
    качаем с запасом — плейлист может играть в другие часы того же показа.
    """
    out = {p["playlist_id"] for p in placements
           if screen_id in p["screens"] and p.get("stopped_at") is None
           and dates_overlap(p["date_from"], p["date_to"], d_from, d_to) is not None}
    out |= {o["playlist_id"] for o in overrides
            if o["screen_id"] == screen_id and d_from <= o["on_date"] <= d_to
            and not o.get("is_off") and o.get("playlist_id") is not None}
    return out


def active_until_for(source: str, now: datetime, broadcast_until: datetime | None = None) -> str | None:
    """До какого момента действует активный источник ленты экрана (наивное МСК, ISO).

    Показ выбирается на час — до начала следующего часа; переопределение
    даты (в т. ч. «выключен») — до ближайшей полуночи; общий эфир — до своего
    срока `broadcast_until` («до ЧЧ:00»), а «до отмены» (None) — без срока:
    живёт, пока его не выключат. Шаблон — None: его агент считает сам.
    Агент без связи по истечении не доигрывает устаревший источник.
    """
    if source == "placement":
        end = now.replace(minute=0, second=0, microsecond=0) + _HOUR
    elif source == "override":
        end = datetime.combine(now.date() + timedelta(days=1), time(0))
    elif source == "broadcast" and broadcast_until is not None:
        end = broadcast_until
    else:
        return None
    return end.isoformat()


# --- недельный шаблон --------------------------------------------------------

def slots_for_screen(template: list[dict], screen_id: int, group_id: int | None) -> list[dict]:
    """Слоты недельного шаблона, которые действуют на экране `screen_id` из группы `group_id`.

    template — весь шаблон сети (placements_store.load_template_slots): {"screen_id",
    "group_id", "day_of_week", "hour", "playlist_id"}. Берутся слоты самого экрана
    (prio 3), его группы (2; у экрана без группы их нет) и общесетевые (1) — тот же
    отбор, что у ленты экрана (routers/screens.py). Слот с screen_id — слот экрана,
    какая бы группа в нём ни стояла. Какой из слотов играет на (день, час), решает
    template_playlist. Формат: {"day_of_week", "hour", "playlist_id", "prio"}, порядок —
    как в template.
    """
    out = []
    for s in template:
        if s.get("screen_id") is not None:
            prio = 3 if s["screen_id"] == screen_id else None
        elif s.get("group_id") is not None:
            prio = 2 if group_id is not None and s["group_id"] == group_id else None
        else:
            prio = 1
        if prio is not None:
            out.append({"day_of_week": s["day_of_week"], "hour": s["hour"],
                        "playlist_id": s["playlist_id"], "prio": prio})
    return out


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
               overrides: list[dict], slots: list[dict], names: dict[int, str],
               playable: set[int] | None = None) -> list[dict]:
    """Что играет на экране `screen_id` в каждый час каждого из `days` (МСК).

    Источники по убыванию приоритета (спека §4.7, без «Эфира сети» — он живёт
    «сейчас», а не по датам): переопределение «выключен» → показы по датам →
    переопределение с плейлистом → недельный шаблон → пусто.

    placements — показы (любых экранов; берутся только этого экрана),
    overrides — переопределения (берутся только этого экрана),
    slots — слоты шаблона, уже выбранные для этого экрана (см. template_playlist),
    names — {playlist_id: название}; недостающее берётся из самих показов и
    переопределений,
    playable — id плейлистов, у которых на этом экране есть что играть (как
    pick_active в ленте); None — не фильтровать. Фильтр идёт ПОСЛЕ решений по
    пересечениям (_choose) и до подстановки переопределения/шаблона: плейлисты
    выбранных показов, которых в playable нет, выпадают из часа, остальные
    играют как обычно, а если не осталось ни одного — час проваливается к
    переопределению с плейлистом, затем к шаблону (как лента с пустым показом).

    Клетка: {"date", "hour", "kind", "playlists": [названия], "playlist_ids": [id],
    "placement_id"}. kind: "off" | "placement" | "alternate" (в клетке ≥2
    плейлиста, они чередуются) | "override" | "template" | "empty".
    placement_id — показ-вершина часа (choose_playlists): его плейлист первый в
    клетке; иначе None. Час, который показ уступил («yield»), показывает того,
    кому уступили: другой показ, а если уступил переопределению — само
    переопределение. Порядок клеток: по дням, внутри дня — по часам.
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
            if not is_off:                       # уступленный переопределению час — ids пуст, см. ниже
                applicable = [p for p in mine if applies_at(p, day, hour)]
                ids, placement_id = _choose(applicable, decisions, ov, screen_id)
                if playable is not None:
                    kept = [i for i in ids if i in playable]
                    if not kept:
                        placement_id = None      # показов в этот час нет — дальше переопределение/шаблон
                    elif kept[0] != ids[0]:      # плейлист вершины пуст — первым стал старший показ
                        ordered = sorted(applicable, key=_recency, reverse=True)
                        older = ordered[[p["id"] for p in ordered].index(placement_id) + 1:]
                        placement_id = next((p["id"] for p in older if p["playlist_id"] == kept[0]), None)
                    ids = kept
            if is_off:
                kind = KIND_OFF
            elif ids:
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
    """Пересечения показа `p` с решением «по очереди» или «уступает» — строки карточки.

    Возвращает элементы find_conflicts (экран, другой плейлист, даты и часы
    пересечения) с полем "mode": "alternate" | "yield". Решение берётся у
    того из пары, кто сохранён позже, — как в choose_playlists, где решает
    новейший показ. «По очереди» видно в карточках обоих показов; «уступает
    «X»» — только у уступившего (тот, кому уступили, идёт как обычно).
    Переопределение «новее» быть не может — его решение лежит у самого p.
    Пересечение с тем же плейлистом пропускается: сам с собой он не
    чередуется. `others` может содержать и сам p.
    """
    conflicts, _ = find_conflicts(p, others, overrides)
    by_id = {o["id"]: o for o in others}
    out = []
    for c in conflicts:
        if c["other_playlist_id"] == p["playlist_id"]:
            continue
        owner = p
        if c["other_kind"] == KIND_PLACEMENT:
            other = by_id[c["other_id"]]
            owner, rest = (p, other) if _recency(p) >= _recency(other) else (other, p)
            key = (owner["id"], c["screen_id"], KIND_PLACEMENT, rest["id"])
        else:
            key = (p["id"], c["screen_id"], KIND_OVERRIDE, c["other_id"])
        mode = decisions.get(key)
        if mode == ALTERNATE or (mode == YIELD and owner is p):
            c["mode"] = mode
            out.append(c)
    return out
