"""ds_agent.current_playlist_files: истёкший active_until (лента устарела —
нет связи) пропускает hourly_sequence и active_playlist/active_items, дальше
шаблон и заглушки; без active_until (эфир сети, шаблон) — как раньше.

ds_agent читает конфиг и настраивает логи при импорте: подкладываем
временный config.ini, а requests (в тестовом образе его нет, при импорте он
только упоминается) — пустым модулем.
"""
import importlib
import os
import sys
import types
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture(scope="module")
def agent(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("agent")
    cfg = tmp / "config.ini"
    cfg.write_text(
        "[server]\nhost = 127.0.0.1\nport = 80\ntoken = t\n"
        f"[player]\nmedia_dir = {tmp / 'media'}\n"
        "[schedule]\npoll_interval = 60\nheartbeat_interval = 30\ncommand_poll_interval = 5\n"
        f"[logging]\nlevel = INFO\nfile = {tmp / 'log' / 'agent.log'}\n")
    os.environ["DS_CONFIG"] = str(cfg)
    sys.modules.setdefault("requests", types.ModuleType("requests"))
    return importlib.import_module("ds_agent")


TUE_0737 = datetime(2026, 10, 27, 7, 37)       # вторник, day_of_week = 1
TUE_0800 = datetime(2026, 10, 27, 8, 0)


def feed(**kw):
    base = {"schedule": [{"day_of_week": 1, "hour": 8, "items": [{"filename": "tpl.mp4"}]}],
            "fallback": ["filler.mp4"]}
    base.update(kw)
    return base


def placement(until="2026-10-27T08:00:00"):
    return feed(active_playlist={"playlist_id": 9}, active_source="placement",
                active_items=[{"filename": "a.mp4"}, {"filename": "a.mp4"}],
                hourly_sequence=["a.mp4", "f.mp4", "a.mp4"], active_until=until)


def test_placement_plays_until_its_hour_ends(agent):
    assert agent.current_playlist_files(placement(), TUE_0737) == ["a.mp4", "f.mp4", "a.mp4"]


def test_expired_placement_falls_to_template_then_fallback(agent):
    assert agent.current_playlist_files(placement(), TUE_0800) == ["tpl.mp4"]
    no_slot = placement()
    no_slot["schedule"] = []
    assert agent.current_playlist_files(no_slot, TUE_0800) == ["filler.mp4"]


def test_expired_active_items_branch_is_skipped_too(agent):
    s = placement()
    s["hourly_sequence"] = []
    assert agent.current_playlist_files(s, TUE_0737) == ["a.mp4", "a.mp4"]
    assert agent.current_playlist_files(s, TUE_0800) == ["tpl.mp4"]


def test_off_override_holds_only_until_midnight(agent):
    off = feed(active_playlist={"playlist_id": None, "off": True}, active_source="override",
               active_items=[], hourly_sequence=[], active_until="2026-10-27T00:00:00")
    assert agent.current_playlist_files(off, datetime(2026, 10, 26, 23, 0)) == []
    assert agent.current_playlist_files(off, TUE_0800) == ["tpl.mp4"]


def test_without_active_until_nothing_expires(agent):
    bc = placement(until=None)
    assert agent.current_playlist_files(bc, datetime(2030, 1, 1)) == ["a.mp4", "f.mp4", "a.mp4"]
    old = placement()
    del old["active_until"]                       # лента старого сервера
    assert agent.current_playlist_files(old, TUE_0800) == ["a.mp4", "f.mp4", "a.mp4"]
