import pandas as pd
import pytest
from finanalyst.core import normalize_periods, parse_period


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
