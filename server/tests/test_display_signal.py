"""Регрессия контроля видеовыхода (HDMI/DisplayPort), миграция 028.

Две части, которые на железе проверить дорого, а сломать легко:
  1. разбор /sys/class/drm/*/status агентом (ds_heartbeat.read_display_status);
  2. правило тревоги «монитор отключён» на сервере (ds_notify.compute_alerts).

Проверяем в том числе главную защиту от ложных срабатываний: NULL (статус
неизвестен) не должен трактоваться как «монитор отключён».
"""
import os
import sys
import types

import pytest

# ─── Импорт агентского модуля ───────────────────────────────────────────────
# Код агента лежит вне образа сервера; в dev-стеке он смонтирован в /agent
# (см. docker-compose.yml). Зависимость requests агенту нужна только для
# самой отправки heartbeat — для разбора статуса выходов она не при чём,
# поэтому подставляем заглушку вместо установки лишнего пакета в ds_api.
_AGENT_DIRS = ["/agent", os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))), "agent")]
for _d in _AGENT_DIRS:
    if os.path.isfile(os.path.join(_d, "ds_heartbeat.py")):
        sys.path.insert(0, _d)
        break
sys.modules.setdefault("requests", types.ModuleType("requests"))
ds_heartbeat = pytest.importorskip(
    "ds_heartbeat", reason="код агента недоступен (каталог agent/ не смонтирован)")

from ds_notify import compute_alerts   # noqa: E402
from conftest import db_row            # noqa: E402


# ─── 1. Агент: разбор /sys/class/drm ────────────────────────────────────────

def _make_drm(tmp_path, connectors):
    """Создать поддельный /sys/class/drm: {'card0-HDMI-A-1': 'connected', ...}."""
    root = tmp_path / "drm"
    root.mkdir()
    (root / "renderD128").mkdir()          # не коннектор — должен игнорироваться
    for name, status in connectors.items():
        d = root / name
        d.mkdir()
        (d / "status").write_text(status + "\n")
    return str(root)


@pytest.fixture
def drm(tmp_path, monkeypatch):
    def _setup(connectors):
        monkeypatch.setattr(ds_heartbeat, "DRM_PATH", _make_drm(tmp_path, connectors))
    return _setup


def test_display_connected_when_any_output_has_monitor(drm):
    drm({"card0-HDMI-A-1": "connected", "card0-DP-1": "disconnected"})
    connected, outputs = ds_heartbeat.read_display_status()
    assert connected is True
    assert "HDMI-A-1:connected" in outputs


def test_display_disconnected_when_all_outputs_empty(drm):
    drm({"card0-HDMI-A-1": "disconnected", "card0-DP-1": "disconnected"})
    connected, _ = ds_heartbeat.read_display_status()
    assert connected is False


def test_unknown_status_is_not_treated_as_disconnected(drm):
    # VGA/DVI на части драйверов всегда отдают unknown — это НЕ повод
    # объявлять монитор отключённым, иначе получим вечную ложную тревогу.
    drm({"card0-VGA-1": "unknown"})
    connected, outputs = ds_heartbeat.read_display_status()
    assert connected is None
    assert outputs == "VGA-1:unknown"


def test_writeback_connector_ignored(drm):
    # Writeback — виртуальный выход, у части драйверов вечно «connected».
    drm({"card0-Writeback-1": "connected", "card0-HDMI-A-1": "disconnected"})
    connected, outputs = ds_heartbeat.read_display_status()
    assert connected is False
    assert "Writeback" not in outputs


def test_missing_drm_directory_gives_unknown(monkeypatch):
    monkeypatch.setattr(ds_heartbeat, "DRM_PATH", "/нет/такого/каталога")
    assert ds_heartbeat.read_display_status() == (None, "")


# ─── 2. Сервер: тревога «монитор отключён» ──────────────────────────────────

def _settings(**over):
    s = {"notify_offline": False, "notify_disk": False, "notify_broken": False,
         "notify_display": True, "offline_minutes": 10, "disk_free_pct": 10}
    s.update(over)
    return s


def test_display_alert_built_for_screen_without_monitor(fake_db):
    db = fake_db([db_row(id=7, name="Касса 1", display_outputs="HDMI-A-1:disconnected")])
    alerts = compute_alerts(db, _settings())
    assert "display:7" in alerts
    atype, message = alerts["display:7"]
    assert atype == "display"
    assert "Касса 1" in message and "HDMI-A-1:disconnected" in message


def test_display_alert_disabled_by_setting(fake_db):
    db = fake_db([db_row(id=7, name="Касса 1", display_outputs="")])
    assert compute_alerts(db, _settings(notify_display=False)) == {}


def test_display_alert_absent_for_old_settings_row(fake_db):
    # БД без миграции 028: ключа notify_display в настройках нет — код обязан
    # молча пропустить проверку, а не упасть с KeyError.
    db = fake_db([db_row(id=7, name="Касса 1", display_outputs="")])
    settings = _settings()
    del settings["notify_display"]
    assert compute_alerts(db, settings) == {}
