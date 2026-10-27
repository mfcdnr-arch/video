"""active_until в ленте экрана: до какого момента (наивное МСК) действует
активный источник — показ до конца часа, переопределение до полуночи.

Агент без связи не должен доигрывать показ после его часа: истёкший
источник пропускается, играет шаблон, затем заглушки.
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ds_player import active_expired, repoll_after_expiry

MSK = timezone(timedelta(hours=3))
S = {"active_until": "2026-10-27T08:00:00"}


def test_not_expired_before_until():
    assert active_expired(S, datetime(2026, 10, 27, 7, 59, 59)) is False


def test_expired_at_and_after_until():
    assert active_expired(S, datetime(2026, 10, 27, 8, 0)) is True
    assert active_expired(S, datetime(2026, 10, 28, 3, 0)) is True


def test_aware_msk_now_compared_as_wall_clock():
    # агент берёт datetime.now(MSK): сравниваем по московским часам, без сдвига
    assert active_expired(S, datetime(2026, 10, 27, 7, 59, tzinfo=MSK)) is False
    assert active_expired(S, datetime(2026, 10, 27, 8, 0, tzinfo=MSK)) is True


def test_missing_null_or_garbage_never_expires():
    now = datetime(2030, 1, 1)
    assert active_expired({}, now) is False
    assert active_expired({"active_until": None}, now) is False
    assert active_expired({"active_until": "завтра"}, now) is False
    assert active_expired({"active_until": 1793075915}, now) is False
    assert active_expired(None, now) is False


def test_repoll_only_shortly_after_expiry_and_not_too_often():
    at = datetime(2026, 10, 27, 8, 0, 1)
    assert repoll_after_expiry(S, at, seconds_since_poll=30) is True
    assert repoll_after_expiry(S, at, seconds_since_poll=2) is False                         # только что опрашивали
    assert repoll_after_expiry(S, datetime(2026, 10, 27, 7, 59, 50), 30) is False            # ещё не истёк
    assert repoll_after_expiry(S, datetime(2026, 10, 27, 8, 5), 30) is False                 # давно истёк — обычный опрос
    assert repoll_after_expiry({}, at, 30) is False
    assert repoll_after_expiry({"active_until": "мусор"}, at, 30) is False
