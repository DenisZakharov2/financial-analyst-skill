import pandas as pd
from finanalyst.core import validate_inputs


def _tables(portfolio: pd.DataFrame, **extra) -> dict:
    return {"portfolio": portfolio, **extra}


def test_duplicate_account_id_fails():
    portfolio = pd.DataFrame({
        "account_id": ["A1", "A1"],
        "client_id": ["C1", "C2"],
        "balance": [10.0, 20.0],
        "dpd": [0, 0],
    })
    report = validate_inputs(_tables(portfolio))
    check = next(item for item in report.checks if item.check_id == "portfolio_pk_unique")
    assert check.status == "fail"
    assert len(report.failed) == 1


def test_negative_balance_is_warning():
    portfolio = pd.DataFrame({
        "account_id": ["A1", "A2"],
        "client_id": ["C1", "C2"],
        "balance": [10.0, -5.0],
        "dpd": [0, 0],
    })
    report = validate_inputs(_tables(portfolio))
    check = next(item for item in report.checks if item.check_id == "portfolio_balance_non_negative")
    assert check.status == "warn"


def test_dpd_bucket_mismatch_is_warning():
    portfolio = pd.DataFrame({
        "account_id": ["A1"],
        "client_id": ["C1"],
        "balance": [10.0],
        "dpd": [95],
        "par_bucket": ["current"],
    })
    report = validate_inputs(_tables(portfolio))
    check = next(item for item in report.checks if item.check_id == "portfolio_dpd_bucket_consistent")
    assert check.status == "warn"


def test_finance_all_rows_warned():
    finance = pd.DataFrame({
        "period": ["2024-01", "2024-01"],
        "product_code": ["P1", "ALL"],
        "interest_income": [10.0, 10.0],
    })
    report = validate_inputs({"finance": finance})
    assert any("product_code=ALL" in item for item in report.warnings)


def test_macro_survey_period_check():
    survey = pd.DataFrame({
        "period_start": pd.to_datetime(["2026-01-01", None]),
        "industry": ["A", "A"],
        "indicator": ["I", "I"],
        "value": [1.0, 2.0],
    })
    report = validate_inputs({"macro_survey": survey})
    check = next(item for item in report.checks if item.check_id == "macro_survey_periods")
    assert check.status == "fail"


def test_missing_required_columns_are_warned():
    portfolio = pd.DataFrame({"client_id": ["C1"], "balance": [10.0]})
    report = validate_inputs(_tables(portfolio))
    check = next(item for item in report.checks if item.check_id == "portfolio_required_columns")
    assert check.status == "warn"
    assert "account_id" in check.detail and "dpd" in check.detail
    assert not report.failed
