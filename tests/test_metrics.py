import numpy as np
import pandas as pd
import pytest
from finanalyst.metrics import (
    MetricError,
    finance_summary,
    forecast_trend,
    funnel_summary,
    macro_anomalies,
    macro_dynamics,
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


def test_macro_anomalies_are_computed_within_series(date_strings_fixture):
    dates = date_strings_fixture("2024-01", 30)
    left = pd.DataFrame({"period_start": dates, "category": "A", "value": 10.0})
    left.loc[5, "value"] = 100.0
    right = pd.DataFrame({"period_start": dates, "category": "B", "value": 1000.0})
    frame = pd.concat([left, right], ignore_index=True)
    anomalies = macro_anomalies(frame, value_col="value", group_cols=("category",))
    assert len(anomalies) == 1
    assert anomalies.iloc[0]["category"] == "A"


def test_forecast_band_order(date_strings_fixture):
    dates = date_strings_fixture("2020-01", 12)
    values = np.arange(1, 13, dtype=float) + np.array([0.4, -0.3, 0.2, -0.5, 0.6, -0.2,
                                                        0.3, -0.4, 0.5, -0.1, 0.2, -0.3])
    series = pd.DataFrame({"period_start": dates, "value": values})
    result = forecast_trend(series, horizon=4)
    assert result["available"]
    assert result["scenarios"]["пессимистичный"][0] < result["scenarios"]["базовый"][0]
    assert result["scenarios"]["базовый"][0] < result["scenarios"]["оптимистичный"][0]
    assert result["mae"] > 0


def test_forecast_requires_six_points():
    series = pd.DataFrame({"period_start": ["2024-01", "2024-02", "2024-03"],
                           "value": [1.0, 2.0, 3.0]})
    assert forecast_trend(series)["available"] is False


def test_forecast_with_perfect_fit_reports_zero_band(date_strings_fixture):
    dates = date_strings_fixture("2020-01", 10)
    series = pd.DataFrame({"period_start": dates, "value": np.arange(1, 11, dtype=float)})
    result = forecast_trend(series)
    assert result["band_mae"] == 0
    assert "MAE бэктеста = 0" in result["assumption"]


@pytest.mark.parametrize("periods,months", [
    (["2024-01", "2024-02"], 2),
    (["01.2024", "02.2024"], 2),
    (["январь 2024", "февраль 2024"], 2),
    (["1к2024", "2к2024"], 6),
    (["2023", "2024"], 24),
])
def test_unit_economics_counts_months_for_supported_periods(periods, months):
    finance = pd.DataFrame({"period": periods, "interest_income": [100.0] * len(periods)})
    portfolio = pd.DataFrame({"client_id": ["C1", "C2"], "balance": [10.0, 10.0]})
    result = unit_economics(finance, portfolio=portfolio)
    assert result["months"] == months
    assert result["arpu"] == pytest.approx(200.0 / 2 / months)


def test_finance_periods_are_canonical():
    finance = pd.DataFrame({
        "period": ["2024-01", "01.2024", "январь 2024"],
        "product_code": ["P1", "P1", "P1"],
        "interest_income": [10.0, 10.0, 10.0],
    })
    result = finance_summary(finance)
    assert len(result["rows"]) == 1
    assert result["rows"].iloc[0]["revenue"] == pytest.approx(30.0)


def test_cac_uses_new_clients_with_account_in_portfolio():
    """Знаменатель CAC — новые клиенты портфеля, а не все зарегистрированные."""
    finance = pd.DataFrame({
        "period": ["2024-01", "2024-02"],
        "interest_income": [100.0, 100.0],
        "marketing_cost": [60.0, 60.0],
    })
    clients = pd.DataFrame({
        "client_id": ["C1", "C2", "C3"],
        "registration_date": ["2024-01-10", "2023-11-01", "2024-02-20"],
    })
    portfolio = pd.DataFrame({"client_id": ["C1", "C2"], "balance": [10.0, 10.0]})
    result = unit_economics(finance, clients=clients, portfolio=portfolio)
    # C3 зарегистрирован в окне, но счёта в портфеле нет: в базу CAC не входит,
    # иначе CAC делится на клиентов, которых нет в ARPU.
    assert result["new_clients"] == 1
    assert result["active_clients"] == 2
    assert result["cac_base"] == 1
    assert result["cac"] == pytest.approx(120.0)
    assert any("со счётом в портфеле" in item for item in result["assumptions"])


def test_cac_counts_all_registered_clients_without_portfolio():
    """Без портфеля база — все клиенты с датой регистрации (документированный фолбэк)."""
    finance = pd.DataFrame({
        "period": ["2024-01", "2024-02"],
        "interest_income": [100.0, 100.0],
        "marketing_cost": [60.0, 60.0],
    })
    clients = pd.DataFrame({
        "client_id": ["C1", "C2", "C3"],
        "registration_date": ["2024-01-10", "2023-11-01", "2024-02-20"],
    })
    result = unit_economics(finance, clients=clients)
    assert result["new_clients"] == 2
    assert result["cac"] == pytest.approx(60.0)
    assert any("по дате регистрации" in item for item in result["assumptions"])


def test_cac_is_null_when_no_new_clients_in_window():
    """Ноль новых клиентов — это не «нет данных»: подменять базу активными нельзя."""
    finance = pd.DataFrame({
        "period": ["2024-01"],
        "interest_income": [100.0],
        "marketing_cost": [40.0],
    })
    clients = pd.DataFrame({
        "client_id": ["C1", "C2"],
        "registration_date": ["2022-05-01", "2023-01-15"],
    })
    portfolio = pd.DataFrame({"client_id": ["C1", "C2"], "balance": [10.0, 10.0]})
    result = unit_economics(finance, clients=clients, portfolio=portfolio)
    assert result["new_clients"] == 0
    assert result["cac"] is None
    assert any("нет новых клиентов" in item for item in result["assumptions"])


def test_cac_falls_back_to_active_clients_without_dates():
    finance = pd.DataFrame({"period": ["2024-01"], "interest_income": [100.0],
                            "marketing_cost": [40.0]})
    clients = pd.DataFrame({"client_id": ["C1", "C2", "C3", "C4"]})
    result = unit_economics(finance, clients=clients)
    assert result["new_clients"] is None
    assert result["cac"] == pytest.approx(10.0)   # 40 / 4 активных клиента
    assert any("Нет даты регистрации" in item for item in result["assumptions"])


def test_cac_without_client_base_returns_none():
    finance = pd.DataFrame({"period": ["2024-01"], "interest_income": [100.0],
                            "marketing_cost": [50.0]})
    result = unit_economics(finance)
    assert result["active_clients"] is None
    assert result["cac"] is None
    assert any("Нет данных о клиентах" in item for item in result["assumptions"])


def test_nim_is_annualized_over_total_balance():
    finance = pd.DataFrame({
        "period": ["2024-01", "2024-02", "2024-03"],
        "product_code": ["P1"] * 3,
        "interest_income": [10.0, 10.0, 10.0],
        "funding_cost": [4.0, 4.0, 4.0],
    })
    portfolio = pd.DataFrame({"client_id": ["C1", "C2"], "balance": [1000.0, 1000.0]})
    totals = finance_summary(finance, portfolio)["totals"]
    # (30 − 12) / 3 мес / 2000 × 100 × 12 месяцев
    assert totals["nim_months"] == 3
    assert totals["nim_pct"] == pytest.approx(3.6)


def test_finance_total_row_is_not_summed_with_detail():
    """Правило словаря: product_code=ALL — итог, а не ещё один продукт."""
    finance = pd.DataFrame({
        "period": ["2024-01", "2024-01", "2024-01", "2024-02", "2024-02", "2024-02"],
        "product_code": ["P1", "P2", "ALL"] * 2,
        "interest_income": [100.0, 200.0, 300.0, 110.0, 220.0, 330.0],
    })
    result = finance_summary(finance)
    assert result["totals"]["revenue"] == pytest.approx(630.0)
    assert result["totals"]["excluded_total_rows"] == 2
    assert set(result["rows"]["product_code"]) == {"P1", "P2"}


def test_finance_keeps_total_row_when_there_is_no_detail():
    """Если детализации нет, выкидывать итог нельзя: ряд станет пустым."""
    finance = pd.DataFrame({
        "period": ["2024-01", "2024-02"],
        "product_code": ["ALL", "ALL"],
        "interest_income": [300.0, 330.0],
    })
    result = finance_summary(finance)
    assert result["totals"]["revenue"] == pytest.approx(630.0)
    assert result["totals"]["excluded_total_rows"] == 0


def test_portfolio_total_row_is_not_summed_with_detail():
    portfolio = pd.DataFrame({
        "account_id": ["AC1", "AC2", "AC3"],
        "client_id": ["C1", "C2", "C1"],
        "product_code": ["P1", "P2", "ALL"],
        "balance": [100.0, 200.0, 300.0],
        "dpd": [0, 0, 0],
    })
    products = pd.DataFrame({"product_code": ["P1", "P2"], "product_type": ["карта", "кредит"]})
    result = portfolio_summary(portfolio, products)
    assert result["totals"]["total_balance"] == pytest.approx(300.0)
    assert result["totals"]["accounts"] == 2
    assert result["totals"]["excluded_total_rows"] == 1


def test_accounts_in_rows_sum_to_total():
    """Счета в разрезе и в итогах — один счётчик: сумма по продуктам равна итогу."""
    portfolio = pd.DataFrame({
        "account_id": ["A1", "A2", None],
        "client_id": ["C1", "C2", "C3"],
        "product_code": ["P1", "P1", "P2"],
        "balance": [10.0, 20.0, 30.0],
        "dpd": [0, 0, 0],
    })
    products = pd.DataFrame({"product_code": ["P1", "P2"], "product_type": ["карта", "кредит"]})
    result = portfolio_summary(portfolio, products)
    assert result["totals"]["accounts"] == 2                       # уникальные непустые account_id
    assert int(result["rows"]["accounts"].sum()) == result["totals"]["accounts"]


def test_change_pct_is_null_when_previous_is_zero(date_strings_fixture):
    """Контракт change_pct: нулевая база → null, а не бесконечность в отчёте."""
    dates = date_strings_fixture("2024-01", 4)
    series = pd.DataFrame({
        "period_start": dates,
        "value_mln_rub": [0.0, 5.0, 0.0, 10.0],
    })
    frame = macro_dynamics(series)
    assert pd.isna(frame.loc[0, "change_pct"])
    assert pd.isna(frame.loc[1, "change_pct"])          # база 0 → null, а не inf
    assert frame.loc[2, "change_pct"] == pytest.approx(-100.0)
    assert pd.isna(frame.loc[3, "change_pct"])
    assert np.isfinite(frame["change_pct"].dropna()).all()


def test_yoy_uses_calendar_year_not_row_shift(date_strings_fixture):
    """Квартальный ряд: год назад — это 4 строки назад, а не 12."""
    dates = date_strings_fixture("2020-01", 6, freq="Q")
    series = pd.DataFrame({
        "period_start": dates,
        "value_mln_rub": [10.0, 20.0, 30.0, 40.0, 50.0, 60.0],
    })
    frame = macro_dynamics(series)
    assert pd.isna(frame["yoy_pct"].iloc[:4]).all()
    assert frame["yoy_pct"].iloc[4] == pytest.approx(400.0)   # (50 − 10) / 10
    assert frame["yoy_pct"].iloc[5] == pytest.approx(200.0)   # (60 − 20) / 20


def test_yoy_is_skipped_for_daily_series(date_strings_daily_fixture):
    """YoY не строится там, где шаг ряда не месяц/квартал/полугодие."""
    dates = date_strings_daily_fixture("2024-01-01", 20)
    series = pd.DataFrame({
        "period_start": dates,
        "value_mln_rub": [1.0] * 20,
    })
    assert "yoy_pct" not in macro_dynamics(series).columns


def test_report_and_dashboard_share_the_same_anomaly_math(tmp_path, date_strings_fixture):
    """Одна реализация робастного Z: отчёт и дашборд не должны
    расходиться на одном и том же ряду."""
    import json
    import re

    from finanalyst.visuals import build_dashboard

    # MAD = 0 (десять единиц и выброс) — раньше отчёт считал по σ, дашборд давал нули.
    dates = date_strings_fixture("2024-01", 11)
    series = pd.DataFrame({
        "period_start": dates,
        "value": [1.0] * 10 + [1000.0],
    })
    report = macro_anomalies(series, value_col="value", group_cols=())
    path = tmp_path / "dashboard.html"
    build_dashboard(series.assign(industry="X", indicator="I"), "I", path)
    payload = json.loads(re.search(r"const DATA = (\{.*?\});\n",
                                   path.read_text(encoding="utf-8"), re.S).group(1))

    assert len(report) == 1
    assert len(payload["anomalies"]) == 1
    assert max(abs(value) for value in payload["zseries"]["X"]) == pytest.approx(
        round(float(report["_z"].abs().max()), 3))