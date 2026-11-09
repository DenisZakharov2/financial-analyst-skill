import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "financial-analyst"
sys.path.insert(0, str(SKILL / "scripts"))


def _months_from_str(date_str: str) -> int:
    """Convert YYYY-MM string to months since epoch."""
    year, month = map(int, date_str.split("-"))
    return year * 12 + month - 1


def _str_from_months(months: int) -> str:
    """Convert months since epoch to YYYY-MM string."""
    year = months // 12
    month = months % 12 + 1
    return f"{year:04d}-{month:02d}"


def date_strings(start: str, periods: int, freq: str = "M") -> list[str]:
    """Generate list of YYYY-MM strings without pandas datetime operations.
    
    Args:
        start: Start date as "YYYY-MM"
        periods: Number of periods
        freq: "M" for monthly, "Q" for quarterly, "Y" for yearly, "D" for daily
    """
    if freq == "M":
        step = 1
    elif freq == "Q":
        step = 3
    elif freq == "Y":
        step = 12
    elif freq == "D":
        step = 0  # Not used for daily
    else:
        step = 1
    
    start_months = _months_from_str(start)
    result = []
    for i in range(periods):
        months = start_months + i * step
        result.append(_str_from_months(months))
    return result


def date_strings_daily(start: str, periods: int) -> list[str]:
    """Generate list of YYYY-MM-DD strings for daily frequency."""
    year, month, day = map(int, start.split("-"))
    result = []
    for i in range(periods):
        # Simple day increment (approximate)
        d = day + i
        m = month
        y = year
        while d > 28:  # Simplified month boundary
            d -= 28
            m += 1
            if m > 12:
                m = 1
                y += 1
        result.append(f"{y:04d}-{m:02d}-{d:02d}")
    return result


@ pytest.fixture(scope="session")
def date_strings_fixture():
    return date_strings


@ pytest.fixture(scope="session")
def date_strings_daily_fixture():
    return date_strings_daily


@ pytest.fixture(scope="session")
def skill_dir() -> Path:
    return SKILL


@ pytest.fixture(scope="session")
def sample_dir(skill_dir: Path) -> Path:
    return skill_dir / "assets" / "sample"
