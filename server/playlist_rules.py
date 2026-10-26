"""Правила плейлиста: повторы, мягкие предупреждения, «общий час».

Чистая логика без БД: на вход — простые списки и словари, на выход — списки
и строки. SQL-часть живёт в admin_panel (ad_sequence / filler_list), здесь
только то, что можно проверить обычным pytest.

Предупреждения «мягкие»: показываются в окне сборки плейлиста, но ничего не
запрещают — решает менеджер.
"""
from admin_panel import distribute_fillers

MAX_REPEATS_IN_ROW = 3   # больше стольких повторов подряд — предупреждаем
MAX_SHARE = 0.5          # ролик дольше половины плейлиста — предупреждаем


def _repeats(item: dict) -> int:
    return max(1, item.get("repeat_count") or 1)


def expand_repeats(items: list[dict]) -> list[dict]:
    """Каждый элемент — max(1, repeat_count or 1) раз подряд."""
    out = []
    for item in items:
        out.extend([item] * _repeats(item))
    return out


def interleave(sequences: list[list]) -> list:
    """По одному элементу из каждой последовательности по кругу, пока все не кончатся."""
    out = []
    longest = max((len(s) for s in sequences), default=0)
    for i in range(longest):
        for seq in sequences:
            if i < len(seq):
                out.append(seq[i])
    return out


def repeat_warnings(items: list[dict]) -> list[str]:
    """
    Мягкие предупреждения о плейлисте. items — в порядке плейлиста:
    {"media_id", "title", "duration", "repeat_count", "md5"}.

    Порядок в ответе: сначала все «подряд» (по порядку роликов), затем «доля»,
    затем «дубли».
    """
    warnings: list[str] = []

    # 1. Больше MAX_REPEATS_IN_ROW повторов одного ролика подряд.
    for item in items:
        reps = _repeats(item)
        if reps > MAX_REPEATS_IN_ROW:
            warnings.append(
                f"«{item['title']}» ×{reps} подряд — больше {MAX_REPEATS_IN_ROW} повторов")

    # 2. Ролик занимает больше половины плейлиста (с учётом повторов).
    #    Для одного ролика доля всегда 100 % — это не повод для предупреждения.
    if len(items) >= 2:
        secs = [float(item.get("duration") or 0) * _repeats(item) for item in items]
        total = sum(secs)
        if total > 0:
            for item, own in zip(items, secs):
                if own > MAX_SHARE * total:
                    warnings.append(
                        f"«{item['title']}» занимает {int(round(own))} из {int(round(total))} с "
                        f"({round(own * 100 / total)} %) — больше половины плейлиста")

    # 3. Один и тот же файл, добавленный как разные ролики (совпал md5, разный
    #    media_id). Парой считаем первое вхождение файла и каждое следующее.
    first_by_md5: dict[str, dict] = {}
    reported: set = set()
    for item in items:
        md5 = item.get("md5")
        if not md5:
            continue
        first = first_by_md5.setdefault(md5, item)
        if first["media_id"] == item["media_id"] or item["media_id"] in reported:
            continue
        reported.add(item["media_id"])
        warnings.append(f"«{first['title']}» и «{item['title']}» — это один и тот же файл")

    return warnings


def build_shared_hour(ad_seqs: list[list[tuple[str, float]]], fillers: list[tuple[str, float]],
                      target_seconds: float | None) -> list[str]:
    """
    «Общий час»: ролики нескольких плейлистов идут по очереди (interleave).
    С target_seconds в промежутки раскладываются заглушки (distribute_fillers),
    без него — просто имена файлов в порядке чередования.
    """
    merged = interleave(ad_seqs)
    if target_seconds is None:
        return [name for name, _ in merged]
    return distribute_fillers(merged, fillers, target_seconds)[0]


def overflow_minutes(ad_seconds_total: float, limit_seconds: float = 3600) -> int | None:
    """На сколько минут реклама не влезает в час: round(total/60), если больше лимита, иначе None."""
    if ad_seconds_total > limit_seconds:
        return round(ad_seconds_total / 60)
    return None
