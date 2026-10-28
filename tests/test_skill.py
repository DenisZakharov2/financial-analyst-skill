import re
from pathlib import Path

import pytest

REQUIRED_KEYS = ("name", "description", "version", "author", "license", "platforms")


def read_frontmatter(text: str) -> dict:
    assert text.startswith("---"), "SKILL.md должен начинаться с ---"
    end = text.find("\n---", 3)
    assert end > 0, "Не найден закрывающий ---"
    block = text[3:end]
    result = {}
    for line in block.splitlines():
        match = re.match(r"^([a-z_]+):\s*(.+)$", line)
        if match:
            result[match[1]] = match[2].strip().strip('"')
    return result


@pytest.fixture(scope="module")
def skill_text(skill_dir):
    return (skill_dir / "SKILL.md").read_text(encoding="utf-8")


def test_frontmatter_required_fields(skill_text):
    meta = read_frontmatter(skill_text)
    for key in REQUIRED_KEYS:
        assert key in meta, f"нет поля {key}"
    assert meta["name"] == "financial-analyst"


def test_description_is_short_and_complete(skill_text):
    meta = read_frontmatter(skill_text)
    assert len(meta["description"]) <= 60, len(meta["description"])
    assert meta["description"].endswith(".")


def test_body_is_not_empty(skill_text):
    end = skill_text.find("\n---", 3)
    body = skill_text[end + 4:]
    assert len(body.strip()) > 1000
    assert "## Когда использовать" in body
    assert "## Проверка результата" in body


def test_referenced_files_exist(skill_dir, skill_text):
    links = set(re.findall(r"`((?:references|scripts|templates|assets)/[^`]+)`", skill_text))
    assert links, "в SKILL.md нет ссылок на файлы скилла"
    for link in links:
        assert (skill_dir / link).exists(), f"битая ссылка: {link}"


def test_no_absolute_local_paths(skill_dir):
    markers = ("/ho" + "me/", "/Us" + "ers/", "C:" + "\\")
    for path in skill_dir.rglob("*"):
        if path.is_file() and path.suffix in {".md", ".py", ".csv", ".tmpl"}:
            text = path.read_text(encoding="utf-8", errors="ignore")
            for marker in markers:
                assert marker not in text, f"локальный путь в {path}: {marker}"


def test_examples_and_readme_exist():
    root = Path(__file__).resolve().parents[1]
    assert (root / "README.md").exists()
    assert (root / "LICENSE").exists()
    assert (root / "examples" / "quickstart" / "README.md").exists()
