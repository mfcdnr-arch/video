"""Регрессия карточки рекламодателя (routers/advertiser_office.py).

Тонкое место — границы периода. В БД метки времени в UTC, а вся система
живёт по Москве: ошибка на три часа тихо перенесёт показы соседних суток в
чужой период, а значит и в чужой счёт и эфирную справку.

Проверяем ровно это плюс защиту от бессмысленных диапазонов.
"""
from datetime import date, datetime

import pytest
from fastapi import HTTPException

from routers.advertiser_office import _period, _utc_bounds, BUCKET_MIN


# ─── Границы московских суток в UTC ─────────────────────────────────────────

def test_bounds_shift_msk_day_to_utc():
    # Московские сутки 10.07 = [09.07 21:00 UTC; 10.07 21:00 UTC)
    start, end = _utc_bounds(date(2026, 7, 10), date(2026, 7, 10))
    assert start == datetime(2026, 7, 9, 21, 0)
    assert end == datetime(2026, 7, 10, 21, 0)


def test_bounds_cover_whole_range_inclusive():
    # Последний день периода входит целиком: 31 июля закрывается 21:00 UTC
    start, end = _utc_bounds(date(2026, 7, 1), date(2026, 7, 31))
    assert start == datetime(2026, 6, 30, 21, 0)
    assert end == datetime(2026, 7, 31, 21, 0)
    assert (end - start).days == 31


def test_bounds_length_is_exact_days():
    # Ровно сутки на день периода — без «хвостов» в 23 или 25 часов
    for days in (1, 7, 30):
        start, end = _utc_bounds(date(2026, 3, 1), date(2026, 3, days))
        assert (end - start).total_seconds() == days * 24 * 3600


# ─── Разбор и валидация периода ─────────────────────────────────────────────

def test_period_parses_iso_dates():
    assert _period("2026-07-01", "2026-07-31") == (date(2026, 7, 1), date(2026, 7, 31))


def test_period_rejects_reversed_range():
    with pytest.raises(HTTPException) as e:
        _period("2026-07-31", "2026-07-01")
    assert e.value.status_code == 400


def test_period_rejects_too_long_range():
    # Больше года запрещаем: и по здравому смыслу, и потому что сырьё
    # play_log старше года сворачивается в дневные агрегаты
    with pytest.raises(HTTPException):
        _period("2024-01-01", "2026-07-01")


def test_period_allows_single_day():
    assert _period("2026-07-10", "2026-07-10") == (date(2026, 7, 10), date(2026, 7, 10))


# ─── Шаг корзины доступности ────────────────────────────────────────────────

def test_bucket_is_larger_than_heartbeat_interval():
    # Агент шлёт heartbeat раз в 30 секунд. Корзина должна быть заметно
    # больше, иначе единичный потерянный пакет превратится в «простой».
    assert BUCKET_MIN * 60 >= 120
