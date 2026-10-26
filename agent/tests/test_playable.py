import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ds_player import build_playable


def test_repeats_and_order_kept():
    assert build_playable(["a", "a", "b", "a"], set(), lambda f: True) == ["a", "a", "b", "a"]


def test_broken_and_missing_dropped():
    assert build_playable(["a", "x", "b", "m"], {"x"}, lambda f: f != "m") == ["a", "b"]


def test_empty():
    assert build_playable([], set(), lambda f: True) == []
