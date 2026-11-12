"""Регресс-тесты по итогам независимого аудита репозитория.

Каждый тест закрывает конкретное подтверждённое замечание аудита:
в отчёте они помечены как BUG-xx / TEST-xx / DOC-xx.
"""

import numpy as np
import pandas as pd
import pytest
from finanalyst import experiments, pii
from finanalyst.adapters import cbr_survey, client_csv
from finanalyst.comparison import compare_snapshots, portfolio_bridge
from finanalyst.core import (
    KNOWN_TABLES,
    TOTAL_CODES,
    canonical_period,
    drop_total_rows,
    load_tables,
    normalize_periods,
    parse_period,
    period_grain,
    period_labels,
    period_ym,
    validate_inputs,
)
from finanalyst.metrics import forecast_trend
from finanalyst.seasonality_tat import funnel_tat

# ---------------------------------------------------------------------------
# BUG-03: синоним таблицы не должен вытеснять каноническую
# ---------------------------------------------------------------------------

def test_alias_table_does_not_shadow_canonical_table():
    """accounts.csv не должен затирать portfolio.csv (порядок чтения — по алфавиту)."""
    tables = {
        "accounts": pd.DataFrame({"account_id": ["A1"], "balance": [100.0]}),
        "portfolio": pd.DataFrame({"account_id": ["P1"], "balance": [500.0]}),
    }
    normalized, notes = client_csv.normalize(tables)
    assert list(normalized) == ["portfolio"]
    assert normalized["portfolio"]["account_id"].tolist() == ["P1"]
    assert any("каноническая" in note for note in notes)


def test_alias_applications_does_not_shadow_sales():
    tables = {
        "applications": pd.DataFrame({"application_id": ["A1"]}),
        "sales": pd.DataFrame({"application_id": ["S1"]}),
    }
    normalized, _ = client_csv.normalize(tables)
    assert normalized["sales"]["application_id"].tolist() == ["S1"]


def test_funnel_alias_is_known_table():
    """Таблица funnel — допустимое имя, иначе она попадает в unknown_tables."""
    assert "funnel" in KNOWN_TABLES
    report = validate_inputs({"funnel": pd.DataFrame({"stage": ["лид"], "event_date": ["2024-01-01"]})})
    assert not any(check.check_id == "unknown_tables" for check in report.checks)


# ---------------------------------------------------------------------------
# BUG-09: кодировки и одноколоночные CSV
# ---------------------------------------------------------------------------

def test_csv_in_cp1251_is_read(tmp_path):
    path = tmp_path / "clients.csv"
    path.write_text("client_id,region\nC1,Москва\nC2,Казань\n", encoding="cp1251")
    tables = load_tables(tmp_path)
    assert tables["clients"]["region"].tolist() == ["Москва", "Казань"]


def test_single_column_csv_is_not_corrupted(tmp_path):
    """Раньше sep=None «находил» в одноколоночном файле выдуманный разделитель."""
    path = tmp_path / "macro_survey.csv"
    path.write_text("value\n100\n200\n", encoding="utf-8")
    tables = load_tables(tmp_path)
    frame = tables["macro_survey"]
    assert list(frame.columns) == ["value"]
    assert frame["value"].tolist() == [100, 200]


def test_semicolon_separator_is_detected(tmp_path):
    path = tmp_path / "clients.csv"
    path.write_text("client_id;region\nC1;Москва\n", encoding="utf-8")
    tables = load_tables(tmp_path)
    assert list(tables["clients"].columns) == ["client_id", "region"]


# ---------------------------------------------------------------------------
# BUG-04: периоды
# ---------------------------------------------------------------------------

def test_daily_finance_rows_are_not_duplicates():
    """Дневная детализация — это разные строки, а не дубли одного месяца."""
    finance = pd.DataFrame({
        "period": ["2024-05-01", "2024-05-02", "2024-05-03"],
        "product_code": ["CRD", "CRD", "CRD"],
        "interest_income": [100.0, 200.0, 300.0],
    })
    report = validate_inputs({"finance": finance})
    check = next(item for item in report.checks if item.check_id == "finance_grain_unique")
    assert check.status == "pass"


def test_monthly_finance_duplicates_still_fail():
    finance = pd.DataFrame({
        "period": ["2024-05", "2024-05"],
        "product_code": ["CRD", "CRD"],
        "interest_income": [100.0, 200.0],
    })
    report = validate_inputs({"finance": finance})
    check = next(item for item in report.checks if item.check_id == "finance_grain_unique")
    assert check.status == "fail"


def test_string_and_timestamp_first_of_month_agree():
    """«2024-05-01» и Timestamp('2024-05-01') — один и тот же тип периода."""
    assert parse_period("2024-05-01")[1] == parse_period(pd.Timestamp("2024-05-01"))[1]


def test_period_grain_detects_daily_detail():
    assert period_grain(["2024-05-01", "2024-05-02"]) == "D"
    assert period_grain(["2024-05", "2024-06"]) == "M"
    assert period_grain(["2024-05-01", "2024-06-01"]) == "M"


def test_canonical_period_keeps_day_at_daily_grain():
    assert canonical_period("2024-05-15", "D") == "2024-05-15"
    assert canonical_period("2024-05-15", "M") == "2024-05"
    assert period_ym("2024-05-15") == "2024-05"


def test_normalize_periods_keeps_month_bucket_and_full_date():
    frame = pd.DataFrame({"period": ["2024-05-15", "2024-05-01"], "value": [1, 2]})
    result = normalize_periods(frame)
    assert result["period_ym"].tolist() == ["2024-05", "2024-05"]
    assert result["period_full"].tolist() == ["2024-05-01", "2024-05-15"]
    assert result["period_type"].tolist() == ["D", "D"]


def test_months_ru_accepts_short_names():
    assert parse_period("янв 2024")[0].month == 1
    assert parse_period("фев 2024")[0].month == 2
    assert parse_period("сен 2024")[0].month == 9
    assert parse_period("дек 2024")[0].month == 12


def test_period_labels_uses_format_argument():
    labels = period_labels(pd.Series(["2024-05-15"]), fmt="%Y-%m-%d")
    assert labels.tolist() == ["2024-05-15"]


# ---------------------------------------------------------------------------
# BUG-06: нулевая база в сравнении срезов
# ---------------------------------------------------------------------------

def test_delta_pct_is_none_when_base_is_zero():
    base = pd.DataFrame({"period": ["2024-01"], "interest_income": [0.0]})
    target = pd.DataFrame({"period": ["2024-01"], "interest_income": [500000.0]})
    result = compare_snapshots({"finance": base}, {"finance": target})
    frame = result["finance_comparison"]
    row = frame.iloc[0]
    assert row["delta_abs"] == 500000.0
    assert row["delta_pct"] is None or (isinstance(row["delta_pct"], float) and np.isnan(row["delta_pct"]))
    assert "нулевым" in str(row["delta_pct_note"])


def test_delta_pct_is_zero_when_both_sides_are_zero():
    base = pd.DataFrame({"period": ["2024-01"], "interest_income": [0.0]})
    target = pd.DataFrame({"period": ["2024-01"], "interest_income": [0.0]})
    result = compare_snapshots({"finance": base}, {"finance": target})
    assert result["finance_comparison"].iloc[0]["delta_pct"] == 0.0


# ---------------------------------------------------------------------------
# BUG-07: детекция ПДн
# ---------------------------------------------------------------------------

def test_sparse_pii_column_is_detected():
    """Email у 10 % клиентов — тоже ПДн: прежний порог 30 % их пропускал."""
    values = ["ivanov@example.com"] * 10 + ["нет данных"] * 90
    frame = pd.DataFrame({"contact_info": values})
    pii_type, matched = pii._detect_series_pii_type(frame["contact_info"], "contact_info")
    assert pii_type == "email"
    assert matched == 10


def test_technical_ids_are_not_treated_as_bank_cards():
    """16-значные номера проходят алгоритм Луна примерно в 10 % случаев."""
    rng = np.random.default_rng(20240101)
    digits = rng.integers(0, 10, size=(500, 16))
    values = ["".join(str(int(d)) for d in row) for row in digits]
    frame = pd.DataFrame({"deal_no": values})
    assert pii._detect_series_pii_type(frame["deal_no"], "deal_no") == (None, 0)


def test_snils_rejects_three_identical_digits():
    """Правило СФР: в первых девяти разрядах СНИЛС нет трёх цифр подряд."""
    assert pii.is_valid_snils("11111111145") is False
    assert pii.is_valid_snils("11223334445") is False


def test_snils_accepts_valid_number():
    assert pii.is_valid_snils("112-233-445 95") is True


# ---------------------------------------------------------------------------
# BUG-09: итоговые строки и регистр par_bucket
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("code", ["ALL", "TOTAL", "ИТОГО", "ВСЕГО"])
def test_total_codes_are_dropped(code):
    assert code in TOTAL_CODES
    frame = pd.DataFrame({
        "period": ["2024-01", "2024-01"],
        "product_code": ["CRD", code],
        "amount": [10.0, 10.0],
    })
    cleaned, excluded = drop_total_rows(frame, group=("period",))
    assert excluded == 1
    assert cleaned["product_code"].tolist() == ["CRD"]


def test_par_bucket_comparison_ignores_case():
    portfolio = pd.DataFrame({
        "account_id": ["A1"],
        "client_id": ["C1"],
        "balance": [10.0],
        "dpd": [95],
        "par_bucket": ["90+"],
    })
    report = validate_inputs({"portfolio": portfolio})
    check = next(item for item in report.checks if item.check_id == "portfolio_dpd_bucket_consistent")
    assert check.status == "pass"


# ---------------------------------------------------------------------------
# BUG-10: переходы воронки с пропущенным этапом
# ---------------------------------------------------------------------------

def test_skipped_stage_is_not_counted_as_adjacent_transition():
    """«лид -> одобрение» без «заявки» — это не переход одного этапа."""
    events = pd.DataFrame({
        "application_id": ["A1", "A1", "A2", "A2"],
        "stage": ["лид", "одобрение", "лид", "обращение"],
        "event_date": ["2024-01-01", "2024-01-05", "2024-01-01", "2024-01-02"],
    })
    result = funnel_tat(events)
    assert result["skipped_count"] == 1
    transitions = set(result["by_transition"]["transition"])
    assert "лид -> одобрение" not in transitions
    assert "лид -> обращение" in transitions
    assert not result["skipped_transitions"].empty
    assert result["skipped_transitions"].iloc[0]["transition"] == "лид -> одобрение"


def test_backward_transition_is_excluded_from_tat():
    events = pd.DataFrame({
        "application_id": ["A1", "A1"],
        "stage": ["заявка", "лид"],
        "event_date": ["2024-01-05", "2024-01-01"],
    })
    result = funnel_tat(events)
    assert result["backward_count"] == 0
    assert result["by_transition"].empty


# ---------------------------------------------------------------------------
# BUG-08: разбор книг ЦБ РФ
# ---------------------------------------------------------------------------

def test_cbr_survey_parses_comma_decimals():
    raw = pd.DataFrame([
        ["Период", "Показатель"],
        ["2024", "1 234,5"],
        ["2024", "14,8"],
    ])
    parsed = cbr_survey._parse_sheet(raw, "Лист", "stat.xlsx")
    assert not parsed.empty
    assert sorted(parsed["value"].tolist()) == [14.8, 1234.5]


def test_cbr_dedupe_avoids_name_collision():
    assert cbr_survey._dedupe(["Итого", "Итого", "Итого_1"]) == ["Итого", "Итого_1", "Итого_1_1"]


# ---------------------------------------------------------------------------
# TEST-01: календарные даты в фикстуре
# ---------------------------------------------------------------------------

def test_date_strings_daily_follows_calendar(date_strings_daily_fixture):
    dates = date_strings_daily_fixture("2024-01-28", 5)
    assert dates == ["2024-01-28", "2024-01-29", "2024-01-30", "2024-01-31", "2024-02-01"]


def test_date_strings_daily_has_no_gaps(date_strings_daily_fixture):
    dates = date_strings_daily_fixture("2024-01-01", 40)
    parsed = [pd.Timestamp(value) for value in dates]
    gaps = {(later - earlier).days for earlier, later in zip(parsed, parsed[1:])}
    assert gaps == {1}


# ---------------------------------------------------------------------------
# BUG-10: разбор дат
# ---------------------------------------------------------------------------

def test_vectorised_date_helpers_return_calendar_values():
    """Месяц и квартал — номера в году, а не индексы с эпохи."""
    from finanalyst.seasonality_tat import (
        I_NAT,
        _ns_to_datetime_str,
        _ns_to_month,
        _ns_to_quarter,
    )

    ns = np.array(["2024-01-15", "2024-05-20", "2024-12-31"], dtype="datetime64[ns]").astype("int64")
    assert _ns_to_month(ns).tolist() == [1, 5, 12]
    assert _ns_to_quarter(ns).tolist() == [1, 2, 4]
    assert _ns_to_datetime_str(ns).tolist() == ["2024-01-15", "2024-05-20", "2024-12-31"]
    assert _ns_to_month(np.array([I_NAT])).tolist() == [0]
    assert _ns_to_datetime_str(np.array([I_NAT])).tolist() == [""]


def test_parse_dates_to_ns_handles_iso_and_russian_formats():
    from finanalyst.seasonality_tat import I_NAT, _parse_dates_to_ns

    values = np.array(["2024-05-15", "2024-05", "15.05.2024", "май 2024", "нет данных"], dtype=object)
    ns = _parse_dates_to_ns(values)
    assert ns[0] != I_NAT
    assert ns[1] == ns[3], "«2024-05» и «май 2024» должны давать одну дату"
    assert ns[2] != I_NAT, "«15.05.2024» разбирается точечным парсером"
    assert ns[4] == I_NAT


# ---------------------------------------------------------------------------
# BUG-11: фигуры matplotlib закрываются
# ---------------------------------------------------------------------------

def test_charts_do_not_leave_open_figures(tmp_path):
    import matplotlib.pyplot as plt
    from finanalyst.visuals import chart_funnel, chart_portfolio

    before = len(plt.get_fignums())
    chart_portfolio(pd.DataFrame({"product_type": ["кредит"], "total_balance": [100.0]}), tmp_path / "p.png")
    chart_funnel(pd.DataFrame({"stage": ["лид"], "from_start_pct": [100.0]}), tmp_path / "f.png")
    assert len(plt.get_fignums()) == before


# ---------------------------------------------------------------------------
# BUG-12: отчёты не падают на отсутствующем графике
# ---------------------------------------------------------------------------

def test_report_with_missing_chart_is_still_written(tmp_path):
    from finanalyst.reporting import ReportData, write_pdf

    report = ReportData(
        title="Тест",
        sections=[("Пусто", pd.DataFrame(), "")],
        charts={"portfolio": tmp_path / "нет_такого.png"},
    )
    path = write_pdf(report, tmp_path / "report.pdf")
    assert path.exists() and path.stat().st_size > 0


# ---------------------------------------------------------------------------
# Ревизия 2.1.1: формулы, которые расходились с контрактом
# ---------------------------------------------------------------------------

def test_km_retention_is_recorded_before_current_hazard():
    """Первый этап воронки удерживает 100 %, даже если часть заявок на нём отваливается."""
    events = pd.DataFrame({
        "application_id": ["A1", "A1", "A2"],
        "stage": ["лид", "обращение", "лид"],
        "event_date": ["2024-01-01", "2024-01-03", "2024-01-01"],
    })
    curve = funnel_tat(events)["survival_curve"].set_index("stage")
    assert curve.loc["лид", "km_retention_pct"] == 100.0
    assert curve.loc["лид", "stage_hazard_pct"] == 50.0
    assert curve.loc["обращение", "km_retention_pct"] == 50.0


def test_return_to_earlier_stage_increments_backward_count():
    """Возврат — это более поздняя дата и более ранний этап, а не перепутанный порядок строк."""
    events = pd.DataFrame({
        "application_id": ["A1", "A1", "A1"],
        "stage": ["лид", "обращение", "лид"],
        "event_date": ["2024-01-01", "2024-01-02", "2024-01-05"],
    })
    result = funnel_tat(events)
    assert result["backward_count"] == 1
    transitions = set(result["by_transition"]["transition"])
    assert "лид -> обращение" in transitions
    assert "обращение -> лид" not in transitions


def test_forecast_daily_horizon_does_not_collapse_onto_one_date():
    dates = pd.date_range("2024-01-01", periods=10, freq="D").strftime("%Y-%m-%d")
    series = pd.DataFrame({"period_start": list(dates), "value": np.arange(1, 11, dtype=float)})
    result = forecast_trend(series, horizon=4)
    assert result["available"] is True
    assert result["periods"] == ["2024-01-11", "2024-01-12", "2024-01-13", "2024-01-14"]


def test_forecast_mape_is_null_when_backtest_actual_is_zero():
    series = pd.DataFrame({
        "period_start": ["2024-01", "2024-02", "2024-03", "2024-04",
                         "2024-05", "2024-06", "2024-07", "2024-08"],
        "value": [10.0, 12.0, 14.0, 16.0, 18.0, 20.0, 0.0, 24.0],
    })
    result = forecast_trend(series, horizon=2)
    assert result["available"] is True
    assert result["mape_pct"] is None


def test_zero_opening_book_does_not_report_zero_nrr():
    base = pd.DataFrame({"client_id": ["C1"], "balance": [0.0]})
    target = pd.DataFrame({"client_id": ["C2"], "balance": [100.0]})
    result = portfolio_bridge(base, target)
    assert result["net_dollar_retention_pct"] is None
    assert result["balance_growth_pct"] is None
    assert result["client_retention_pct"] is None
    assert result["bridge_table"]["contribution_to_growth_pct"].isna().all()


def test_lower_is_better_flips_positive_ab_effect_to_stop():
    rng = np.random.default_rng(7)
    n_clients = 200
    groups = ["control"] * n_clients + ["treatment"] * n_clients
    values = np.concatenate([
        rng.normal(100.0, 10.0, size=n_clients),
        rng.normal(112.0, 10.0, size=n_clients),
    ])
    frame = pd.DataFrame({
        "client_id": [f"C-{i}" for i in range(n_clients * 2)],
        "experiment_group": groups,
        "value": values,
    })
    better = experiments.run_ab_test(frame, higher_is_better=True)
    worse = experiments.run_ab_test(frame, higher_is_better=False)
    assert better["recommendation"] == "launch"
    assert worse["recommendation"] == "stop_degradation"
    assert "меньше лучше" in worse["assumption"]


def test_proportion_ci_stays_inside_unit_interval_when_wald_does_not():
    """9/10 против 1/10: интервал Вальда вылезает за 1, Ньюкомб — нет."""
    rows = []
    for i in range(10):
        rows.append({"client_id": f"C-{i}", "experiment_group": "control", "value": 1.0 if i == 0 else 0.0})
    for i in range(10):
        rows.append({"client_id": f"T-{i}", "experiment_group": "treatment", "value": 0.0 if i == 0 else 1.0})
    result = experiments.run_ab_test(pd.DataFrame(rows), metric_type="proportion")
    low, high = result["ci_95"]
    assert result["test_used"] == "two_proportion_z_test"
    assert -1.0 <= low <= high <= 1.0
    assert abs(result["absolute_effect"] - 0.8) < 1e-9


def test_year_and_months_do_not_double_count_the_window():
    from finanalyst.metrics import _months_in_periods

    periods = ["2024"] + [f"2024-{month:02d}" for month in range(1, 13)]
    assert _months_in_periods(pd.Series(periods)) == 12


def test_pvm_ignores_total_row():
    from finanalyst import drivers

    base = pd.DataFrame({
        "product_code": ["A", "B", "ALL"],
        "volume": [100.0, 100.0, 200.0],
        "revenue": [10.0, 30.0, 40.0],
    })
    target = base.copy()
    target["revenue"] = [10.0, 30.0, 80.0]
    result = drivers.pvm_decomposition(base, target)
    assert result["available"] is True
    assert abs(result["total_delta"]) < 0.01
    assert "ALL" not in set(result["by_segment"]["product_code"])


def test_skipped_stage_count_is_the_number_of_missing_stages():
    events = pd.DataFrame({
        "application_id": ["A1", "A1"],
        "stage": ["лид", "заявка"],
        "event_date": ["2024-01-01", "2024-01-04"],
    })
    skipped = funnel_tat(events)["skipped_transitions"]
    assert skipped.iloc[0]["transition"] == "лид -> заявка"
    assert skipped.iloc[0]["stages_skipped"] == 1


def test_bridge_keeps_negative_balances():
    base = pd.DataFrame({"client_id": ["C1", "C2"], "balance": [100.0, -50.0]})
    target = pd.DataFrame({"client_id": ["C1", "C2"], "balance": [80.0, -40.0]})
    result = portfolio_bridge(base, target)
    assert result["balance_base"] == 50.0
    assert result["balance_target"] == 40.0
    assert result["balance_delta"] == -10.0


def test_margin_word_is_not_parsed_as_march():
    ts, kind = parse_period("маржа 2024")
    assert pd.isna(ts) and kind == ""
    march, march_kind = parse_period("март 2024")
    assert march_kind == "M" and march.month == 3
    stamped, stamped_kind = parse_period("15.05.2024 13:45:00")
    assert stamped_kind == "D" and stamped.day == 15 and stamped.month == 5


def test_hmac_int_and_float_ids_match():
    left = pii._hmac_token(1, b"secret", "CL")
    right = pii._hmac_token(1.0, b"secret", "CL")
    assert left == right
    assert len(left.split("-", 1)[1]) == 16


def test_product_name_is_not_treated_as_fio():
    products = pd.DataFrame({
        "product_name": ["Карта Зарплатная Базовая", "Кредит Наличный Стандарт", "Вклад Срочный Премиум"],
    })
    assert pii.scan_pii({"products": products}) == []


def test_numeric_phone_column_is_detected():
    clients = pd.DataFrame({"msisdn": [79001234567, 79007654321, 79161112233]})
    findings = pii.scan_pii({"clients": clients})
    assert findings and findings[0].pii_type == "phone_ru"


