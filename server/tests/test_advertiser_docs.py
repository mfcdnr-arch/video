"""Регрессия документов по рекламодателю (routers/advertiser_docs.py).

Проверяем то, что дороже всего исправлять задним числом: нумерацию
документов (по ней их ищут в переписке и в бухгалтерии) и шапку с
реквизитами — если оттуда что-то тихо пропадёт, акт станет непригодным
для подписания, а заметят это уже у заказчика.
"""
from types import SimpleNamespace

from routers.advertiser_docs import (_next_number, _party_block, _customer_block,
                                     _fmt_period, DOC_TITLES, PDF_ROWS_LIMIT)
from datetime import date


class _CountDB:
    """Мини-мок: execute(...).scalar() отдаёт заданное число документов."""
    def __init__(self, count):
        self._count = count

    def execute(self, *a, **kw):
        return SimpleNamespace(scalar=lambda: self._count)


# ─── Номера документов ──────────────────────────────────────────────────────

def test_number_starts_from_one():
    assert _next_number(_CountDB(0), 7, "act", date(2026, 7, 31)) == "7-АКТ-20260731-1"


def test_number_increments_within_type():
    # второй акт за тот же период — версия 2, прежний остаётся в реестре
    assert _next_number(_CountDB(1), 7, "act", date(2026, 7, 31)) == "7-АКТ-20260731-2"


def test_number_differs_by_document_type():
    d = date(2026, 7, 31)
    assert _next_number(_CountDB(0), 7, "airtime", d) == "7-ЭС-20260731-1"
    assert _next_number(_CountDB(0), 7, "summary", d) == "7-ОТЧ-20260731-1"


def test_number_includes_advertiser_id():
    # номера двух рекламодателей за один период не должны совпадать
    d = date(2026, 7, 31)
    assert _next_number(_CountDB(0), 7, "act", d) != _next_number(_CountDB(0), 8, "act", d)


# ─── Шапка документа ────────────────────────────────────────────────────────

def test_party_block_warns_when_requisites_empty():
    # Пустые реквизиты — не молчаливая пустая шапка, а явное предупреждение
    text = " ".join(_party_block({}))
    assert "не заполнены" in text


def test_party_block_contains_key_requisites():
    company = {"legal_name": "ООО «Ромашка»", "inn": "1234567890", "kpp": "123401001",
               "legal_address": "г. Донецк, ул. Артёма, 1", "bank_name": "Банк",
               "bank_account": "40702810900000012345", "bank_bik": "049514000"}
    text = " ".join(_party_block(company))
    for expect in ("ООО «Ромашка»", "1234567890", "123401001", "Артёма", "40702810900000012345"):
        assert expect in text


def test_customer_block_falls_back_to_display_name():
    # Юр. наименование не заполнено — берём то, под которым заведён кабинет
    adv = SimpleNamespace(name="Кофейня «Утро»", legal_name=None, inn=None, kpp=None,
                          legal_address=None)
    assert "Кофейня «Утро»" in _customer_block(adv)[0]


def test_customer_block_prefers_legal_name():
    adv = SimpleNamespace(name="Кофейня «Утро»", legal_name='ООО «Утро»', inn="7701234567",
                          kpp=None, legal_address=None)
    block = _customer_block(adv)
    assert "ООО «Утро»" in block[0]
    assert "7701234567" in " ".join(block)


# ─── Прочее ─────────────────────────────────────────────────────────────────

def test_period_is_formatted_the_russian_way():
    assert _fmt_period(date(2026, 7, 1), date(2026, 7, 31)) == "01.07.2026 — 31.07.2026"


def test_all_document_types_have_titles():
    # Название типа попадает в имя скачиваемого файла — без него будет «act.pdf»
    for t in ("airtime", "act", "summary"):
        assert DOC_TITLES.get(t)


def test_pdf_row_limit_is_sane():
    # Ниже сотни строк ограничение сделало бы PDF бесполезным, выше десятка
    # тысяч — неподъёмным; детализация сверх лимита уходит в Excel
    assert 100 <= PDF_ROWS_LIMIT <= 10000
