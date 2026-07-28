"""
Карточка рекламодателя: всё про одного рекламодателя в одном месте.

Зачем отдельный раздел: те же данные раньше приходилось собирать из четырёх
мест (медиатека, отчёты, кампании, биллинг), а рекламодателю нужен один
понятный ответ на вопросы «сколько раз меня показали», «где и когда»,
«всё ли отработало» и «сколько я должен».

Принцип: показываем ТОЛЬКО доказуемый факт из play_log и телеметрии экранов.
Контакты/охват (OTS) не считаем — их неоткуда взять без камер и внешних
данных о трафике, а недоказуемая цифра в споре с рекламодателем хуже, чем
её отсутствие (решение заказчика от 28.07.2026).

Все периоды — московские сутки: в БД метки в UTC, поэтому границы
сдвигаются на −3 часа, как в биллинге (routers/billing.py).
"""
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.orm import Session

from deps import get_db, get_current_admin, advertiser_scope
from routers.billing import _calc_amount, _parse_date

router = APIRouter()


@router.get("/advertisers/me")
def my_advertiser(admin: dict = Depends(get_current_admin), db: Session = Depends(get_db)):
    """
    Кабинет текущего пользователя. Панель вызывает это сразу после входа:
    рекламодателя нужно открыть в его карточке, не показывая список чужих.
    """
    aid = admin.get("advertiser_id")
    if not aid:
        raise HTTPException(404, "Учётная запись не привязана к рекламодателю")
    row = db.execute(text("SELECT id, name FROM advertisers WHERE id = :id"), {"id": aid}).fetchone()
    if not row:
        raise HTTPException(404, "Кабинет не найден")
    return {"advertiser_id": row.id, "name": row.name}

# Шаг «корзины» доступности экрана, минут. Heartbeat идёт раз в 30 секунд,
# поэтому корзина в 5 минут переживает единичные пропуски пакета и при этом
# даёт достаточную точность (0.08 часа).
BUCKET_MIN = 5


def _period(date_from: str, date_to: str):
    d_from = _parse_date(date_from, "date_from")
    d_to = _parse_date(date_to, "date_to")
    if d_to < d_from:
        raise HTTPException(400, "date_to раньше date_from")
    if (d_to - d_from).days > 366:
        raise HTTPException(400, "период больше года — сузьте диапазон")
    return d_from, d_to


# Границы периода в UTC: московские сутки [d_from 00:00; d_to 23:59:59] —
# это [d_from-3ч; d_to+1сут-3ч) по UTC.
def _utc_bounds(d_from: date, d_to: date):
    start = datetime.combine(d_from, datetime.min.time()) - timedelta(hours=3)
    end = datetime.combine(d_to + timedelta(days=1), datetime.min.time()) - timedelta(hours=3)
    return start, end


def _get_advertiser(db: Session, aid: int):
    row = db.execute(text("""
        SELECT id, name, COALESCE(billing_mode, 'per_minute') AS billing_mode,
               COALESCE(price_per_minute, 0) AS price_per_minute,
               COALESCE(price_per_play, 0) AS price_per_play,
               legal_name, inn, kpp, legal_address, contact_person, phone, email
        FROM advertisers WHERE id = :id
    """), {"id": aid}).fetchone()
    if not row:
        raise HTTPException(404, "Рекламодатель не найден")
    return row


@router.get("/advertisers/{aid}/overview")
def advertiser_overview(date_from: str = Query(...), date_to: str = Query(...),
                        aid: int = Depends(advertiser_scope),
                        admin: dict = Depends(get_current_admin), db: Session = Depends(get_db)):
    """
    Сводка за период: объёмы, доля в эфире, распределение по дням/часам,
    топ-экраны и деньги по действующему тарифу.
    """
    adv = _get_advertiser(db, aid)
    d_from, d_to = _period(date_from, date_to)
    start, end = _utc_bounds(d_from, d_to)
    p = {"aid": aid, "start": start, "end": end}

    totals = db.execute(text("""
        SELECT COUNT(*) AS plays,
               COALESCE(SUM(EXTRACT(EPOCH FROM (pl.ended_at - pl.started_at))), 0) / 60.0 AS minutes,
               COUNT(DISTINCT pl.screen_id) AS screens,
               COUNT(DISTINCT pl.media_id) AS creatives
        FROM play_log pl JOIN media m ON m.id = pl.media_id
        WHERE m.advertiser_id = :aid AND pl.started_at >= :start AND pl.started_at < :end
    """), p).fetchone()

    # Доля в эфире. Считаем на тех экранах, где рекламодатель реально был:
    # доля от ВСЕГО эфира (включая заглушки) и доля среди коммерческих
    # роликов — вторая честнее для сравнения с другими рекламодателями.
    sov = db.execute(text("""
        WITH scope AS (
            SELECT DISTINCT pl.screen_id
            FROM play_log pl JOIN media m ON m.id = pl.media_id
            WHERE m.advertiser_id = :aid AND pl.started_at >= :start AND pl.started_at < :end
        )
        SELECT COUNT(*) AS all_plays,
               COUNT(*) FILTER (WHERE m.advertiser_id IS NOT NULL
                                  AND COALESCE(m.is_filler, FALSE) = FALSE) AS commercial_plays,
               COUNT(*) FILTER (WHERE m.advertiser_id = :aid) AS own_plays
        FROM play_log pl JOIN media m ON m.id = pl.media_id
        WHERE pl.screen_id IN (SELECT screen_id FROM scope)
          AND pl.started_at >= :start AND pl.started_at < :end
    """), p).fetchone()

    by_day = db.execute(text("""
        SELECT (pl.started_at + INTERVAL '3 hours')::date AS on_date,
               COUNT(*) AS plays,
               COALESCE(SUM(EXTRACT(EPOCH FROM (pl.ended_at - pl.started_at))), 0) / 60.0 AS minutes
        FROM play_log pl JOIN media m ON m.id = pl.media_id
        WHERE m.advertiser_id = :aid AND pl.started_at >= :start AND pl.started_at < :end
        GROUP BY 1 ORDER BY 1
    """), p).fetchall()

    # Тепловая карта «день недели × час» (МСК): видно, попадает ли реклама
    # в тот прайм, за который заплачено.
    heat = db.execute(text("""
        SELECT EXTRACT(ISODOW FROM pl.started_at + INTERVAL '3 hours')::int AS dow,
               EXTRACT(HOUR  FROM pl.started_at + INTERVAL '3 hours')::int AS hour,
               COUNT(*) AS plays
        FROM play_log pl JOIN media m ON m.id = pl.media_id
        WHERE m.advertiser_id = :aid AND pl.started_at >= :start AND pl.started_at < :end
        GROUP BY 1, 2
    """), p).fetchall()

    by_screen = db.execute(text("""
        SELECT s.id, s.name, s.city, s.location, s.venue_type,
               COUNT(*) AS plays,
               COALESCE(SUM(EXTRACT(EPOCH FROM (pl.ended_at - pl.started_at))), 0) / 60.0 AS minutes
        FROM play_log pl
        JOIN media m ON m.id = pl.media_id
        JOIN screens s ON s.id = pl.screen_id
        WHERE m.advertiser_id = :aid AND pl.started_at >= :start AND pl.started_at < :end
        GROUP BY s.id, s.name, s.city, s.location, s.venue_type
        ORDER BY plays DESC
    """), p).fetchall()

    plays = int(totals.plays or 0)
    minutes = round(float(totals.minutes or 0), 1)
    price = float(adv.price_per_play if adv.billing_mode == "per_play" else adv.price_per_minute)
    amount = _calc_amount(adv.billing_mode, price, plays, minutes)
    days = (d_to - d_from).days + 1

    all_plays = int(sov.all_plays or 0)
    comm_plays = int(sov.commercial_plays or 0)
    own_plays = int(sov.own_plays or 0)

    # Предыдущий период такой же длины, впритык перед текущим: рекламодателю
    # важно «стало больше или меньше», а не абсолютная цифра в вакууме.
    prev_to = d_from - timedelta(days=1)
    prev_from = prev_to - timedelta(days=days - 1)
    ps, pe = _utc_bounds(prev_from, prev_to)
    prev = db.execute(text("""
        SELECT COUNT(*) AS plays,
               COALESCE(SUM(EXTRACT(EPOCH FROM (pl.ended_at - pl.started_at))), 0) / 60.0 AS minutes
        FROM play_log pl JOIN media m ON m.id = pl.media_id
        WHERE m.advertiser_id = :aid AND pl.started_at >= :start AND pl.started_at < :end
    """), {"aid": aid, "start": ps, "end": pe}).fetchone()
    prev_plays = int(prev.plays or 0)
    prev_minutes = round(float(prev.minutes or 0), 1)

    def _delta(now_v, was_v):
        """Прирост в процентах. None, если сравнивать не с чем (было 0)."""
        if not was_v:
            return None
        return round(100.0 * (now_v - was_v) / was_v, 1)

    invoices = db.execute(text("""
        SELECT id, period_start, period_end, amount, adjustment_amount, status, due_date, paid_at
        FROM invoices WHERE advertiser_id = :aid
        ORDER BY period_start DESC LIMIT 12
    """), {"aid": aid}).fetchall()

    return {
        "advertiser": {"id": adv.id, "name": adv.name, "legal_name": adv.legal_name,
                       "inn": adv.inn, "kpp": adv.kpp, "legal_address": adv.legal_address,
                       "contact_person": adv.contact_person, "phone": adv.phone,
                       "email": adv.email},
        "period": {"date_from": d_from.isoformat(), "date_to": d_to.isoformat(), "days": days},
        "totals": {
            "plays": plays,
            "minutes": minutes,
            "screens": int(totals.screens or 0),
            "creatives": int(totals.creatives or 0),
            "plays_per_day": round(plays / days, 1) if days else 0,
        },
        "share": {
            "own_plays": own_plays,
            "all_plays": all_plays,
            "commercial_plays": comm_plays,
            # Доля эфира: от всех выходов и отдельно среди рекламных
            "pct_all": round(100.0 * own_plays / all_plays, 1) if all_plays else None,
            "pct_commercial": round(100.0 * own_plays / comm_plays, 1) if comm_plays else None,
        },
        "money": {"billing_mode": adv.billing_mode, "price": price, "amount": amount},
        "previous": {
            "date_from": prev_from.isoformat(), "date_to": prev_to.isoformat(),
            "plays": prev_plays, "minutes": prev_minutes,
            "plays_delta_pct": _delta(plays, prev_plays),
            "minutes_delta_pct": _delta(minutes, prev_minutes),
        },
        "by_day": [{"date": r.on_date.isoformat(), "plays": int(r.plays),
                    "minutes": round(float(r.minutes), 1)} for r in by_day],
        "heatmap": [{"dow": r.dow, "hour": r.hour, "plays": int(r.plays)} for r in heat],
        "by_screen": [{"screen_id": r.id, "name": r.name, "city": r.city,
                       "location": r.location, "venue_type": r.venue_type,
                       "plays": int(r.plays), "minutes": round(float(r.minutes), 1)}
                      for r in by_screen],
        "invoices": [dict(r._mapping) for r in invoices],
    }


@router.get("/advertisers/{aid}/airtime")
def advertiser_airtime(date_from: str = Query(...), date_to: str = Query(...),
                       screen_id: int = Query(None), media_id: int = Query(None),
                       limit: int = Query(500, le=5000), offset: int = Query(0),
                       aid: int = Depends(advertiser_scope),
                       admin: dict = Depends(get_current_admin), db: Session = Depends(get_db)):
    """
    Сырой журнал выходов — то, что в отрасли называют proof of play, а в
    документах будет «эфирной справкой»: каждая строка = один показ.
    """
    _get_advertiser(db, aid)
    d_from, d_to = _period(date_from, date_to)
    start, end = _utc_bounds(d_from, d_to)
    where = ["m.advertiser_id = :aid", "pl.started_at >= :start", "pl.started_at < :end"]
    p = {"aid": aid, "start": start, "end": end, "limit": limit, "offset": offset}
    if screen_id:
        where.append("pl.screen_id = :sid"); p["sid"] = screen_id
    if media_id:
        where.append("pl.media_id = :mid"); p["mid"] = media_id
    cond = " AND ".join(where)

    total = db.execute(text(f"""
        SELECT COUNT(*) FROM play_log pl JOIN media m ON m.id = pl.media_id WHERE {cond}
    """), p).scalar()

    rows = db.execute(text(f"""
        SELECT pl.id,
               pl.started_at + INTERVAL '3 hours' AS started_msk,
               EXTRACT(ISODOW FROM pl.started_at + INTERVAL '3 hours')::int AS dow,
               ROUND(EXTRACT(EPOCH FROM (pl.ended_at - pl.started_at))::numeric, 1) AS seconds,
               s.name AS screen, s.city, s.location,
               COALESCE(m.title, m.filename) AS creative, m.id AS media_id
        FROM play_log pl
        JOIN media m ON m.id = pl.media_id
        JOIN screens s ON s.id = pl.screen_id
        WHERE {cond}
        ORDER BY pl.started_at DESC
        LIMIT :limit OFFSET :offset
    """), p).fetchall()

    return {"total": int(total or 0), "limit": limit, "offset": offset,
            "items": [dict(r._mapping) for r in rows]}


@router.get("/advertisers/{aid}/delivery")
def advertiser_delivery(date_from: str = Query(...), date_to: str = Query(...),
                        aid: int = Depends(advertiser_scope),
                        admin: dict = Depends(get_current_admin), db: Session = Depends(get_db)):
    """
    Качество доставки за период: сколько времени экраны, на которых крутился
    рекламодатель, были недоступны или показывали «в стену» (монитор
    отключён), и как это соотносится с планом кампаний.

    Доступность считается по истории heartbeat (minipc_status): период режем
    на корзины по BUCKET_MIN минут; корзина без единого heartbeat — простой,
    корзина, где агент сообщил display_connected = false, — «монитор
    отключён». Это факт из телеметрии, а не оценка.
    """
    _get_advertiser(db, aid)
    d_from, d_to = _period(date_from, date_to)
    start, end = _utc_bounds(d_from, d_to)
    # Если период включает сегодняшний день, его «хвост» ещё не наступил:
    # без этого отчёт за текущие сутки насчитал бы простой на часы вперёд.
    now = datetime.utcnow()
    end = min(end, now)
    if end <= start:
        raise HTTPException(400, "период ещё не начался")
    p = {"aid": aid, "start": start, "end": end, "bucket": f"{BUCKET_MIN} minutes"}

    rows = db.execute(text("""
        WITH scope AS (
            SELECT pl.screen_id, COUNT(*) AS plays
            FROM play_log pl JOIN media m ON m.id = pl.media_id
            WHERE m.advertiser_id = :aid AND pl.started_at >= :start AND pl.started_at < :end
            GROUP BY pl.screen_id
        ),
        -- Экран мог быть заведён посреди периода: до этого момента простоя
        -- не было — его просто не существовало. Считаем от более поздней из
        -- двух дат, иначе документ обвинит нас в несуществующем простое.
        window_of AS (
            SELECT s.id AS screen_id,
                   GREATEST(:start, COALESCE(s.created_at, :start)) AS w_start
            FROM screens s WHERE s.id IN (SELECT screen_id FROM scope)
        ),
        buckets AS (
            SELECT st.screen_id,
                   to_timestamp(floor(extract(epoch FROM st.timestamp)
                       / extract(epoch FROM CAST(:bucket AS interval)))
                       * extract(epoch FROM CAST(:bucket AS interval))) AS b,
                   bool_or(COALESCE(st.display_connected, TRUE)) AS display_ok
            FROM minipc_status st
            WHERE st.screen_id IN (SELECT screen_id FROM scope)
              AND st.timestamp >= :start AND st.timestamp < :end
            GROUP BY 1, 2
        )
        SELECT s.id, s.name, s.city, s.location, sc.plays, w.w_start,
               COUNT(b.b) AS alive_buckets,
               COUNT(b.b) FILTER (WHERE b.display_ok IS FALSE) AS display_off_buckets
        FROM scope sc
        JOIN screens s ON s.id = sc.screen_id
        JOIN window_of w ON w.screen_id = sc.screen_id
        LEFT JOIN buckets b ON b.screen_id = sc.screen_id AND b.b >= w.w_start
        GROUP BY s.id, s.name, s.city, s.location, sc.plays, w.w_start
        ORDER BY s.name
    """), p).fetchall()

    screens, off_h, dark_h, no_tele = [], 0.0, 0.0, 0
    for r in rows:
        alive = int(r.alive_buckets or 0)
        dark = int(r.display_off_buckets or 0)
        span = max(1, int((end - r.w_start).total_seconds() // (BUCKET_MIN * 60)))
        item = {"screen_id": r.id, "name": r.name, "city": r.city, "location": r.location,
                "plays": int(r.plays or 0)}
        if alive == 0:
            # Телеметрии за период нет вовсе (экран старее истории heartbeat
            # или записи вычищены). Показы при этом были, значит экран РАБОТАЛ
            # — объявлять 100% простоя нельзя, это была бы неправда в отчёте.
            item.update({"offline_hours": None, "display_off_hours": None,
                         "availability_pct": None, "no_telemetry": True})
            no_tele += 1
        else:
            oh = round(max(0, span - alive) * BUCKET_MIN / 60.0, 1)
            dh = round(dark * BUCKET_MIN / 60.0, 1)
            off_h += oh
            dark_h += dh
            item.update({"offline_hours": oh, "display_off_hours": dh,
                         "availability_pct": round(100.0 * min(alive, span) / span, 1),
                         "no_telemetry": False})
        screens.append(item)

    # План/факт по кампаниям, пересекающимся с периодом: именно отсюда
    # берётся недобор, который на этапе 3 превратится в компенсацию.
    from routers.campaigns import _campaign_facts
    today = (datetime.utcnow() + timedelta(hours=3)).date()
    camp_rows = db.execute(text("""
        SELECT * FROM campaigns
        WHERE advertiser_id = :aid AND date_from <= :dt AND date_to >= :df
        ORDER BY date_from DESC
    """), {"aid": aid, "df": d_from, "dt": d_to}).fetchall()
    campaigns = []
    for c in camp_rows:
        facts = _campaign_facts(db, c, today)
        campaigns.append({"id": c.id, "name": c.name,
                          "date_from": c.date_from.isoformat(),
                          "date_to": c.date_to.isoformat(),
                          "target_plays_per_day": c.target_plays_per_day,
                          "status": facts["status"],
                          "plan_to_date": facts["plan_to_date"],
                          "fact_to_date": facts["fact_to_date"],
                          "fulfillment_pct": facts["fulfillment_pct"],
                          "shortfall": facts["shortfall"]})

    return {
        "period": {"date_from": d_from.isoformat(), "date_to": d_to.isoformat()},
        "totals": {"offline_hours": round(off_h, 1), "display_off_hours": round(dark_h, 1),
                   "screens": len(screens), "screens_without_telemetry": no_tele},
        "screens": screens,
        "campaigns": campaigns,
    }


@router.get("/advertisers/{aid}/now-playing")
def advertiser_now_playing(aid: int = Depends(advertiser_scope),
                           admin: dict = Depends(get_current_admin),
                           db: Session = Depends(get_db)):
    """
    Что из роликов рекламодателя идёт прямо сейчас и на каких экранах.

    Самый частый вопрос клиента — «меня вообще показывают?». Берём
    playing_file из heartbeat: экран считается живым, если выходил на связь
    последние 2 минуты (heartbeat раз в 30 секунд).
    """
    _get_advertiser(db, aid)
    rows = db.execute(text("""
        SELECT s.id, s.name, s.city, s.location, s.last_seen, s.display_connected,
               COALESCE(m.title, m.filename) AS creative
        FROM screens s
        JOIN media m ON m.filename = s.playing_file
        WHERE m.advertiser_id = :aid
          AND s.last_seen > NOW() - INTERVAL '2 minutes'
        ORDER BY s.name
    """), {"aid": aid}).fetchall()
    return {"checked_at": datetime.utcnow().isoformat(),
            "screens": [dict(r._mapping) for r in rows]}


@router.get("/advertisers/{aid}/alerts")
def advertiser_alerts(aid: int = Depends(advertiser_scope),
                      admin: dict = Depends(get_current_admin),
                      db: Session = Depends(get_db)):
    """
    Что требует внимания рекламодателя: ролик отклонён модерацией (и почему),
    ждёт проверки, скоро истекает срок показа, ролик не воспроизводится,
    выставлен или просрочен счёт, готовы документы за период.

    Считается на лету по текущим данным — отдельного хранилища уведомлений
    нет намеренно: «прочитанность» тут не нужна, важно текущее состояние.
    """
    _get_advertiser(db, aid)
    out = []

    creatives = db.execute(text("""
        SELECT id, COALESCE(title, filename) AS title, review_status, reject_reason,
               valid_until, is_broken
        FROM media WHERE advertiser_id = :aid
    """), {"aid": aid}).fetchall()
    now = datetime.utcnow()
    for c in creatives:
        if c.review_status == "rejected":
            out.append({"level": "danger", "kind": "rejected",
                        "text": f"Ролик «{c.title}» отклонён модерацией"
                                + (f": {c.reject_reason}" if c.reject_reason else ""),
                        "media_id": c.id})
        elif c.review_status == "pending":
            out.append({"level": "info", "kind": "pending",
                        "text": f"Ролик «{c.title}» ждёт проверки — в эфир пойдёт после одобрения",
                        "media_id": c.id})
        if c.is_broken:
            out.append({"level": "danger", "kind": "broken",
                        "text": f"Ролик «{c.title}» не воспроизводится на экранах",
                        "media_id": c.id})
        if c.valid_until and now <= c.valid_until <= now + timedelta(days=3):
            out.append({"level": "warn", "kind": "expiring",
                        "text": f"У ролика «{c.title}» {c.valid_until.strftime('%d.%m.%Y')} "
                                f"истекает срок показа — после этой даты он пропадёт из эфира",
                        "media_id": c.id})

    invoices = db.execute(text("""
        SELECT id, period_start, period_end, amount, adjustment_amount, status, due_date
        FROM invoices WHERE advertiser_id = :aid AND status = 'issued'
        ORDER BY period_start DESC
    """), {"aid": aid}).fetchall()
    today = (now + timedelta(hours=3)).date()
    for i in invoices:
        total = float(i.amount) + float(i.adjustment_amount or 0)
        overdue = i.due_date and i.due_date < today
        out.append({
            "level": "danger" if overdue else "info",
            "kind": "invoice",
            "text": (f"Счёт за {i.period_start.strftime('%d.%m.%Y')}—{i.period_end.strftime('%d.%m.%Y')} "
                     f"на {total:.2f} ₽ "
                     + (f"просрочен (оплатить до {i.due_date.strftime('%d.%m.%Y')})" if overdue
                        else (f"— оплатить до {i.due_date.strftime('%d.%m.%Y')}" if i.due_date else "выставлен"))),
            "invoice_id": i.id})

    # Документы группируем по периоду: пакет — это 4 файла (справка PDF+Excel,
    # акт, отчёт), и четыре одинаковых строки в списке были бы просто шумом.
    docs = db.execute(text("""
        SELECT period_start, period_end, COUNT(*) AS files, MAX(created_at) AS created_at
        FROM advertiser_documents
        WHERE advertiser_id = :aid AND created_at > NOW() - INTERVAL '30 days'
        GROUP BY period_start, period_end
        ORDER BY MAX(created_at) DESC LIMIT 5
    """), {"aid": aid}).fetchall()
    for d in docs:
        out.append({"level": "info", "kind": "document",
                    "text": f"Готовы документы за "
                            f"{d.period_start.strftime('%d.%m.%Y')}—{d.period_end.strftime('%d.%m.%Y')} "
                            f"({d.files} файл{'а' if 2 <= d.files <= 4 else 'ов'}) — "
                            f"вкладка «Документы»"})

    order = {"danger": 0, "warn": 1, "info": 2}
    out.sort(key=lambda x: order.get(x["level"], 3))
    return {"total": len(out),
            "danger": sum(1 for x in out if x["level"] == "danger"),
            "items": out}


@router.get("/advertisers/{aid}/airtime.xlsx")
def advertiser_airtime_xlsx(date_from: str = Query(...), date_to: str = Query(...),
                            aid: int = Depends(advertiser_scope),
                            admin: dict = Depends(get_current_admin),
                            db: Session = Depends(get_db)):
    """
    Журнал выходов в Excel — рекламодатель выгружает сам, не дёргая
    администратора. Тот же набор строк, что и на вкладке «Эфир».
    """
    from fastapi.responses import StreamingResponse
    from routers.reports import _build_xlsx

    adv = _get_advertiser(db, aid)
    d_from, d_to = _period(date_from, date_to)
    start, end = _utc_bounds(d_from, d_to)
    rows = db.execute(text("""
        SELECT pl.started_at + INTERVAL '3 hours' AS started_msk,
               EXTRACT(ISODOW FROM pl.started_at + INTERVAL '3 hours')::int AS dow,
               ROUND(EXTRACT(EPOCH FROM (pl.ended_at - pl.started_at))::numeric, 1) AS seconds,
               COALESCE(m.title, m.filename) AS creative,
               s.name AS screen, s.city, s.location
        FROM play_log pl
        JOIN media m ON m.id = pl.media_id
        JOIN screens s ON s.id = pl.screen_id
        WHERE m.advertiser_id = :aid AND pl.started_at >= :start AND pl.started_at < :end
        ORDER BY pl.started_at
    """), {"aid": aid, "start": start, "end": end}).fetchall()

    dow = ["", "пн", "вт", "ср", "чт", "пт", "сб", "вс"]
    data = [[r.started_msk.strftime("%d.%m.%Y %H:%M:%S"), dow[r.dow], r.creative,
             r.screen, ", ".join(x for x in (r.city, r.location) if x), float(r.seconds or 0)]
            for r in rows]
    buf = _build_xlsx([(f"Выходы {d_from:%d.%m.%Y}-{d_to:%d.%m.%Y}",
                        ["Дата и время (МСК)", "День", "Ролик", "Экран", "Адрес", "Длительность, с"],
                        data)])
    fname = f"airtime_{aid}_{d_from:%Y%m%d}_{d_to:%Y%m%d}.xlsx"
    return StreamingResponse(
        buf, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'})


@router.get("/advertisers/{aid}/creatives")
def advertiser_creatives(aid: int = Depends(advertiser_scope),
                         admin: dict = Depends(get_current_admin),
                         db: Session = Depends(get_db)):
    """
    Ролики рекламодателя: статус модерации 38-ФЗ, сроки действия и до какой
    даты обязаны храниться подтверждения показа.

    Срок хранения — ст. 12 ФЗ «О рекламе»: рекламные материалы и договоры
    хранятся год со дня последнего распространения. Считаем от последнего
    фактического показа, чтобы было видно, когда материал можно удалять.
    """
    _get_advertiser(db, aid)
    rows = db.execute(text("""
        SELECT m.id, COALESCE(m.title, m.filename) AS title, m.filename,
               m.duration_seconds, m.category, m.age_rating, m.erid,
               m.review_status, m.reviewed_by, m.reviewed_at, m.reject_reason,
               m.valid_from, m.valid_until, m.is_broken,
               (SELECT MAX(pl.started_at) FROM play_log pl WHERE pl.media_id = m.id) AS last_play,
               (SELECT COUNT(*) FROM play_log pl WHERE pl.media_id = m.id) AS plays_total
        FROM media m
        WHERE m.advertiser_id = :aid AND m.status <> 'document'
        ORDER BY m.created_at DESC
    """), {"aid": aid}).fetchall()

    out = []
    for r in rows:
        d = dict(r._mapping)
        # до какой даты хранить подтверждения (год с последнего показа)
        d["keep_until"] = (r.last_play + timedelta(days=365)).date().isoformat() if r.last_play else None
        out.append(d)
    return out
