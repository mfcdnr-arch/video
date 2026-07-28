"""Биллинг: тарифы рекламодателей, предварительный расчёт, счета, печать счёта в PDF.

Модель (решение от 2026-07-06): у каждого рекламодателя свой способ
расчёта — ЛИБО по минутам эфира, ЛИБО по количеству показов — и свои
индивидуальные цены. Счёт фиксирует тариф и объёмы на момент закрытия
периода (снимок), чтобы последующие изменения цен не меняли уже
выставленные суммы.
"""
import io
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from deps import get_db, get_current_admin, require_write

router = APIRouter()

BILLING_MODES = {"per_minute", "per_play"}
INVOICE_STATUSES = {"issued", "paid", "canceled"}


def _parse_date(value: str, field: str) -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        raise HTTPException(400, f"{field}: ожидается дата в формате YYYY-MM-DD")


def _period_stats(db: Session, advertiser_id: int, period_start: date, period_end: date):
    """Показы и минуты эфира рекламодателя за период (обе границы включительно)."""
    row = db.execute(text("""
        SELECT COUNT(*) AS plays,
               COALESCE(SUM(EXTRACT(EPOCH FROM (pl.ended_at - pl.started_at))), 0) / 60.0 AS minutes
        FROM play_log pl
        JOIN media m ON m.id = pl.media_id
        WHERE m.advertiser_id = :aid
          AND pl.started_at >= CAST(:d_from AS timestamp) - INTERVAL '3 hours'
          AND pl.started_at < CAST(:d_to AS timestamp) + INTERVAL '21 hours'
    """), {"aid": advertiser_id, "d_from": period_start, "d_to": period_end}).fetchone()
    plays = int(row.plays or 0)
    minutes = round(float(row.minutes or 0), 1)
    return plays, minutes


def _calc_amount(billing_mode: str, price: float, plays: int, minutes: float) -> float:
    if billing_mode == "per_play":
        return round(plays * price, 2)
    return round(minutes * price, 2)


# ─── Тарифы рекламодателя ───────────────────────────────────────────────────

@router.patch("/advertisers/{adv_id}/billing")
def update_advertiser_billing(adv_id: int, body: dict = Body(...),
                              admin: dict = Depends(require_write),
                              db: Session = Depends(get_db)):
    """Настройка биллинга рекламодателя: способ расчёта и цены."""
    exists = db.execute(text("SELECT id FROM advertisers WHERE id = :id"), {"id": adv_id}).fetchone()
    if not exists:
        raise HTTPException(404, "Рекламодатель не найден")

    sets, params = [], {"id": adv_id}
    if "billing_mode" in body:
        mode = str(body["billing_mode"])
        if mode not in BILLING_MODES:
            raise HTTPException(400, f"billing_mode должен быть одним из: {', '.join(sorted(BILLING_MODES))}")
        sets.append("billing_mode = :mode")
        params["mode"] = mode
    if "price_per_minute" in body:
        try:
            params["ppm"] = float(body["price_per_minute"])
        except (TypeError, ValueError):
            raise HTTPException(400, "price_per_minute: ожидается число")
        if params["ppm"] < 0:
            raise HTTPException(400, "price_per_minute не может быть отрицательной")
        sets.append("price_per_minute = :ppm")
    if "price_per_play" in body:
        try:
            params["ppp"] = float(body["price_per_play"])
        except (TypeError, ValueError):
            raise HTTPException(400, "price_per_play: ожидается число")
        if params["ppp"] < 0:
            raise HTTPException(400, "price_per_play не может быть отрицательной")
        sets.append("price_per_play = :ppp")

    if not sets:
        raise HTTPException(400, "Нечего обновлять")

    was = db.execute(text("SELECT name, billing_mode, price_per_minute, price_per_play "
                          "FROM advertisers WHERE id = :id"), {"id": adv_id}).fetchone()
    row = db.execute(text(
        f"UPDATE advertisers SET {', '.join(sets)} WHERE id = :id "
        "RETURNING id, name, billing_mode, price_per_minute, price_per_play"
    ), params).fetchone()
    # Отдельная запись в журнал: изменение цены — денежное решение, и в карточке
    # рекламодателя должно быть видно, кто и когда его принял.
    db.execute(text("""
        INSERT INTO audit_log (event_type, title, detail, actor)
        VALUES ('billing', 'Изменён тариф', :d, :who)
    """), {"d": f"{row.name}: {was.billing_mode} {float(was.price_per_minute or 0):g}/"
                f"{float(was.price_per_play or 0):g} → {row.billing_mode} "
                f"{float(row.price_per_minute or 0):g}/{float(row.price_per_play or 0):g}",
           "who": admin.get("username", "admin")})
    db.commit()
    return dict(row._mapping)


# ─── Предварительный расчёт (до выставления счёта) ──────────────────────────

@router.get("/billing/preview")
def billing_preview(period_start: str = Query(...), period_end: str = Query(...),
                    advertiser_id: int = Query(None),
                    admin: dict = Depends(get_current_admin),
                    db: Session = Depends(get_db)):
    """
    Расчёт по текущим тарифам за период — что будет в счёте, если закрыть
    период сейчас. Без advertiser_id — по всем рекламодателям сразу.
    """
    d_from = _parse_date(period_start, "period_start")
    d_to = _parse_date(period_end, "period_end")
    if d_to < d_from:
        raise HTTPException(400, "period_end раньше period_start")

    # Госорганы в расчёт не берём: тарифа у них нет, счета им не выставляются
    # (миграция 030). Иначе они висели бы в списке нулевыми строками.
    where = "WHERE COALESCE(a.kind, 'advertiser') <> 'gov'"
    params = {}
    if advertiser_id is not None:
        where += " AND a.id = :aid"
        params["aid"] = advertiser_id
    advertisers = db.execute(text(f"""
        SELECT a.id, a.name, COALESCE(a.billing_mode, 'per_minute') AS billing_mode,
               COALESCE(a.price_per_minute, 0) AS price_per_minute,
               COALESCE(a.price_per_play, 0) AS price_per_play
        FROM advertisers a {where} ORDER BY a.name
    """), params).fetchall()
    if advertiser_id is not None and not advertisers:
        raise HTTPException(404, "Рекламодатель не найден")

    items, total = [], 0.0
    for a in advertisers:
        plays, minutes = _period_stats(db, a.id, d_from, d_to)
        price = float(a.price_per_play if a.billing_mode == "per_play" else a.price_per_minute)
        amount = _calc_amount(a.billing_mode, price, plays, minutes)
        total += amount
        # уже есть действующий счёт на этот период?
        inv = db.execute(text("""
            SELECT id, status FROM invoices
            WHERE advertiser_id = :aid AND period_start = :ds AND period_end = :de
              AND status <> 'canceled'
        """), {"aid": a.id, "ds": d_from, "de": d_to}).fetchone()
        items.append({
            "advertiser_id": a.id, "advertiser": a.name,
            "billing_mode": a.billing_mode, "price": price,
            "plays": plays, "minutes": minutes, "amount": amount,
            "existing_invoice_id": inv.id if inv else None,
            "existing_invoice_status": inv.status if inv else None,
        })

    return {"period_start": d_from.isoformat(), "period_end": d_to.isoformat(),
            "items": items, "total": round(total, 2)}


@router.get("/billing/downtime")
def billing_downtime(period_start: str = Query(...), period_end: str = Query(...),
                     admin: dict = Depends(get_current_admin),
                     db: Session = Depends(get_db)):
    """
    Справка о простое экранов за период: часы офлайна по каждому экрану,
    вычисленные по пробелам в heartbeat-логе (minipc_status): разрыв между
    соседними отметками больше 10 минут считается простоем, края периода
    без отметок — тоже. Используется владельцем как основание для скидки
    в счёте (сами суммы биллинга по показам простой уже учитывают —
    ролики в простое не игрались и не тарифицировались).
    """
    d_from = _parse_date(period_start, "period_start")
    d_to = _parse_date(period_end, "period_end")
    if d_to < d_from:
        raise HTTPException(400, "period_end раньше period_start")

    GAP = 600  # секунд без heartbeat, после которых считаем простоем

    p_start = datetime(d_from.year, d_from.month, d_from.day)
    p_end = datetime(d_to.year, d_to.month, d_to.day) + timedelta(days=1)
    now_naive = datetime.now(timezone.utc).replace(tzinfo=None)
    p_end_capped = min(p_end, now_naive)  # будущее простоем не считаем
    if p_end_capped <= p_start:
        return {"period_start": d_from.isoformat(), "period_end": d_to.isoformat(), "screens": []}
    period_seconds = (p_end_capped - p_start).total_seconds()

    hb = db.execute(text("""
        WITH hb AS (
            SELECT screen_id, timestamp AS ts,
                   LAG(timestamp) OVER (PARTITION BY screen_id ORDER BY timestamp) AS prev
            FROM minipc_status
            WHERE timestamp >= :df AND timestamp < :dt
        )
        SELECT screen_id,
               COALESCE(SUM(EXTRACT(EPOCH FROM (ts - prev)))
                        FILTER (WHERE EXTRACT(EPOCH FROM (ts - prev)) > :gap), 0) AS gap_seconds,
               MIN(ts) AS first_hb, MAX(ts) AS last_hb
        FROM hb GROUP BY screen_id
    """), {"df": p_start, "dt": p_end_capped, "gap": GAP}).fetchall()
    hb_map = {r.screen_id: r for r in hb}

    screens = db.execute(text("SELECT id, name, created_at FROM screens ORDER BY name")).fetchall()
    result = []
    for s in screens:
        # экран появился позже начала периода — его «период» короче
        eff_start = max(p_start, s.created_at) if s.created_at else p_start
        eff_seconds = max(0.0, (p_end_capped - eff_start).total_seconds())
        if eff_seconds <= 0:
            continue
        r = hb_map.get(s.id)
        if not r:
            offline = eff_seconds   # ни одного heartbeat за период
        else:
            offline = float(r.gap_seconds)
            lead = (r.first_hb - eff_start).total_seconds()
            if lead > GAP:
                offline += lead
            tail = (p_end_capped - r.last_hb).total_seconds()
            if tail > GAP:
                offline += tail
        offline = min(offline, eff_seconds)
        result.append({
            "screen_id": s.id, "name": s.name,
            "offline_hours": round(offline / 3600.0, 1),
            "uptime_pct": round(100.0 * (1 - offline / eff_seconds), 1),
        })

    result.sort(key=lambda x: -x["offline_hours"])
    return {"period_start": d_from.isoformat(), "period_end": d_to.isoformat(),
            "period_hours": round(period_seconds / 3600.0, 1), "screens": result}


@router.get("/billing/details")
def billing_details(advertiser_id: int = Query(...),
                    period_start: str = Query(...), period_end: str = Query(...),
                    admin: dict = Depends(get_current_admin),
                    db: Session = Depends(get_db)):
    """
    Детализация по роликам рекламодателя за период: показы, время эфира и
    ожидаемый доход по каждому ролику. Считается той же логикой и по тому же
    периоду, что и /billing/preview, поэтому сумма строк сходится с суммой
    счёта копейка в копейку (с точностью до округления по строкам).
    """
    d_from = _parse_date(period_start, "period_start")
    d_to = _parse_date(period_end, "period_end")
    if d_to < d_from:
        raise HTTPException(400, "period_end раньше period_start")

    adv = db.execute(text("""
        SELECT id, name, COALESCE(billing_mode, 'per_minute') AS billing_mode,
               COALESCE(price_per_minute, 0) AS price_per_minute,
               COALESCE(price_per_play, 0) AS price_per_play
        FROM advertisers WHERE id = :id
    """), {"id": advertiser_id}).fetchone()
    if not adv:
        raise HTTPException(404, "Рекламодатель не найден")
    price = float(adv.price_per_play if adv.billing_mode == "per_play" else adv.price_per_minute)

    rows = db.execute(text("""
        SELECT m.id AS media_id, COALESCE(m.title, m.filename) AS title,
               COUNT(pl.id) AS plays,
               COALESCE(SUM(EXTRACT(EPOCH FROM (pl.ended_at - pl.started_at))), 0) / 60.0 AS minutes
        FROM play_log pl
        JOIN media m ON m.id = pl.media_id
        WHERE m.advertiser_id = :aid
          AND pl.started_at >= CAST(:d_from AS timestamp) - INTERVAL '3 hours'
          AND pl.started_at < CAST(:d_to AS timestamp) + INTERVAL '21 hours'
        GROUP BY m.id, m.title, m.filename
        ORDER BY minutes DESC, plays DESC
    """), {"aid": advertiser_id, "d_from": d_from, "d_to": d_to}).fetchall()

    items, total = [], 0.0
    for r in rows:
        plays = int(r.plays or 0)
        minutes = round(float(r.minutes or 0), 1)
        income = _calc_amount(adv.billing_mode, price, plays, minutes)
        total += income
        items.append({"media_id": r.media_id, "title": r.title,
                      "plays": plays, "minutes": minutes, "income": income})

    return {"advertiser_id": adv.id, "advertiser": adv.name,
            "billing_mode": adv.billing_mode, "price": price,
            "period_start": d_from.isoformat(), "period_end": d_to.isoformat(),
            "items": items, "total": round(total, 2)}


# ─── Счета ──────────────────────────────────────────────────────────────────

@router.post("/billing/invoices")
def create_invoice(body: dict = Body(...), admin: dict = Depends(require_write),
                   db: Session = Depends(get_db)):
    """
    Закрыть период для рекламодателя: посчитать объёмы по play_log,
    зафиксировать тариф и сумму, создать счёт.
    Тело: {"advertiser_id": 1, "period_start": "2026-07-01", "period_end": "2026-07-31", "note": "..."}
    """
    adv_id = body.get("advertiser_id")
    if not adv_id:
        raise HTTPException(400, "advertiser_id обязателен")
    d_from = _parse_date(body.get("period_start"), "period_start")
    d_to = _parse_date(body.get("period_end"), "period_end")
    if d_to < d_from:
        raise HTTPException(400, "period_end раньше period_start")

    adv = db.execute(text("""
        SELECT id, name, COALESCE(billing_mode, 'per_minute') AS billing_mode,
               COALESCE(price_per_minute, 0) AS price_per_minute,
               COALESCE(price_per_play, 0) AS price_per_play,
               COALESCE(kind, 'advertiser') AS kind
        FROM advertisers WHERE id = :id
    """), {"id": adv_id}).fetchone()
    if not adv:
        raise HTTPException(404, "Рекламодатель не найден")
    if adv.kind == "gov":
        raise HTTPException(400, "Это госорган: счета ему не выставляются")

    plays, minutes = _period_stats(db, adv.id, d_from, d_to)
    price = float(adv.price_per_play if adv.billing_mode == "per_play" else adv.price_per_minute)
    amount = _calc_amount(adv.billing_mode, price, plays, minutes)

    # Корректировка (скидка за простой и т.п.): отрицательная = скидка.
    adjustment, adj_note = 0.0, None
    if body.get("adjustment_amount") not in (None, "", 0):
        try:
            adjustment = round(float(body["adjustment_amount"]), 2)
        except (TypeError, ValueError):
            raise HTTPException(400, "adjustment_amount: ожидается число")
        adj_note = (body.get("adjustment_note") or "").strip() or None

    # Срок оплаты берём из действующего договора (payment_days). Без договора
    # срок не выдумываем — в дебиторке такой счёт просто не будет «просроченным».
    contract = db.execute(text("""
        SELECT id, payment_days FROM contracts
        WHERE advertiser_id = :aid AND is_active
          AND (valid_from IS NULL OR valid_from <= :de)
          AND (valid_to IS NULL OR valid_to >= :ds)
        ORDER BY id DESC LIMIT 1
    """), {"aid": adv.id, "ds": d_from, "de": d_to}).fetchone()
    contract_id = contract.id if contract else None
    due_date = (d_to + timedelta(days=int(contract.payment_days))) if contract else None

    try:
        row = db.execute(text("""
            INSERT INTO invoices (advertiser_id, period_start, period_end,
                                  billing_mode, price, plays_total, minutes_total, amount,
                                  adjustment_amount, adjustment_note, note,
                                  contract_id, due_date)
            VALUES (:aid, :ds, :de, :mode, :price, :plays, :minutes, :amount,
                    :adj, :adjn, :note, :ctr, :due)
            RETURNING id
        """), {"aid": adv.id, "ds": d_from, "de": d_to, "mode": adv.billing_mode,
               "price": price, "plays": plays, "minutes": minutes, "amount": amount,
               "adj": adjustment, "adjn": adj_note,
               "note": (body.get("note") or "").strip() or None,
               "ctr": contract_id, "due": due_date})
        invoice_id = row.fetchone()[0]
        db.commit()
    except Exception:
        db.rollback()
        raise HTTPException(409, "На этот период у рекламодателя уже есть неотменённый счёт")

    return {"id": invoice_id, "advertiser_id": adv.id, "advertiser": adv.name,
            "period_start": d_from.isoformat(), "period_end": d_to.isoformat(),
            "billing_mode": adv.billing_mode, "price": price,
            "plays_total": plays, "minutes_total": minutes,
            "amount": amount, "adjustment_amount": adjustment, "adjustment_note": adj_note,
            "total_due": round(amount + adjustment, 2), "status": "issued"}


@router.get("/billing/invoices")
def list_invoices(advertiser_id: int = Query(None), status: str = Query(None),
                  admin: dict = Depends(get_current_admin),
                  db: Session = Depends(get_db)):
    conds, params = [], {}
    if advertiser_id is not None:
        conds.append("i.advertiser_id = :aid")
        params["aid"] = advertiser_id
    if status is not None:
        if status not in INVOICE_STATUSES:
            raise HTTPException(400, f"status должен быть одним из: {', '.join(sorted(INVOICE_STATUSES))}")
        conds.append("i.status = :st")
        params["st"] = status
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    rows = db.execute(text(f"""
        SELECT i.id, i.advertiser_id, a.name AS advertiser,
               i.period_start, i.period_end, i.billing_mode, i.price,
               i.plays_total, i.minutes_total, i.amount, i.status,
               COALESCE(i.adjustment_amount, 0) AS adjustment_amount, i.adjustment_note,
               i.amount + COALESCE(i.adjustment_amount, 0) AS total_due,
               i.note, i.created_at, i.paid_at
        FROM invoices i
        JOIN advertisers a ON a.id = i.advertiser_id
        {where}
        ORDER BY i.created_at DESC, i.id DESC
    """), params).fetchall()
    return [dict(r._mapping) for r in rows]


@router.patch("/billing/invoices/{invoice_id}")
def update_invoice(invoice_id: int, body: dict = Body(...),
                   admin: dict = Depends(require_write),
                   db: Session = Depends(get_db)):
    """Смена статуса счёта (issued/paid/canceled) и/или примечания."""
    inv = db.execute(text("SELECT id, status FROM invoices WHERE id = :id"),
                     {"id": invoice_id}).fetchone()
    if not inv:
        raise HTTPException(404, "Счёт не найден")

    sets, params = [], {"id": invoice_id}
    if "status" in body:
        new_status = str(body["status"])
        if new_status not in INVOICE_STATUSES:
            raise HTTPException(400, f"status должен быть одним из: {', '.join(sorted(INVOICE_STATUSES))}")
        sets.append("status = :st")
        params["st"] = new_status
        # paid_at ставим при оплате, сбрасываем при возврате в issued/canceled
        sets.append("paid_at = " + ("NOW()" if new_status == "paid" else "NULL"))
    if "note" in body:
        sets.append("note = :note")
        params["note"] = (body.get("note") or "").strip() or None
    if "adjustment_amount" in body:
        # скидку можно менять только до оплаты
        if inv.status == "paid" and body.get("status") != "issued":
            raise HTTPException(409, "Счёт оплачен — сначала снимите отметку об оплате")
        try:
            params["adj"] = round(float(body.get("adjustment_amount") or 0), 2)
        except (TypeError, ValueError):
            raise HTTPException(400, "adjustment_amount: ожидается число")
        sets.append("adjustment_amount = :adj")
    if "adjustment_note" in body:
        sets.append("adjustment_note = :adjn")
        params["adjn"] = (body.get("adjustment_note") or "").strip() or None

    if not sets:
        raise HTTPException(400, "Нечего обновлять")

    try:
        db.execute(text(f"UPDATE invoices SET {', '.join(sets)} WHERE id = :id"), params)
        db.commit()
    except Exception:
        db.rollback()
        # восстановление отменённого счёта, когда на период уже выставлен новый
        raise HTTPException(409, "На этот период уже есть другой неотменённый счёт")

    row = db.execute(text("""
        SELECT i.id, i.advertiser_id, a.name AS advertiser, i.period_start, i.period_end,
               i.billing_mode, i.price, i.plays_total, i.minutes_total, i.amount,
               COALESCE(i.adjustment_amount, 0) AS adjustment_amount, i.adjustment_note,
               i.amount + COALESCE(i.adjustment_amount, 0) AS total_due,
               i.status, i.note, i.created_at, i.paid_at
        FROM invoices i JOIN advertisers a ON a.id = i.advertiser_id
        WHERE i.id = :id
    """), {"id": invoice_id}).fetchone()
    return dict(row._mapping)


@router.delete("/billing/invoices/{invoice_id}")
def delete_invoice(invoice_id: int, admin: dict = Depends(require_write),
                   db: Session = Depends(get_db)):
    """Удалить счёт. Оплаченные счета удалять нельзя — сначала отменить оплату."""
    inv = db.execute(text("SELECT status FROM invoices WHERE id = :id"), {"id": invoice_id}).fetchone()
    if not inv:
        raise HTTPException(404, "Счёт не найден")
    if inv.status == "paid":
        raise HTTPException(409, "Оплаченный счёт нельзя удалить — сначала снимите отметку об оплате")
    db.execute(text("DELETE FROM invoices WHERE id = :id"), {"id": invoice_id})
    db.commit()
    return {"status": "deleted"}


# ─── Печать счёта в PDF ─────────────────────────────────────────────────────

@router.get("/billing/invoices/{invoice_id}/pdf")
def invoice_pdf(invoice_id: int, admin: dict = Depends(get_current_admin),
                db: Session = Depends(get_db)):
    inv = db.execute(text("""
        SELECT i.*, a.name AS advertiser
        FROM invoices i JOIN advertisers a ON a.id = i.advertiser_id
        WHERE i.id = :id
    """), {"id": invoice_id}).fetchone()
    if not inv:
        raise HTTPException(404, "Счёт не найден")

    from reportlab.lib.pagesizes import A4
    from reportlab.lib import colors
    from reportlab.lib.units import mm
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    # Шрифты DejaVu уже зарегистрированы при загрузке routers/reports.py
    # (регистрация в pdfmetrics глобальная на процесс).

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle('TitleRu', parent=styles['Title'], fontName='DejaVuSans-Bold')
    normal_style = ParagraphStyle('NormalRu', parent=styles['Normal'], fontName='DejaVuSans')

    if inv.billing_mode == "per_play":
        unit_label = "Показ ролика"
        qty = f"{inv.plays_total} шт."
        price_label = f"{float(inv.price):.2f} ₽/показ"
    else:
        unit_label = "Минута эфирного времени"
        qty = f"{float(inv.minutes_total):.1f} мин."
        price_label = f"{float(inv.price):.2f} ₽/мин."

    status_ru = {"issued": "Выставлен", "paid": "Оплачен", "canceled": "Отменён"}.get(inv.status, inv.status)

    elems = [
        Paragraph(f"Счёт № {inv.id}", title_style),
        Spacer(1, 4 * mm),
        Paragraph(f"Рекламодатель: {inv.advertiser}", normal_style),
        Paragraph(f"Период: {inv.period_start.strftime('%d.%m.%Y')} — {inv.period_end.strftime('%d.%m.%Y')}", normal_style),
        Paragraph(f"Дата выставления: {inv.created_at.strftime('%d.%m.%Y')}", normal_style),
        Paragraph(f"Статус: {status_ru}", normal_style),
        Spacer(1, 6 * mm),
    ]

    adjustment = float(inv.adjustment_amount or 0)
    total_due = float(inv.amount) + adjustment

    data = [
        ["Услуга", "Объём", "Тариф", "Сумма"],
        [unit_label, qty, price_label, f"{float(inv.amount):.2f} ₽"],
    ]
    if adjustment:
        adj_label = "Скидка" if adjustment < 0 else "Доплата"
        if inv.adjustment_note:
            adj_label += f" ({inv.adjustment_note})"
        data.append([adj_label, "", "", f"{adjustment:+.2f} ₽"])
    data.append(["", "", "Итого к оплате:", f"{total_due:.2f} ₽"])

    last = len(data) - 1
    table = Table(data, colWidths=[70 * mm, 35 * mm, 40 * mm, 35 * mm])
    table.setStyle(TableStyle([
        ('FONTNAME', (0, 0), (-1, -1), 'DejaVuSans'),
        ('FONTNAME', (0, 0), (-1, 0), 'DejaVuSans-Bold'),
        ('FONTNAME', (2, last), (-1, last), 'DejaVuSans-Bold'),
        ('FONTSIZE', (0, 0), (-1, -1), 10),
        ('GRID', (0, 0), (-1, last - 1), 0.5, colors.grey),
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#eeeeee')),
        ('ALIGN', (1, 0), (-1, -1), 'RIGHT'),
    ]))
    elems.append(table)
    if inv.note:
        elems.append(Spacer(1, 6 * mm))
        elems.append(Paragraph(f"Примечание: {inv.note}", normal_style))
    elems.append(Spacer(1, 10 * mm))
    elems.append(Paragraph(f"Справочно: за период показано {inv.plays_total} показов, "
                           f"{float(inv.minutes_total):.1f} минут эфира.", normal_style))

    buf = io.BytesIO()
    SimpleDocTemplate(buf, pagesize=A4, title=f"Счёт №{inv.id}").build(elems)
    buf.seek(0)
    return StreamingResponse(buf, media_type="application/pdf", headers={
        "Content-Disposition": f'attachment; filename="invoice_{inv.id}.pdf"'
    })
