"""Регрессия математики биллинга (routers/billing.py).

Защищаем «денежные» вычисления: расчёт суммы по тарифу, разбор дат периода,
преобразование объёмов из play_log. Раньше это проверялось только руками.
"""
from datetime import date

import pytest
from fastapi import HTTPException

from routers.billing import _calc_amount, _parse_date, _period_stats
from conftest import db_row


# ─── _calc_amount: сумма по способу расчёта ─────────────────────────────────

def test_calc_amount_per_play_uses_plays_only():
    # per_play: сумма = показы × цена; минуты игнорируются
    assert _calc_amount("per_play", 2.5, 4, 999.9) == 10.0


def test_calc_amount_per_minute_uses_minutes_only():
    # per_minute: сумма = минуты × цена; показы игнорируются
    assert _calc_amount("per_minute", 3.0, 999, 2.5) == 7.5


def test_calc_amount_rounds_to_two_decimals():
    # 3 × 0.333 = 0.999 → округление до копеек = 1.0
    assert _calc_amount("per_play", 0.333, 3, 0) == 1.0
    # 12.34 мин × 1.005 = 12.4017 → 12.4
    assert _calc_amount("per_minute", 1.005, 0, 12.34) == 12.4


def test_calc_amount_zero_volume_is_zero():
    assert _calc_amount("per_play", 5.0, 0, 0) == 0.0
    assert _calc_amount("per_minute", 5.0, 0, 0.0) == 0.0


def test_calc_amount_unknown_mode_falls_back_to_per_minute():
    # Любой режим, кроме "per_play", трактуется как расчёт по минутам (ветка else).
    assert _calc_amount("whatever", 4.0, 100, 2.0) == 8.0


def test_calc_amount_large_values_no_float_drift():
    # 100000 показов × 1.99 = 199000.0 ровно
    assert _calc_amount("per_play", 1.99, 100000, 0) == 199000.0


# ─── _parse_date: разбор дат периода ────────────────────────────────────────

def test_parse_date_valid():
    assert _parse_date("2026-07-01", "period_start") == date(2026, 7, 1)


@pytest.mark.parametrize("bad", ["2026-13-01", "2026-02-30", "01-07-2026", "abc", "", None])
def test_parse_date_invalid_raises_400(bad):
    with pytest.raises(HTTPException) as exc:
        _parse_date(bad, "period_start")
    assert exc.value.status_code == 400
    assert "period_start" in exc.value.detail  # имя поля попадает в сообщение


# ─── _period_stats: объёмы из play_log (граница включительна) ───────────────

def test_period_stats_casts_and_rounds(fake_db):
    # plays → int, minutes → round(…, 1)
    db = fake_db([db_row(plays=5, minutes=12.34)])
    plays, minutes = _period_stats(db, advertiser_id=1,
                                   period_start=date(2026, 7, 1),
                                   period_end=date(2026, 7, 31))
    assert plays == 5
    assert minutes == 12.3


def test_period_stats_handles_nulls_as_zero(fake_db):
    # Нет показов за период → SUM/COUNT дают NULL → (0, 0.0)
    db = fake_db([db_row(plays=None, minutes=None)])
    assert _period_stats(db, 1, date(2026, 7, 1), date(2026, 7, 2)) == (0, 0.0)


def test_period_stats_passes_advertiser_and_period_params(fake_db):
    # Регресс: границы периода и рекламодатель уходят в запрос как есть.
    db = fake_db([db_row(plays=0, minutes=0)])
    _period_stats(db, advertiser_id=42,
                  period_start=date(2026, 7, 1), period_end=date(2026, 7, 31))
    assert db.last_params["aid"] == 42
    assert db.last_params["d_from"] == date(2026, 7, 1)
    assert db.last_params["d_to"] == date(2026, 7, 31)


# ─── Итог к оплате: сумма + корректировка (скидка/доплата) ──────────────────

def test_total_due_applies_negative_adjustment_as_discount():
    # Логика счёта: total_due = amount + adjustment; скидка — отрицательная.
    amount = _calc_amount("per_play", 10.0, 50, 0)      # 500.0
    discount = -75.0
    assert round(amount + discount, 2) == 425.0


def test_total_due_applies_positive_adjustment_as_surcharge():
    amount = _calc_amount("per_minute", 20.0, 0, 30.0)  # 600.0
    surcharge = 50.0
    assert round(amount + surcharge, 2) == 650.0
