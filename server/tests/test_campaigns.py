"""Регрессия математики кампаний (routers/campaigns.py).

Защищаем план/факт: план = target × число прошедших дней, факт = показы за дни
в пределах [начало … min(сегодня, конец)], процент выполнения, недобор,
переходы статусов (scheduled/active/finished/paused) и границы дат по МСК.
Настоящий Postgres не нужен — строки play_log подставляются моком.
"""
from datetime import date, timedelta

import pytest
from fastapi import HTTPException

from routers.campaigns import _campaign_facts, _parse_date
from conftest import db_row


def campaign(**overrides):
    """Кампания-заготовка с доступом по атрибутам (как строка campaigns)."""
    base = dict(
        advertiser_id=1,
        date_from=date(2026, 7, 1),
        date_to=date(2026, 7, 10),
        target_plays_per_day=100,
        group_id=None,
        is_active=True,
    )
    base.update(overrides)
    from types import SimpleNamespace
    return SimpleNamespace(**base)


def _rows(mapping):
    """{date: plays} → список строк с .on_date/.plays (как отдаёт GROUP BY)."""
    return [db_row(on_date=d, plays=p) for d, p in mapping.items()]


# ─── План / факт / процент / недобор в середине кампании ────────────────────

def test_active_midcampaign_plan_fact_pct_shortfall(fake_db):
    c = campaign()  # 01–10 июля, план 100/день
    today = date(2026, 7, 5)
    plays = {date(2026, 7, 1): 100, date(2026, 7, 2): 80, date(2026, 7, 3): 120,
             date(2026, 7, 4): 100, date(2026, 7, 5): 50}
    facts = _campaign_facts(fake_db(_rows(plays)), c, today)

    assert facts["status"] == "active"
    assert facts["days_total"] == 10
    assert facts["days_due"] == 5                 # 01..05 включительно
    assert facts["plan_to_date"] == 500           # 5 × 100
    assert facts["fact_to_date"] == 450           # 100+80+120+100+50
    assert facts["fulfillment_pct"] == 90.0       # 450/500
    assert facts["shortfall"] == 50               # 500-450


def test_daily_breakdown_has_entry_per_day_with_future_flag(fake_db):
    c = campaign()
    today = date(2026, 7, 5)
    facts = _campaign_facts(fake_db(_rows({date(2026, 7, 2): 30})), c, today)

    daily = facts["daily"]
    assert len(daily) == 10                        # по одной записи на каждый день периода
    day2 = next(x for x in daily if x["date"] == "2026-07-02")
    assert day2 == {"date": "2026-07-02", "plan": 100, "fact": 30, "future": False}
    day8 = next(x for x in daily if x["date"] == "2026-07-08")
    assert day8["fact"] == 0 and day8["future"] is True   # день после сегодня — будущий


# ─── Статусы ────────────────────────────────────────────────────────────────

def test_scheduled_before_start_gives_zero_plan_and_none_pct(fake_db):
    c = campaign()
    today = date(2026, 6, 30)                       # до старта
    facts = _campaign_facts(fake_db([]), c, today)
    assert facts["status"] == "scheduled"
    assert facts["days_due"] == 0
    assert facts["plan_to_date"] == 0
    assert facts["fact_to_date"] == 0
    assert facts["fulfillment_pct"] is None         # деления на ноль нет
    assert facts["shortfall"] == 0


def test_finished_after_end_counts_full_span(fake_db):
    c = campaign()                                  # 01–10, план 100/день = 1000
    today = date(2026, 7, 20)                        # после конца
    full = {date(2026, 7, d): 100 for d in range(1, 11)}
    facts = _campaign_facts(fake_db(_rows(full)), c, today)
    assert facts["status"] == "finished"
    assert facts["days_due"] == 10
    assert facts["plan_to_date"] == 1000
    assert facts["fact_to_date"] == 1000
    assert facts["fulfillment_pct"] == 100.0
    assert facts["shortfall"] == 0


def test_paused_overrides_status_but_keeps_math(fake_db):
    c = campaign(is_active=False)
    today = date(2026, 7, 5)
    facts = _campaign_facts(fake_db(_rows({date(2026, 7, 1): 100})), c, today)
    assert facts["status"] == "paused"              # приоритет у флага is_active
    assert facts["plan_to_date"] == 500
    assert facts["fact_to_date"] == 100


# ─── Границы и защита от отрицательных значений ─────────────────────────────

def test_fact_excludes_days_after_period_end(fake_db):
    # Показ с датой ПОСЛЕ конца кампании не должен попасть в факт.
    c = campaign(date_to=date(2026, 7, 5))
    today = date(2026, 7, 10)                        # кампания завершена
    plays = {date(2026, 7, 4): 100, date(2026, 7, 5): 100, date(2026, 7, 6): 999}
    facts = _campaign_facts(fake_db(_rows(plays)), c, today)
    assert facts["days_due"] == 5                    # 01..05
    assert facts["fact_to_date"] == 200              # 07-06 исключён


def test_overachievement_pct_over_100_and_no_negative_shortfall(fake_db):
    c = campaign()
    today = date(2026, 7, 5)                          # план 500
    facts = _campaign_facts(fake_db(_rows({date(2026, 7, 1): 600})), c, today)
    assert facts["fact_to_date"] == 600
    assert facts["fulfillment_pct"] == 120.0
    assert facts["shortfall"] == 0                   # недобор не уходит в минус


def test_single_day_campaign(fake_db):
    c = campaign(date_from=date(2026, 7, 1), date_to=date(2026, 7, 1), target_plays_per_day=50)
    today = date(2026, 7, 1)
    facts = _campaign_facts(fake_db(_rows({date(2026, 7, 1): 40})), c, today)
    assert facts["days_total"] == 1
    assert facts["days_due"] == 1
    assert facts["plan_to_date"] == 50
    assert facts["fact_to_date"] == 40
    assert facts["fulfillment_pct"] == 80.0
    assert facts["shortfall"] == 10


def test_group_scoped_campaign_passes_group_id_param(fake_db):
    # Кампания по группе: group_id должен уйти в параметры запроса.
    c = campaign(group_id=7)
    db = fake_db(_rows({date(2026, 7, 1): 10}))
    _campaign_facts(db, c, date(2026, 7, 5))
    assert db.last_params.get("gid") == 7


# ─── _parse_date (та же валидация, что в биллинге) ──────────────────────────

def test_parse_date_valid_and_invalid():
    assert _parse_date("2026-07-01", "date_from") == date(2026, 7, 1)
    with pytest.raises(HTTPException) as exc:
        _parse_date("2026-99-99", "date_to")
    assert exc.value.status_code == 400
    assert "date_to" in exc.value.detail
