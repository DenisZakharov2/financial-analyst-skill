"""Упаковка: файлы зависимостей и версия пакета должны быть согласованы.

Lock-файл обещает «проверенный набор версий», поэтому он обязан совпадать по
составу с `requirements.txt` и попадать в его диапазоны. Версия пакета живёт
в трёх местах (`pyproject.toml`, `SKILL.md`, `finanalyst/__init__.py`) и раньше
расходилась молча.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ENTRY = re.compile(r"^([A-Za-z0-9_.\-]+)\s*(.*)$")


def _entries(name: str) -> dict[str, str]:
    """Пакет → версия или диапазон: строки без комментариев и пустых строк."""
    result: dict[str, str] = {}
    text = (ROOT / name).read_text(encoding="utf-8")
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        match = ENTRY.match(line)
        assert match, f"{name}: не разобрана строка {line!r}"
        result[match.group(1).lower()] = match.group(2).replace(" ", "")
    return result


def test_lock_and_requirements_list_same_packages():
    ranges = _entries("requirements.txt")
    lock = _entries("requirements.lock.txt")
    assert set(lock) == set(ranges), f"только в lock: {set(lock) - set(ranges)}, " \
                                     f"только в requirements: {set(ranges) - set(lock)}"


def test_lock_versions_fit_requirements_ranges():
    specifiers = pytest.importorskip("packaging.specifiers")
    ranges = _entries("requirements.txt")
    for package, pinned in _entries("requirements.lock.txt").items():
        version = pinned.removeprefix("==")
        allowed = specifiers.SpecifierSet(ranges[package])
        assert version in allowed, f"{package}=={version} вне диапазона {ranges[package]}"


def test_lock_file_is_pinned():
    for package, pinned in _entries("requirements.lock.txt").items():
        assert re.fullmatch(r"==\d+(\.\d+)*", pinned), f"{package}: {pinned!r} — не точная версия"


def test_versions_match_everywhere():
    from finanalyst import __version__

    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    project = re.search(r'^version = "([^"]+)"', pyproject, re.M)
    skill = re.search(r"^version:\s*(\S+)$",
                      (ROOT / "skills/financial-analyst/SKILL.md").read_text(encoding="utf-8"),
                      re.M)
    assert project and skill, "не найдена версия в pyproject.toml или SKILL.md"
    assert project.group(1) == skill.group(1) == __version__
