import pandas as pd
import pytest
from finanalyst.core import canonical_period, normalize_periods, parse_period


@pytest.mark.parametrize("value,expected,kind", [
    ("2026-01", "2026-01-01", "M"),
    ("2026-01-15", "2026-01-15", "D"),
    ("01.2026", "2026-01-01", "M"),
    ("1к02", "2002-01-01", "Q"),
    ("4кв2025", "2025-10-01", "Q"),
    ("2020 год", "2020-01-01", "Y"),
    ("январь 2020", "2020-01-01", "M"),
])
def test_parse_period(value, expected, kind):
    parsed, period_type = parse_period(value)
    assert parsed.strftime("%Y-%m-%d") == expected
    assert period_type == kind


def test_parse_period_rejects_garbage():
    parsed, period_type = parse_period("нет данных")
    assert pd.isna(parsed)
    assert period_type == ""


def test_normalize_periods_sorts_by_date_not_string():
    frame = pd.DataFrame({"period": ["4к25", "1к02", "2026-01"], "value": [1, 2, 3]})
    result = normalize_periods(frame)
    assert list(result["value"]) == [2, 1, 3]
    assert result["period_start"].is_monotonic_increasing


@pytest.mark.parametrize("value", ["2024-13", "2024-02-30", "19.2024", "0.2024"])
def test_parse_period_returns_nat_for_impossible_dates(value):
    """Невозможная дата — неразобранный период, а не исключение: его ловит контроль качества."""
    parsed, period_type = parse_period(value)
    assert pd.isna(parsed)
    assert period_type == ""


def test_canonical_period_keeps_impossible_date_as_text():
    assert canonical_period("2024-13") == "2024-13"


def test_normalize_periods_tolerates_impossible_dates():
    frame = pd.DataFrame({"period": ["2024-13", "2024-02"], "value": [1, 2]})
    result = normalize_periods(frame)
    assert int(result["period_start"].isna().sum()) == 1
    assert int(result["period_start"].notna().sum()) == 1
    assert list(result.loc[result["period_start"].notna(), "value"]) == [2]


@pytest.mark.parametrize("value,expected", [
    ("2024-01", "2024-01"),
    ("01.2024", "2024-01"),
    ("январь 2024", "2024-01"),
    ("1к2024", "2024-01"),
    ("нет данных", "нет данных"),
])
def test_canonical_period(value, expected):
    assert canonical_period(value) == expected
