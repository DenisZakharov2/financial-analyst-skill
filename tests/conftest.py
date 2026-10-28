import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "financial-analyst"
sys.path.insert(0, str(SKILL / "scripts"))


@pytest.fixture(scope="session")
def skill_dir() -> Path:
    return SKILL


@pytest.fixture(scope="session")
def sample_dir(skill_dir: Path) -> Path:
    return skill_dir / "assets" / "sample"
