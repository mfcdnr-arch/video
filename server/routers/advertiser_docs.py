"""
Документы по рекламодателю: реквизиты сторон, договоры и три документа —
эфирная справка, акт оказанных услуг и сводный отчёт за период.

Почему файлы сохраняются на диск, а не собираются заново при каждом скачивании:
акт подписывают. Если через месяц открыть «тот же» акт и получить пересчитанный
по текущим данным документ (кто-то добавил показ, поменял тариф, ролик
переименовали), подписанный экземпляр и его копия разойдутся — а это спор с
рекламодателем на ровном месте. Поэтому документ формируется один раз,
кладётся в /data/backups/documents, хеш содержимого пишется в БД, а скачивание
отдаёт ровно тот файл. Пересобрать можно только явно — новой версией.

Налоги: расчёты по УСН, «НДС не облагается» (решение заказчика). Поля ставки
в company_settings есть на случай смены режима — тогда меняются данные, а не
схема.
"""
import hashlib
import io
import os
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from deps import get_db, get_current_admin, require_write, require_superadmin, advertiser_scope
from routers.billing import _calc_amount, _parse_date
from routers.advertiser_office import _utc_bounds, _period, _get_advertiser

router = APIRouter()

DOC_DIR = os.path.join(os.getenv("BACKUP_DIR", "/data/backups"), "documents")
DOC_TITLES = {"airtime": "Эфирная справка", "act": "Акт оказанных услуг",
              "summary": "Сводный отчёт"}
# Больше этого числа строк в PDF-справку не кладём: файл становится
# неподъёмным, а детализация всё равно нужна в Excel (его формируем всегда).
PDF_ROWS_LIMIT = 1500
DOW = ["", "пн", "вт", "ср", "чт", "пт", "сб", "вс"]


# ─── Реквизиты исполнителя (наши) ───────────────────────────────────────────

_COMPANY_FIELDS = ["legal_name", "short_name", "inn", "kpp", "ogrn", "legal_address",
                   "postal_address", "bank_name", "bank_bik", "bank_account",
                   "corr_account", "director_post", "director_name", "accountant_name",
                   "phone", "email", "vat_mode", "vat_rate"]


@router.get("/company/settings")
def get_company(admin: dict = Depends(get_current_admin), db: Session = Depends(get_db)):
    row = db.execute(text("SELECT * FROM company_settings WHERE id = 1")).mappings().first()
    return dict(row) if row else {}


@router.patch("/company/settings")
def update_company(body: dict = Body(...), admin: dict = Depends(require_superadmin),
                   db: Session = Depends(get_db)):
    sets, params = [], {}
    for f in _COMPANY_FIELDS:
        if f not in body:
            continue
        val = body[f]
        if f == "vat_mode":
            val = str(val or "usn").lower()
            if val not in ("usn", "vat"):
                raise HTTPException(400, "vat_mode: usn или vat")
        if f == "vat_rate":
            try:
                val = float(val or 0)
            except (TypeError, ValueError):
                raise HTTPException(400, "vat_rate: ожидается число")
        sets.append(f"{f} = :{f}")
        params[f] = val
    if not sets:
        raise HTTPException(400, "Нечего обновлять")
    sets.append("updated_at = NOW()")
    db.execute(text(f"UPDATE company_settings SET {', '.join(sets)} WHERE id = 1"), params)
    db.execute(text("""
        INSERT INTO audit_log (event_type, title, detail, actor)
        VALUES ('settings', 'Изменены реквизиты организации', :d, :who)
    """), {"d": ", ".join(params.keys()), "who": admin["username"]})
    db.commit()
    return {"status": "ok"}


# ─── Договоры ───────────────────────────────────────────────────────────────

@router.get("/advertisers/{aid}/contracts")
def list_contracts(aid: int, admin: dict = Depends(get_current_admin),
                   db: Session = Depends(get_db)):
    rows = db.execute(text("""
        SELECT * FROM contracts WHERE advertiser_id = :aid
        ORDER BY is_active DESC, COALESCE(valid_from, signed_on) DESC NULLS LAST, id DESC
    """), {"aid": aid}).fetchall()
    return [dict(r._mapping) for r in rows]


@router.post("/advertisers/{aid}/contracts")
def create_contract(aid: int, body: dict = Body(...), admin: dict = Depends(require_write),
                    db: Session = Depends(get_db)):
    """
    Договор задаёт расчётный период, а он НЕ обязан быть календарным месяцем:
    period_kind='month' — календарный, 'days' — каждые period_days от
    period_anchor. Отсюда же берётся срок оплаты счёта.
    """
    _get_advertiser(db, aid)
    number = (body.get("number") or "").strip()
    if not number:
        raise HTTPException(400, "Номер договора обязателен")
    kind = body.get("period_kind") or "month"
    if kind not in ("month", "days"):
        raise HTTPException(400, "period_kind: month или days")
    days = body.get("period_days")
    if kind == "days":
        try:
            days = int(days)
        except (TypeError, ValueError):
            raise HTTPException(400, "period_days: ожидается число дней")
        if days < 1:
            raise HTTPException(400, "period_days должен быть больше нуля")
    else:
        days = None

    def _d(field):
        v = body.get(field)
        return _parse_date(v, field) if v else None

    row = db.execute(text("""
        INSERT INTO contracts (advertiser_id, number, signed_on, valid_from, valid_to,
                               period_kind, period_days, period_anchor, payment_days,
                               auto_renew, note, created_by)
        VALUES (:aid, :num, :signed, :vfrom, :vto, :kind, :days, :anchor, :pay,
                :renew, :note, :who)
        RETURNING *
    """), {"aid": aid, "num": number, "signed": _d("signed_on"), "vfrom": _d("valid_from"),
           "vto": _d("valid_to"), "kind": kind, "days": days, "anchor": _d("period_anchor"),
           "pay": int(body.get("payment_days") or 5),
           "renew": bool(body.get("auto_renew")), "note": body.get("note"),
           "who": admin["username"]}).fetchone()
    db.execute(text("""
        INSERT INTO audit_log (event_type, title, detail, actor)
        VALUES ('billing', 'Добавлен договор', :d, :who)
    """), {"d": f"№{number}", "who": admin["username"]})
    db.commit()
    return dict(row._mapping)


@router.patch("/contracts/{contract_id}")
def update_contract(contract_id: int, body: dict = Body(...),
                    admin: dict = Depends(require_write), db: Session = Depends(get_db)):
    allowed = {"number": str, "payment_days": int, "auto_renew": bool, "is_active": bool,
               "note": str, "period_days": int}
    sets, params = [], {"id": contract_id}
    for f, caster in allowed.items():
        if f in body:
            v = body[f]
            if caster is int and v is not None:
                v = int(v)
            if caster is bool:
                v = bool(v)
            sets.append(f"{f} = :{f}")
            params[f] = v
    for f in ("signed_on", "valid_from", "valid_to", "period_anchor"):
        if f in body:
            params[f] = _parse_date(body[f], f) if body[f] else None
            sets.append(f"{f} = :{f}")
    if not sets:
        raise HTTPException(400, "Нечего обновлять")
    row = db.execute(text(f"UPDATE contracts SET {', '.join(sets)} WHERE id = :id RETURNING *"),
                     params).fetchone()
    if not row:
        raise HTTPException(404, "Договор не найден")
    db.commit()
    return dict(row._mapping)


@router.delete("/contracts/{contract_id}")
def delete_contract(contract_id: int, admin: dict = Depends(require_write),
                    db: Session = Depends(get_db)):
    db.execute(text("DELETE FROM contracts WHERE id = :id"), {"id": contract_id})
    db.commit()
    return {"status": "deleted"}


# ─── Данные периода для документов ──────────────────────────────────────────

def _period_facts(db: Session, aid: int, d_from: date, d_to: date):
    """Объёмы и построчная детализация выходов за период (МСК-границы)."""
    start, end = _utc_bounds(d_from, d_to)
    p = {"aid": aid, "start": start, "end": end}
    rows = db.execute(text("""
        SELECT pl.started_at + INTERVAL '3 hours' AS started_msk,
               EXTRACT(ISODOW FROM pl.started_at + INTERVAL '3 hours')::int AS dow,
               EXTRACT(EPOCH FROM (pl.ended_at - pl.started_at)) AS seconds,
               COALESCE(m.title, m.filename) AS creative,
               s.name AS screen, s.city, s.location
        FROM play_log pl
        JOIN media m ON m.id = pl.media_id
        JOIN screens s ON s.id = pl.screen_id
        WHERE m.advertiser_id = :aid AND pl.started_at >= :start AND pl.started_at < :end
        ORDER BY pl.started_at
    """), p).fetchall()
    by_day = db.execute(text("""
        SELECT (pl.started_at + INTERVAL '3 hours')::date AS on_date, COUNT(*) AS plays,
               COALESCE(SUM(EXTRACT(EPOCH FROM (pl.ended_at - pl.started_at))), 0) / 60.0 AS minutes
        FROM play_log pl JOIN media m ON m.id = pl.media_id
        WHERE m.advertiser_id = :aid AND pl.started_at >= :start AND pl.started_at < :end
        GROUP BY 1 ORDER BY 1
    """), p).fetchall()
    by_screen = db.execute(text("""
        SELECT s.name AS screen, s.city, s.location, COUNT(*) AS plays,
               COALESCE(SUM(EXTRACT(EPOCH FROM (pl.ended_at - pl.started_at))), 0) / 60.0 AS minutes
        FROM play_log pl JOIN media m ON m.id = pl.media_id JOIN screens s ON s.id = pl.screen_id
        WHERE m.advertiser_id = :aid AND pl.started_at >= :start AND pl.started_at < :end
        GROUP BY s.name, s.city, s.location ORDER BY plays DESC
    """), p).fetchall()
    plays = len(rows)
    minutes = round(sum(float(r.seconds or 0) for r in rows) / 60.0, 1)
    return {"rows": rows, "by_day": by_day, "by_screen": by_screen,
            "plays": plays, "minutes": minutes}


def _amount_for(db: Session, adv, plays: int, minutes: float):
    price = float(adv.price_per_play if adv.billing_mode == "per_play" else adv.price_per_minute)
    return price, _calc_amount(adv.billing_mode, price, plays, minutes)


def _party_block(company) -> list:
    """Строки реквизитов исполнителя для шапки документа."""
    if not company:
        return ["Исполнитель: реквизиты не заполнены (Настройки → Реквизиты организации)"]
    out = [f"Исполнитель: {company.get('legal_name') or company.get('short_name') or '—'}"]
    ident = ", ".join(x for x in (
        f"ИНН {company['inn']}" if company.get("inn") else None,
        f"КПП {company['kpp']}" if company.get("kpp") else None,
        f"ОГРН {company['ogrn']}" if company.get("ogrn") else None) if x)
    if ident:
        out.append(ident)
    if company.get("legal_address"):
        out.append(f"Адрес: {company['legal_address']}")
    bank = ", ".join(x for x in (
        company.get("bank_name"),
        f"р/с {company['bank_account']}" if company.get("bank_account") else None,
        f"к/с {company['corr_account']}" if company.get("corr_account") else None,
        f"БИК {company['bank_bik']}" if company.get("bank_bik") else None) if x)
    if bank:
        out.append(f"Банк: {bank}")
    return out


def _customer_block(adv) -> list:
    out = [f"Заказчик: {adv.legal_name or adv.name}"]
    ident = ", ".join(x for x in (
        f"ИНН {adv.inn}" if adv.inn else None,
        f"КПП {adv.kpp}" if adv.kpp else None) if x)
    if ident:
        out.append(ident)
    if adv.legal_address:
        out.append(f"Адрес: {adv.legal_address}")
    return out


# ─── Сборка PDF ─────────────────────────────────────────────────────────────

def _pdf(elements_builder, title: str) -> bytes:
    from reportlab.lib.pagesizes import A4
    from reportlab.platypus import SimpleDocTemplate
    # Шрифты DejaVu регистрируются при импорте routers/reports.py (глобально
    # на процесс) — без них кириллица в PDF превращается в квадраты.
    import routers.reports  # noqa: F401
    buf = io.BytesIO()
    SimpleDocTemplate(buf, pagesize=A4, title=title).build(elements_builder())
    return buf.getvalue()


def _styles():
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    s = getSampleStyleSheet()
    return {
        "title": ParagraphStyle('T', parent=s['Title'], fontName='DejaVuSans-Bold', fontSize=14),
        "h2": ParagraphStyle('H', parent=s['Heading2'], fontName='DejaVuSans-Bold', fontSize=11),
        "n": ParagraphStyle('N', parent=s['Normal'], fontName='DejaVuSans', fontSize=9, leading=12),
        "small": ParagraphStyle('S', parent=s['Normal'], fontName='DejaVuSans', fontSize=8,
                                leading=10, textColor="#555555"),
    }


def _table(data, widths, align_right_from=1):
    from reportlab.lib import colors
    from reportlab.platypus import Table, TableStyle
    t = Table(data, colWidths=widths, repeatRows=1)
    t.setStyle(TableStyle([
        ('FONTNAME', (0, 0), (-1, -1), 'DejaVuSans'),
        ('FONTNAME', (0, 0), (-1, 0), 'DejaVuSans-Bold'),
        ('FONTSIZE', (0, 0), (-1, -1), 8),
        ('GRID', (0, 0), (-1, -1), 0.4, colors.HexColor('#999999')),
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#eeeeee')),
        ('ALIGN', (align_right_from, 1), (-1, -1), 'RIGHT'),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
    ]))
    return t


def _fmt_period(d_from: date, d_to: date) -> str:
    return f"{d_from:%d.%m.%Y} — {d_to:%d.%m.%Y}"


def _build_airtime_pdf(company, adv, d_from, d_to, facts, number):
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, Spacer

    def build():
        st = _styles()
        e = [Paragraph(f"Эфирная справка № {number}", st["title"]), Spacer(1, 3 * mm)]
        for line in _party_block(company) + _customer_block(adv):
            e.append(Paragraph(line, st["n"]))
        e.append(Spacer(1, 3 * mm))
        e.append(Paragraph(f"Период размещения: {_fmt_period(d_from, d_to)} (время московское)", st["n"]))
        e.append(Paragraph(f"Всего выходов: {facts['plays']}, суммарное время в эфире: "
                           f"{facts['minutes']:.1f} мин.", st["n"]))
        e.append(Spacer(1, 5 * mm))

        rows = facts["rows"]
        if len(rows) <= PDF_ROWS_LIMIT:
            e.append(Paragraph("Детализация выходов", st["h2"]))
            data = [["Дата и время", "День", "Ролик", "Экран", "Сек"]]
            for r in rows:
                data.append([r.started_msk.strftime("%d.%m.%Y %H:%M:%S"), DOW[r.dow],
                             r.creative, r.screen, f"{float(r.seconds or 0):.0f}"])
            e.append(_table(data, [32 * mm, 12 * mm, 55 * mm, 45 * mm, 14 * mm], 4))
        else:
            # Тысячи строк в PDF нечитаемы и весят десятки мегабайт: даём
            # сводку по дням, а построчную детализацию — в Excel-версии.
            e.append(Paragraph("Сводка по дням", st["h2"]))
            data = [["Дата", "День", "Выходов", "Минут"]]
            for r in facts["by_day"]:
                data.append([r.on_date.strftime("%d.%m.%Y"), DOW[r.on_date.isoweekday()],
                             str(r.plays), f"{float(r.minutes):.1f}"])
            e.append(_table(data, [35 * mm, 20 * mm, 30 * mm, 30 * mm]))
            e.append(Spacer(1, 3 * mm))
            e.append(Paragraph(f"Выходов за период — {len(rows)}. Построчная детализация "
                               f"каждого показа приведена в приложении к настоящей справке "
                               f"(файл Excel).", st["small"]))
        e.append(Spacer(1, 6 * mm))
        e.append(Paragraph("Справка сформирована автоматически по журналу показов системы "
                           "цифровых экранов. Каждая строка соответствует фактическому "
                           "воспроизведению ролика на экране.", st["small"]))
        return e
    return _pdf(build, f"Эфирная справка {number}")


def _build_act_pdf(company, adv, d_from, d_to, facts, price, amount, number, contract):
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, Spacer

    def build():
        st = _styles()
        vat_mode = (company or {}).get("vat_mode", "usn")
        vat_rate = float((company or {}).get("vat_rate") or 0)
        e = [Paragraph(f"Акт № {number} оказанных услуг", st["title"])]
        e.append(Paragraph(f"от {date.today():%d.%m.%Y}", st["n"]))
        e.append(Spacer(1, 3 * mm))
        for line in _party_block(company) + _customer_block(adv):
            e.append(Paragraph(line, st["n"]))
        if contract:
            base = f"Основание: договор № {contract['number']}"
            if contract.get("signed_on"):
                base += f" от {contract['signed_on']:%d.%m.%Y}"
            e.append(Paragraph(base, st["n"]))
        e.append(Spacer(1, 4 * mm))
        e.append(Paragraph(f"Период оказания услуг: {_fmt_period(d_from, d_to)}", st["n"]))
        e.append(Spacer(1, 4 * mm))

        if adv.billing_mode == "per_play":
            unit, qty, price_s = "показ", f"{facts['plays']}", f"{price:.2f} ₽/показ"
        else:
            unit, qty, price_s = "мин.", f"{facts['minutes']:.1f}", f"{price:.2f} ₽/мин."
        # Наименование услуги — в Paragraph, иначе длинная строка не
        # переносится и наезжает на соседнюю колонку.
        service = Paragraph("Размещение рекламных материалов на цифровых экранах",
                            st["n"])
        data = [["Наименование услуги", "Ед.", "Кол-во", "Цена", "Сумма"],
                [service, unit, qty, price_s, f"{amount:.2f} ₽"]]
        if vat_mode == "vat" and vat_rate:
            vat = round(amount * vat_rate / 100.0, 2)
            data.append(["", "", "", f"НДС {vat_rate:g}%", f"{vat:.2f} ₽"])
            data.append(["", "", "", "Итого с НДС", f"{amount + vat:.2f} ₽"])
        else:
            data.append(["", "", "", "Итого", f"{amount:.2f} ₽"])
        e.append(_table(data, [78 * mm, 14 * mm, 20 * mm, 28 * mm, 28 * mm], 1))
        e.append(Spacer(1, 3 * mm))
        if vat_mode != "vat" or not vat_rate:
            e.append(Paragraph("НДС не облагается (применяется упрощённая система "
                               "налогообложения).", st["n"]))
        e.append(Spacer(1, 4 * mm))
        e.append(Paragraph(f"Фактический объём подтверждается эфирной справкой за тот же "
                           f"период: {facts['plays']} выходов, {facts['minutes']:.1f} мин. "
                           f"эфирного времени.", st["n"]))
        e.append(Spacer(1, 4 * mm))
        e.append(Paragraph("Услуги оказаны в полном объёме. Заказчик претензий по объёму и "
                           "качеству не имеет.", st["n"]))
        e.append(Spacer(1, 12 * mm))

        director = (company or {}).get("director_name") or ""
        post = (company or {}).get("director_post") or "Руководитель"
        sign = [["Исполнитель", "Заказчик"],
                [Paragraph(f"{post}<br/><br/>_______________ / {director} /", st["n"]),
                 Paragraph("Уполномоченный представитель<br/><br/>"
                           "_______________ / ______________ /", st["n"])],
                ["М.П.", "М.П."]]
        from reportlab.platypus import Table, TableStyle
        t = Table(sign, colWidths=[85 * mm, 85 * mm])
        t.setStyle(TableStyle([('FONTNAME', (0, 0), (-1, -1), 'DejaVuSans'),
                               ('FONTNAME', (0, 0), (-1, 0), 'DejaVuSans-Bold'),
                               ('FONTSIZE', (0, 0), (-1, -1), 9),
                               ('TOPPADDING', (0, 1), (-1, -1), 10)]))
        e.append(t)
        return e
    return _pdf(build, f"Акт {number}")


def _build_summary_pdf(company, adv, d_from, d_to, facts, amount, delivery, number):
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, Spacer

    def build():
        st = _styles()
        days = (d_to - d_from).days + 1
        e = [Paragraph(f"Отчёт о размещении рекламы", st["title"]),
             Paragraph(f"{adv.legal_name or adv.name} · {_fmt_period(d_from, d_to)}", st["n"]),
             Spacer(1, 5 * mm)]

        e.append(Paragraph("Итоги периода", st["h2"]))
        e.append(_table([["Показатель", "Значение"],
                         ["Выходов в эфир", str(facts["plays"])],
                         ["Время в эфире, мин.", f"{facts['minutes']:.1f}"],
                         ["В среднем выходов в день", f"{facts['plays'] / days:.1f}" if days else "0"],
                         ["Экранов задействовано", str(len(facts["by_screen"]))],
                         ["Сумма за период, ₽", f"{amount:.2f}"]],
                        [95 * mm, 45 * mm]))
        e.append(Spacer(1, 5 * mm))

        e.append(Paragraph("Размещение по экранам", st["h2"]))
        data = [["Экран", "Адрес", "Выходов", "Минут"]]
        for r in facts["by_screen"]:
            data.append([r.screen, ", ".join(x for x in (r.city, r.location) if x),
                         str(r.plays), f"{float(r.minutes):.1f}"])
        e.append(_table(data, [45 * mm, 65 * mm, 25 * mm, 25 * mm], 2))
        e.append(Spacer(1, 5 * mm))

        e.append(Paragraph("Выходы по дням", st["h2"]))
        data = [["Дата", "День", "Выходов", "Минут"]]
        for r in facts["by_day"]:
            data.append([r.on_date.strftime("%d.%m.%Y"), DOW[r.on_date.isoweekday()],
                         str(r.plays), f"{float(r.minutes):.1f}"])
        e.append(_table(data, [35 * mm, 20 * mm, 30 * mm, 30 * mm]))

        if delivery:
            e.append(Spacer(1, 5 * mm))
            e.append(Paragraph("Качество доставки", st["h2"]))
            data = [["Экран", "Простой, ч", "Без монитора, ч", "Доступность"]]
            for s in delivery["screens"]:
                data.append([s["name"],
                             "—" if s["offline_hours"] is None else f"{s['offline_hours']:.1f}",
                             "—" if s["display_off_hours"] is None else f"{s['display_off_hours']:.1f}",
                             "нет данных" if s["availability_pct"] is None else f"{s['availability_pct']:.1f}%"])
            e.append(_table(data, [50 * mm, 30 * mm, 40 * mm, 30 * mm]))
            e.append(Spacer(1, 2 * mm))
            e.append(Paragraph("Простой — время, когда мини-ПК не выходил на связь. "
                               "«Без монитора» — экран работал, но монитор был отключён от "
                               "видеовыхода. Оба показателя берутся из телеметрии устройств.",
                               st["small"]))

        e.append(Spacer(1, 6 * mm))
        e.append(Paragraph("Отчёт сформирован автоматически по журналу показов. Данные о "
                           "количестве контактов с аудиторией не приводятся: система "
                           "фиксирует фактические выходы рекламы, а не число зрителей.",
                           st["small"]))
        return e
    return _pdf(build, f"Отчёт {number}")


def _build_airtime_xlsx(facts, d_from, d_to) -> bytes:
    from routers.reports import _build_xlsx
    data = [[r.started_msk.strftime("%d.%m.%Y %H:%M:%S"), DOW[r.dow], r.creative, r.screen,
             ", ".join(x for x in (r.city, r.location) if x), round(float(r.seconds or 0), 1)]
            for r in facts["rows"]]
    buf = _build_xlsx([(f"Выходы {d_from:%d.%m.%Y}-{d_to:%d.%m.%Y}",
                        ["Дата и время (МСК)", "День", "Ролик", "Экран", "Адрес",
                         "Длительность, с"], data)])
    return buf.getvalue()


# ─── Формирование и выдача документов ───────────────────────────────────────

def _next_number(db: Session, aid: int, doc_type: str, d_to: date) -> str:
    """Номер вида 12/АКТ-20260731 — уникален в пределах рекламодателя и типа."""
    n = db.execute(text("""
        SELECT COUNT(*) FROM advertiser_documents
        WHERE advertiser_id = :aid AND doc_type = :t
    """), {"aid": aid, "t": doc_type}).scalar() or 0
    code = {"airtime": "ЭС", "act": "АКТ", "summary": "ОТЧ"}.get(doc_type, "ДОК")
    return f"{aid}-{code}-{d_to:%Y%m%d}-{n + 1}"


def _store(db: Session, aid: int, doc_type: str, fmt: str, d_from: date, d_to: date,
           number: str, content: bytes, actor: str, invoice_id=None) -> dict:
    os.makedirs(DOC_DIR, exist_ok=True)
    digest = hashlib.sha256(content).hexdigest()
    fname = f"{aid}_{doc_type}_{d_from:%Y%m%d}_{d_to:%Y%m%d}_{digest[:8]}.{fmt}"
    path = os.path.join(DOC_DIR, fname)
    with open(path, "wb") as f:
        f.write(content)
    row = db.execute(text("""
        INSERT INTO advertiser_documents
            (advertiser_id, invoice_id, doc_type, doc_format, period_start, period_end,
             number, filename, size_bytes, sha256, created_by)
        VALUES (:aid, :inv, :t, :fmt, :df, :dt, :num, :fn, :sz, :sha, :who)
        RETURNING id, doc_type, doc_format, number, period_start, period_end,
                  size_bytes, created_at
    """), {"aid": aid, "inv": invoice_id, "t": doc_type, "fmt": fmt, "df": d_from, "dt": d_to,
           "num": number, "fn": fname, "sz": len(content), "sha": digest,
           "who": actor}).fetchone()
    return dict(row._mapping)


@router.get("/advertisers/{aid}/documents")
def list_documents(aid: int = Depends(advertiser_scope),
                   admin: dict = Depends(get_current_admin), db: Session = Depends(get_db)):
    rows = db.execute(text("""
        SELECT id, doc_type, doc_format, number, period_start, period_end,
               size_bytes, sha256, created_by, created_at
        FROM advertiser_documents WHERE advertiser_id = :aid
        ORDER BY period_end DESC, created_at DESC
    """), {"aid": aid}).fetchall()
    out = []
    for r in rows:
        d = dict(r._mapping)
        d["title"] = DOC_TITLES.get(r.doc_type, r.doc_type)
        out.append(d)
    return out


@router.post("/advertisers/{aid}/documents")
def generate_documents(aid: int, body: dict = Body(...), admin: dict = Depends(require_write),
                       db: Session = Depends(get_db)):
    """
    Сформировать пакет документов за период: эфирная справка (PDF + Excel),
    акт и сводный отчёт.

    Если за этот период документы уже формировались, повторный вызов ничего
    не пересобирает и отдаёт прежние — иначе подписанный экземпляр разошёлся
    бы с копией. Явная пересборка: {"regenerate": true} — она создаёт НОВУЮ
    версию с новым номером, старая остаётся в реестре.
    """
    adv = _get_advertiser(db, aid)
    d_from, d_to = _period(body.get("date_from"), body.get("date_to"))
    regenerate = bool(body.get("regenerate"))

    existing = db.execute(text("""
        SELECT id, doc_type, doc_format, number, period_start, period_end,
               size_bytes, created_at
        FROM advertiser_documents
        WHERE advertiser_id = :aid AND period_start = :df AND period_end = :dt
        ORDER BY created_at
    """), {"aid": aid, "df": d_from, "dt": d_to}).fetchall()
    if existing and not regenerate:
        return {"status": "exists", "reused": True,
                "documents": [dict(r._mapping) for r in existing],
                "hint": "Документы за этот период уже сформированы. "
                        "Чтобы собрать новую версию, включите пересборку."}

    company = db.execute(text("SELECT * FROM company_settings WHERE id = 1")).mappings().first()
    company = dict(company) if company else {}
    if not company.get("legal_name") and not company.get("short_name"):
        raise HTTPException(400, "Сначала заполните реквизиты организации: "
                                 "Настройки → Реквизиты организации")

    contract = db.execute(text("""
        SELECT * FROM contracts
        WHERE advertiser_id = :aid AND is_active
          AND (valid_from IS NULL OR valid_from <= :dt)
          AND (valid_to IS NULL OR valid_to >= :df)
        ORDER BY id DESC LIMIT 1
    """), {"aid": aid, "df": d_from, "dt": d_to}).mappings().first()
    contract = dict(contract) if contract else None

    facts = _period_facts(db, aid, d_from, d_to)
    price, amount = _amount_for(db, adv, facts["plays"], facts["minutes"])

    # Качество доставки — тем же расчётом, что на вкладке карточки
    from routers.advertiser_office import advertiser_delivery
    try:
        delivery = advertiser_delivery(date_from=d_from.isoformat(), date_to=d_to.isoformat(),
                                       aid=aid, admin=admin, db=db)
    except HTTPException:
        delivery = None

    who = admin["username"]
    docs = []
    num_air = _next_number(db, aid, "airtime", d_to)
    docs.append(_store(db, aid, "airtime", "pdf", d_from, d_to, num_air,
                       _build_airtime_pdf(company, adv, d_from, d_to, facts, num_air), who))
    docs.append(_store(db, aid, "airtime", "xlsx", d_from, d_to, num_air,
                       _build_airtime_xlsx(facts, d_from, d_to), who))
    num_act = _next_number(db, aid, "act", d_to)
    docs.append(_store(db, aid, "act", "pdf", d_from, d_to, num_act,
                       _build_act_pdf(company, adv, d_from, d_to, facts, price, amount,
                                      num_act, contract), who))
    num_sum = _next_number(db, aid, "summary", d_to)
    docs.append(_store(db, aid, "summary", "pdf", d_from, d_to, num_sum,
                       _build_summary_pdf(company, adv, d_from, d_to, facts, amount,
                                          delivery, num_sum), who))

    db.execute(text("""
        INSERT INTO audit_log (event_type, title, detail, actor)
        VALUES ('billing', 'Сформированы документы', :d, :who)
    """), {"d": f"{adv.name}: {_fmt_period(d_from, d_to)}", "who": who})
    db.commit()
    return {"status": "ok", "reused": False, "documents": docs}


@router.get("/advertisers/{aid}/documents/{doc_id}/download")
def download_document(doc_id: int, aid: int = Depends(advertiser_scope),
                      admin: dict = Depends(get_current_admin), db: Session = Depends(get_db)):
    """Отдать сохранённый файл как есть — без пересчёта (см. модуль-докстринг)."""
    row = db.execute(text("""
        SELECT * FROM advertiser_documents WHERE id = :id AND advertiser_id = :aid
    """), {"id": doc_id, "aid": aid}).mappings().first()
    if not row:
        raise HTTPException(404, "Документ не найден")
    path = os.path.join(DOC_DIR, row["filename"])
    if not os.path.exists(path):
        raise HTTPException(410, "Файл документа отсутствует на диске — сформируйте заново")
    title = DOC_TITLES.get(row["doc_type"], row["doc_type"])
    nice = f"{title} {row['number']}.{row['doc_format']}".replace("/", "-")
    media = ("application/pdf" if row["doc_format"] == "pdf"
             else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    return FileResponse(path, media_type=media, filename=nice)


# ─── Дебиторка, история тарифа, заметки (для администратора) ────────────────

@router.get("/billing/receivables")
def receivables(admin: dict = Depends(get_current_admin), db: Session = Depends(get_db)):
    """
    Кто сколько должен: выставлено, оплачено, просрочено и сколько дней висит.
    Срок оплаты берётся из счёта (due_date считается из договора при выставлении).
    """
    rows = db.execute(text("""
        SELECT a.id AS advertiser_id, a.name,
               COUNT(*) FILTER (WHERE i.status = 'issued') AS unpaid_count,
               COALESCE(SUM(i.amount + COALESCE(i.adjustment_amount, 0))
                        FILTER (WHERE i.status = 'issued'), 0) AS unpaid_amount,
               COALESCE(SUM(i.amount + COALESCE(i.adjustment_amount, 0))
                        FILTER (WHERE i.status = 'paid'), 0) AS paid_amount,
               MIN(i.due_date) FILTER (WHERE i.status = 'issued') AS earliest_due,
               COUNT(*) FILTER (WHERE i.status = 'issued'
                                  AND i.due_date IS NOT NULL
                                  AND i.due_date < CURRENT_DATE) AS overdue_count
        FROM advertisers a
        JOIN invoices i ON i.advertiser_id = a.id AND i.status <> 'canceled'
        GROUP BY a.id, a.name
        HAVING COUNT(*) FILTER (WHERE i.status = 'issued') > 0
        ORDER BY overdue_count DESC, unpaid_amount DESC
    """)).fetchall()
    today = date.today()
    out = []
    for r in rows:
        d = dict(r._mapping)
        d["days_overdue"] = (today - r.earliest_due).days if (r.earliest_due and r.earliest_due < today) else 0
        out.append(d)
    return out


@router.get("/advertisers/{aid}/tariff-history")
def tariff_history(aid: int, admin: dict = Depends(get_current_admin),
                   db: Session = Depends(get_db)):
    """Кто и когда менял тариф — из общего журнала операций."""
    adv = _get_advertiser(db, aid)
    rows = db.execute(text("""
        SELECT title, detail, actor, created_at FROM audit_log
        WHERE event_type = 'billing' AND title ILIKE '%тариф%' AND detail ILIKE :pat
        ORDER BY created_at DESC LIMIT 50
    """), {"pat": f"%{adv.name}%"}).fetchall()
    return [dict(r._mapping) for r in rows]


@router.patch("/advertisers/{aid}/note")
def update_note(aid: int, body: dict = Body(...), admin: dict = Depends(require_write),
                db: Session = Depends(get_db)):
    """Заметки по клиенту: договорённости, контакты, особенности размещения."""
    _get_advertiser(db, aid)
    db.execute(text("UPDATE advertisers SET note = :n WHERE id = :id"),
               {"n": (body.get("note") or "").strip() or None, "id": aid})
    db.commit()
    return {"status": "ok"}


@router.patch("/advertisers/{aid}/requisites")
def update_requisites(aid: int, body: dict = Body(...), admin: dict = Depends(require_write),
                      db: Session = Depends(get_db)):
    """Реквизиты рекламодателя — идут в акт и эфирную справку."""
    _get_advertiser(db, aid)
    fields = ("legal_name", "inn", "kpp", "legal_address", "contact_person", "phone", "email")
    sets, params = [], {"id": aid}
    for f in fields:
        if f in body:
            sets.append(f"{f} = :{f}")
            params[f] = (str(body[f]).strip() or None) if body[f] is not None else None
    if not sets:
        raise HTTPException(400, "Нечего обновлять")
    db.execute(text(f"UPDATE advertisers SET {', '.join(sets)} WHERE id = :id"), params)
    db.commit()
    return {"status": "ok"}
