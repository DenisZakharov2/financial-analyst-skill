#!/usr/bin/env python3
"""Проверка самого скилла: frontmatter, ссылки, локальные пути, текст.

Запуск: python scripts/check_skill.py
Код возврата: 0 — всё в порядке, 1 — есть ошибки.
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
REQUIRED = ("name", "description", "version", "author", "license", "platforms")
LINK_PATTERN = re.compile(r"`((?:references|scripts|templates|assets)/[^`]+)`")
ABS_PATH_MARKERS = ("/ho" + "me/", "/Us" + "ers/", "C:" + "\\")


def read_frontmatter(text: str) -> dict[str, str]:
    if not text.startswith("---"):
        raise ValueError("SKILL.md должен начинаться с ---")
    end = text.find("\n---", 3)
    if end < 0:
        raise ValueError("Не найден закрывающий ---")
    result: dict[str, str] = {}
    for line in text[3:end].splitlines():
        match = re.match(r"^([a-z_]+):\s*(.+)$", line)
        if match:
            result[match[1]] = match[2].strip().strip('"')
    return result


def check_text(path: Path) -> list[str]:
    issues: list[str] = []
    in_code = False
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if line.startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            continue
        if re.search(r"\S  +\S", line) and not line.lstrip().startswith("|"):
            issues.append(f"{path.name}:{number}: двойной пробел")
        for match in re.finditer(r"[A-Za-zА-Яа-яЁё]+", line):
            word = match.group()
            latin = any("LATIN" in unicodedata.name(ch, "") for ch in word)
            cyrillic = any("CYRILLIC" in unicodedata.name(ch, "") for ch in word)
            if latin and cyrillic:
                issues.append(f"{path.name}:{number}: смешение алфавитов в {word!r}")
    return issues


def main() -> int:
    errors: list[str] = []
    skill_md = SKILL_DIR / "SKILL.md"
    if not skill_md.exists():
        print("Нет SKILL.md")
        return 1

    text = skill_md.read_text(encoding="utf-8")
    try:
        meta = read_frontmatter(text)
    except ValueError as exc:
        print(f"Frontmatter: {exc}")
        return 1

    for key in REQUIRED:
        if key not in meta:
            errors.append(f"frontmatter: нет поля {key}")

    description = meta.get("description", "")
    if len(description) > 60:
        errors.append(f"description: {len(description)} символов, максимум 60")
    if not description.endswith("."):
        errors.append("description: нет точки в конце")

    body = text[text.find("\n---", 3) + 4:]
    if len(body.strip()) < 1000:
        errors.append("тело SKILL.md слишком короткое")

    for link in sorted(set(LINK_PATTERN.findall(text))):
        if not (SKILL_DIR / link).exists():
            errors.append(f"битая ссылка: {link}")

    for path in SKILL_DIR.rglob("*"):
        if not path.is_file() or path.suffix not in {".md", ".py", ".tmpl", ".csv"}:
            continue
        content = path.read_text(encoding="utf-8", errors="ignore")
        for marker in ABS_PATH_MARKERS:
            if marker in content:
                errors.append(f"локальный путь в {path.relative_to(SKILL_DIR)}")
                break
        if path.suffix in {".md", ".tmpl"}:
            errors.extend(check_text(path))

    if errors:
        print("Найдены проблемы:")
        for error in errors:
            print(f"  - {error}")
        return 1

    print(f"OK: {meta['name']} {meta['version']}, description {len(description)} символов, "
          f"ссылки и пути в порядке.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
