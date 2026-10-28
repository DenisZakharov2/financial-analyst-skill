import numpy as np
import pandas as pd
import pytest
from finanalyst.metrics import (
    MetricError,
    finance_summary,
    forecast_trend,
    funnel_summary,
    macro_anomalies,
    portfolio_summary,
    unit_economics,
)


def test_portfolio_par_by_dpd():
    products = pd.DataFrame({"product_code": ["P1"], "product_type": ["карта"]})
    portfolio = pd.DataFrame({
        "account_id": ["A1", "A2", "A3"],
        "client_id": ["C1", "C1", "C2"],
        "product_code": ["P1", "P1", "P1"],
        "balance": [100.0, 100.0, 200.0],
        "limit_amount": [100.0, 100.0, 200.0],
        "dpd": [0, 35, 95],
    })
    result = portfolio_summary(portfolio, products)
    row = result["rows"].iloc[0]
    assert row["par30_pct"] == pytest.approx(75.0)
    assert row["par60_pct"] == pytest.approx(50.0)
    assert row["par90_pct"] == pytest.approx(50.0)
    assert result["totals"]["total_balance"] == pytest.approx(400.0)
    assert result["totals"]["par30_pct"] == pytest.approx(75.0)


def _funnel_events(counts: dict[str, int]) -> pd.DataFrame:
    stages = ["лид", "обращение", "заявка", "одобрение", "договор", "активация"]
    rows = []
    for rank, stage in enumerate(stages, start=1):
        for number in range(counts[stage]):
            for reached in range(1, rank + 1):
                rows.append({"application_id": f"A{number:04d}", "stage": stages[reached - 1]})
    return pd.DataFrame(rows)


def test_funnel_is_monotone_and_correct():
    events = _funnel_events({"лид": 100, "обращение": 80, "заявка": 40,
                             "одобрение": 30, "договор": 25, "активация": 20})
    result = funnel_summary(events)
    rows = result["rows"]
    assert list(rows["from_start_pct"]) == [100.0, 80.0, 40.0, 30.0, 25.0, 20.0]
    assert rows["from_start_pct"].is_monotonic_decreasing
    assert rows.iloc[2]["stage_pct"] == pytest.approx(50.0)
    assert result["biggest_loss_stage"] == "заявка"


def test_funnel_unknown_stage_raises():
    events = pd.DataFrame({"application_id": ["A1"], "stage": ["выдача"]})
    with pytest.raises(MetricError, match="Неизвестные этапы"):
        funnel_summary(events)


def test_funnel_without_application_id_marks_assumption():
    events = pd.DataFrame({"client_id": ["C1", "C1"], "stage": ["лид", "заявка"]})
    result = funnel_summary(events)
    assert "application_id" in result["assumption"]
    assert result["entity"] == "клиент"


def test_unit_economics_monthly_arpu():
    finance = pd.DataFrame({
        "period": ["2024-01", "2024-02"],
        "product_code": ["P1", "P1"],
        "interest_income": [600.0, 600.0],
        "fee_income": [0.0, 0.0],
        "funding_cost": [150.0, 150.0],
        "reserve_expense": [50.0, 50.0],
        "opex": [100.0, 100.0],
        "marketing_cost": [100.0, 100.0],
    })
    portfolio = pd.DataFrame({"client_id": [f"C{i}" for i in range(10)],
                              "balance": [100.0] * 10})
    result = unit_economics(finance, portfolio=portfolio, lifetime_months=12)
    assert result["months"] == 2
    assert result["arpu"] == pytest.approx(60.0)          # 1200 / 10 / 2
    assert result["cac"] == pytest.approx(20.0)           # 200 / 10
    assert result["margin_pct"] == pytest.approx(66.6667, rel=1e-3)
    assert result["ltv_gross"] == pytest.approx(480.0)
    assert result["ltv_cac"] == pytest.approx(24.0)
    assert result["payback_months"] == pytest.approx(0.5)


def test_unit_economics_without_marketing_returns_none():
    finance = pd.DataFrame({"period": ["2024-01"], "interest_income": [100.0]})
    portfolio = pd.DataFrame({"client_id": ["C1"], "balance": [10.0]})
    result = unit_economics(finance, portfolio=portfolio)
    assert result["cac"] is None
    assert result["ltv_cac"] is None
    assert any("marketing_cost" in item for item in result["assumptions"])


def test_finance_margin_rows():
    finance = pd.DataFrame({
        "period": ["2024-01"],
        "product_code": ["P1"],
        "interest_income": [100.0],
        "fee_income": [0.0],
        "funding_cost": [40.0],
        "reserve_expense": [10.0],
        "opex": [20.0],
    })
    result = finance_summary(finance)
    row = result["rows"].iloc[0]
    assert row["revenue"] == pytest.approx(100.0)
    assert row["gross_margin"] == pytest.approx(50.0)
    assert row["margin_pct"] == pytest.approx(50.0)


def test_macro_anomalies_are_computed_within_series():
    dates = pd.date_range("2024-01-01", periods=30, freq="MS")
    left = pd.DataFrame({"period_start": dates, "category": "A", "value": 10.0})
    left.loc[5, "value"] = 100.0
    right = pd.DataFrame({"period_start": dates, "category": "B", "value": 1000.0})
    frame = pd.concat([left, right], ignore_index=True)
    anomalies = macro_anomalies(frame, value_col="value", group_cols=("category",))
    assert len(anomalies) == 1
    assert anomalies.iloc[0]["category"] == "A"


def test_forecast_band_order():
    dates = pd.date_range("2020-01-01", periods=12, freq="MS")
    values = np.arange(1, 13, dtype=float) + np.array([0.4, -0.3, 0.2, -0.5, 0.6, -0.2,
                                                       0.3, -0.4, 0.5, -0.1, 0.2, -0.3])
    series = pd.DataFrame({"period_start": dates, "value": values})
    result = forecast_trend(series, horizon=4)
    assert result["available"]
    assert result["scenarios"]["пессимистичный"][0] < result["scenarios"]["базовый"][0]
    assert result["scenarios"]["базовый"][0] < result["scenarios"]["оптимистичный"][0]
    assert result["mae"] > 0


def test_forecast_requires_six_points():
    series = pd.DataFrame({"period_start": pd.date_range("2024-01-01", periods=3, freq="MS"),
                           "value": [1.0, 2.0, 3.0]})
    assert forecast_trend(series)["available"] is False
