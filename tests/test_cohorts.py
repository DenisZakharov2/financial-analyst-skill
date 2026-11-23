"""Тесты модуля когорт: старт когорты и retention-кривые."""

import pandas as pd
from finanalyst.cohorts import cohort_starts, retention_curves


def test_cohort_starts_from_first_product_in_portfolio():
    """Старт — месяц первого продукта, даже если он раньше регистрации."""
    clients = pd.DataFrame({
        "client_id": ["C-1", "C-2"],
        "registration_date": ["2024-01-05", "2023-12-01"],
    })
    portfolio = pd.DataFrame({
        "client_id": ["C-1", "C-1", "C-2"],
        "open_date": ["2024-03-10", "2024-01-20", "2024-02-15"],
    })
    starts = cohort_starts(clients, portfolio)
    assert starts["C-1"] == "2024-01"
    assert starts["C-2"] == "2024-02"


def test_cohort_starts_fallback_to_registration_date():
    """Без портфеля — месяц регистрации."""
    clients = pd.DataFrame({"client_id": ["C-1"], "registration_date": ["2024-05-20"]})
    starts = cohort_starts(clients, None)
    assert starts["C-1"] == "2024-05"


def test_cohort_starts_skips_unparsed_dates():
    clients = pd.DataFrame({"client_id": ["C-1", "C-2"], "registration_date": ["нет даты", "2024-03-01"]})
    starts = cohort_starts(clients, None)
    assert list(starts.index) == ["C-2"]
    assert starts["C-2"] == "2024-03"


def test_retention_curves_shape_and_bounds():
    starts = pd.Series({"C-1": "2024-01", "C-2": "2024-01", "C-3": "2024-02"})
    portfolio = pd.DataFrame({
        "client_id": ["C-1", "C-1", "C-2", "C-3"],
        "open_date": ["2024-01-10", "2024-06-10", "2024-01-15", "2024-02-20"],
        "close_date": [None, None, None, "2024-02-29"],
        "balance": [100.0, 100.0, 100.0, 100.0],
    })
    curves = retention_curves(starts, portfolio, max_age=6)
    assert list(curves.index) == list(range(7))
    assert set(curves.columns) == {"2024-01", "2024-02"}
    assert (curves.loc[0] == 100.0).all()
    assert curves.loc[1, "2024-01"] == 100.0
    # C-3 закрылся в феврале — в марте его нет.
    assert pd.isna(curves.loc[1, "2024-02"])


def test_retention_curves_empty_without_data():
    assert retention_curves(pd.Series(dtype="object"), None).empty
    assert retention_curves(pd.Series({"C-1": "2024-01"}), pd.DataFrame()).empty
