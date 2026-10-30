"""Регрессия карточки рекламодателя (routers/advertiser_office.py).

Тонкое место — границы периода. В БД метки времени в UTC, а вся система
живёт по Москве: ошибка на три часа тихо перенесёт показы соседних суток в
чужой период, а значит и в чужой счёт и эфирную справку.

Проверяем ровно это плюс защиту от бессмысленных диапазонов.
"""
from datetime import date, datetime, timedelta
from types import SimpleNamespace

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


# --- уведомления: «истекает срок показа» по Москве --------------------------------------

class _Res:
    def __init__(self, rows):
        self._rows = list(rows)

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _AlertsDB:
    """Рекламодатель, его ролики и счета — по подстроке SQL; остальное пусто."""

    def __init__(self, creatives, invoices=()):
        self.routes = [("FROM advertisers WHERE id", [SimpleNamespace(
                            id=1, name="Ромашка", billing_mode="per_minute", price_per_minute=0,
                            price_per_play=0, legal_name=None, inn=None, kpp=None, legal_address=None,
                            contact_person=None, phone=None, email=None)]),
                       ("FROM media WHERE advertiser_id", list(creatives)),
                       ("FROM invoices", list(invoices))]

    def execute(self, query, params=None):
        sql = " ".join(str(query).split())
        for needle, rows in self.routes:
            if needle in sql:
                return _Res(rows)
        return _Res([])


def _clip(mid, valid_until):
    return SimpleNamespace(id=mid, title=f"Ролик {mid}", review_status="approved", reject_reason=None,
                           valid_until=valid_until, is_broken=False)


def test_alerts_expiring_counts_from_moscow_now(monkeypatch):
    from routers import advertiser_office
    n = datetime(2026, 10, 30, 15, 0)                      # «сейчас» по Москве
    monkeypatch.setattr(advertiser_office, "now_msk", lambda: n)
    db = _AlertsDB([_clip(1, n - timedelta(hours=1)),      # срок вышел час назад (по UTC было бы «впереди»)
                    _clip(2, n + timedelta(hours=1)),      # истекает через час
                    _clip(3, n + timedelta(days=4))])      # ещё нескоро
    out = advertiser_office.advertiser_alerts(aid=1, admin={}, db=db)
    assert [i["media_id"] for i in out["items"] if i["kind"] == "expiring"] == [2]


def test_alerts_invoice_overdue_by_moscow_date(monkeypatch):
    from routers import advertiser_office
    n = datetime(2026, 10, 30, 1, 0)                       # 01:00 МСК — по UTC ещё 29.10
    monkeypatch.setattr(advertiser_office, "now_msk", lambda: n)
    inv = SimpleNamespace(id=7, period_start=date(2026, 10, 1), period_end=date(2026, 10, 28), amount=100,
                          adjustment_amount=0, status="issued", due_date=date(2026, 10, 29))
    out = advertiser_office.advertiser_alerts(aid=1, admin={}, db=_AlertsDB([], [inv]))
    (item,) = [i for i in out["items"] if i["kind"] == "invoice"]
    assert item["level"] == "danger" and "просрочен" in item["text"]
