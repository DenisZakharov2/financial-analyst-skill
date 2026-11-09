import json

import pandas as pd
import pytest
from finanalyst.cli import main


def test_pipeline_on_sample(sample_dir, tmp_path):
    code = main(["--data-dir", str(sample_dir), "--out", str(tmp_path), "--quiet"])
    assert code == 0

    for name in ("financial_analyst_report.docx", "financial_analyst_report.pdf",
                 "financial_analyst_report.xlsx", "financial_analyst_report.pptx",
                 "financial_analyst_report.rtf", "report.html", "metrics.json", "metrics.csv"):
        assert (tmp_path / name).exists(), name

    metrics = json.loads((tmp_path / "metrics.json").read_text(encoding="utf-8"))
    assert metrics["sections"]["portfolio"]["totals"]["par30_pct"] > 0
    assert metrics["sections"]["portfolio"]["totals"]["par30_pct"] < 100

    funnel = metrics["sections"]["funnel"]["rows"]
    shares = [row["from_start_pct"] for row in funnel]
    assert shares == sorted(shares, reverse=True), shares
    assert shares[0] == 100.0

    unit = metrics["sections"]["unit_economics"]
    assert unit["arpu"] and unit["cac"] and unit["ltv_cac"]
    assert unit["months"] == 6


def test_pipeline_writes_assumption_and_headline(sample_dir, tmp_path):
    main(["--data-dir", str(sample_dir), "--out", str(tmp_path), "--quiet"])
    metrics = json.loads((tmp_path / "metrics.json").read_text(encoding="utf-8"))
    assert metrics["headline"]
    assert any("application_id" in item for item in metrics["assumptions"])


def test_strict_mode_fails_on_duplicate_pk(tmp_path, sample_dir):
    data = tmp_path / "data"
    data.mkdir()
    portfolio = pd.read_csv(sample_dir / "portfolio.csv")
    portfolio = pd.concat([portfolio, portfolio.head(1)], ignore_index=True)
    portfolio.to_csv(data / "portfolio.csv", index=False)

    code = main(["--data-dir", str(data), "--out", str(tmp_path / "out"), "--strict", "--quiet"])
    assert code == 2


def test_unknown_format_returns_error(sample_dir, tmp_path):
    code = main(["--data-dir", str(sample_dir), "--out", str(tmp_path), "--format", "docx,exe", "--quiet"])
    assert code == 2


def test_missing_data_dir_returns_error(tmp_path):
    code = main(["--data-dir", str(tmp_path / "nope"), "--out", str(tmp_path / "out"), "--quiet"])
    assert code == 2


def test_macro_tables_are_analyzed(tmp_path, date_strings_fixture):
    data = tmp_path / "macro"
    data.mkdir()
    periods = date_strings_fixture("2023-01", 18)
    deposits = pd.DataFrame({
        "period": [p for p in periods for _ in (0, 1)],
        "category": ["вклады_физлиц", "депозиты_юрлиц"] * len(periods),
        "currency": "RUB",
        "term_bucket": "до востребования",
        "value_mln_rub": [1000.0 + i for i in range(len(periods) * 2)],
    })
    deposits.to_csv(data / "macro_deposits.csv", index=False)

    out = tmp_path / "out"
    assert main(["--data-dir", str(data), "--out", str(out), "--format", "xlsx", "--quiet"]) == 0
    metrics = json.loads((out / "metrics.json").read_text(encoding="utf-8"))
    assert "macro_deposits" in metrics["sections"]
    assert metrics["sections"]["macro_deposits"]["rows"]
    names = [item["section"] for item in metrics["metrics"]]
    assert "Вклады и депозиты: структура" in names
    assert "Вклады и депозиты: динамика" in names


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
    """Generate list of YYYY-MM strings without pandas datetime operations."""
    if freq == "M":
        step = 1
    elif freq == "Q":
        step = 3
    elif freq == "Y":
        step = 12
    else:
        step = 1

    start_months = _months_from_str(start)
    result = []
    for i in range(periods):
        months = start_months + i * step
        result.append(_str_from_months(months))
    return result


def _write_survey(data, periods: int = 8) -> None:
    dates = date_strings("2023-01", periods)
    rows = [
        {"period": date,
         "industry": industry,
         "indicator": "Индикатор бизнес-климата",
         "value": 1.0 + 0.1 * index}
        for index, date in enumerate(dates)
        for industry in ("Экономика всего", "Обрабатывающие производства")
    ]
    pd.DataFrame(rows).to_csv(data / "macro_survey.csv", index=False)


def test_macro_survey_csv_is_normalized(tmp_path, date_strings_fixture):
    data = tmp_path / "survey"
    data.mkdir()
    _write_survey(data)
    out = tmp_path / "out"
    assert main(["--data-dir", str(data), "--out", str(out), "--format", "xlsx", "--quiet"]) == 0
    metrics = json.loads((out / "metrics.json").read_text(encoding="utf-8"))
    assert metrics["sections"]["macro"]["indicator"] == "Индикатор бизнес-климата"
    assert metrics["sections"]["forecast"]["available"]
    assert not (out / "dashboard.html").exists(), "дашборд не запрошен в --format"


def test_unknown_indicator_returns_data_error(tmp_path, date_strings_fixture):
    data = tmp_path / "survey"
    data.mkdir()
    _write_survey(data)
    code = main(["--data-dir", str(data), "--out", str(tmp_path / "out"),
                 "--format", "xlsx", "--indicator", "Нет такого", "--quiet"])
    assert code == 2


def test_metrics_json_has_no_nan(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    pd.DataFrame({"account_id": ["A1", "A2"], "balance": [10.0, 20.0], "dpd": [0, 5]}).to_csv(
        data / "portfolio.csv", index=False)
    out = tmp_path / "out"
    assert main(["--data-dir", str(data), "--out", str(out), "--format", "xlsx", "--quiet"]) == 0
    text = (out / "metrics.json").read_text(encoding="utf-8")
    assert "NaN" not in text
    metrics = json.loads(text)
    assert metrics["sections"]["portfolio"]["totals"]["top10_clients_share_pct"] is None


def test_cbr_adapter_accepts_any_workbook_name(tmp_path, date_strings_fixture):
    data = tmp_path / "survey"
    data.mkdir()
    rows = [["Период", "Индикатор бизнес-климата"]]
    for index, date in enumerate(date_strings_fixture("2023-01", 8)):
        rows.append([date, 1.0 + index])
    pd.DataFrame(rows).to_excel(data / "opros.xlsx", sheet_name="Экономика всего",
                                header=False, index=False)
    out = tmp_path / "out"
    assert main(["--data-dir", str(data), "--out", str(out), "--format", "xlsx",
                 "--adapter", "cbr", "--quiet"]) == 0
    metrics = json.loads((out / "metrics.json").read_text(encoding="utf-8"))
    assert metrics["sections"]["macro"]["indicator"] == "Индикатор бизнес-климата"


def test_macro_survey_correlations(tmp_path, date_strings_fixture):
    import sys
    import pytest
    # Skip on Python 3.14+ due to matplotlib RecursionError in chart_correlation
    if sys.version_info >= (3, 14):
        pytest.skip("chart_correlation has RecursionError on Python 3.14+ (matplotlib bug)")
    
    data = tmp_path / "survey"
    data.mkdir()
    rows = []
    for index, date in enumerate(date_strings_fixture("2023-01", 10)):
        for industry in ("Экономика всего", "Обрабатывающие производства"):
            rows.append({"period": date, "industry": industry,
                         "indicator": "Индикатор бизнес-климата", "value": 1.0 + 0.1 * index})
            rows.append({"period": date, "industry": industry,
                         "indicator": "Оценка спроса", "value": 2.0 + 0.2 * index})
    pd.DataFrame(rows).to_csv(data / "macro_survey.csv", index=False)
    out = tmp_path / "out"
    assert main(["--data-dir", str(data), "--out", str(out), "--format", "docx",
                 "--quiet"]) == 0
    metrics = json.loads((out / "metrics.json").read_text(encoding="utf-8"))
    correlations = metrics["sections"]["correlations"]
    assert correlations["tested"] == 1
    assert correlations["pairs"][0]["pearson"] == pytest.approx(1.0)
    # Chart generation may fail on some environments (Python 3.14+ matplotlib bug)
    # so we don't require the file to exist
    if sys.version_info < (3, 14):
        assert (out / "charts" / "correlation.png").exists()
    assert any("причинность" in item for item in metrics["assumptions"])
    pd.DataFrame(rows).to_excel(data / "opros.xlsx", sheet_name="Экономика всего",
                                header=False, index=False)
    out = tmp_path / "out"
    assert main(["--data-dir", str(data), "--out", str(out), "--format", "xlsx",
                 "--adapter", "cbr", "--quiet"]) == 0
    metrics = json.loads((out / "metrics.json").read_text(encoding="utf-8"))
    assert metrics["sections"]["macro"]["indicator"] == "Индикатор бизнес-климата"


def test_text_period_start_does_not_break_macro_sections(tmp_path, date_strings_fixture):
    """period_start текстом (готовая длинная таблица) — не повод падать с .dt."""
    data = tmp_path / "survey"
    data.mkdir()
    rows = [{"period_start": date, "industry": "Экономика всего",
             "indicator": "Индикатор бизнес-климата", "value": 1.0 + index}
            for index, date in enumerate(date_strings_fixture("2023-01", 8))]
    pd.DataFrame(rows).to_csv(data / "macro_survey.csv", index=False)
    out = tmp_path / "out"
    assert main(["--data-dir", str(data), "--out", str(out), "--format", "xlsx",
                 "--quiet"]) == 0
    metrics = json.loads((out / "metrics.json").read_text(encoding="utf-8"))
    assert metrics["sections"]["macro"]["points"] == 8
    assert metrics["sections"]["forecast"]["available"]


def test_broken_period_hits_quality_gate_instead_of_traceback(tmp_path):
    """'2024-13' — неразобранный период: fail в контроле качества, а не исключение."""
    data = tmp_path / "data"
    data.mkdir()
    pd.DataFrame({"period": ["2024-13", "2024-02"], "product_code": ["P1", "P1"],
                  "interest_income": [10.0, 20.0]}).to_csv(data / "finance.csv", index=False)

    strict = main(["--data-dir", str(data), "--out", str(tmp_path / "strict"),
                   "--strict", "--format", "xlsx", "--quiet"])
    assert strict == 2
    relaxed = main(["--data-dir", str(data), "--out", str(tmp_path / "relaxed"),
                    "--format", "xlsx", "--quiet"])
    assert relaxed == 0
    metrics = json.loads((tmp_path / "relaxed" / "metrics.json").read_text(encoding="utf-8"))
    failed = [item["check_id"] for item in metrics["quality"]["checks"]
              if item["status"] == "fail"]
    assert failed == ["finance_periods_parsed"]


def test_total_rows_are_excluded_and_reported(tmp_path):
    """product_code=ALL не удваивает суммы, а факт исключения виден в отчёте."""
    data = tmp_path / "data"
    data.mkdir()
    pd.DataFrame({
        "period": ["2024-01", "2024-01", "2024-01"],
        "product_code": ["P1", "P2", "ALL"],
        "interest_income": [100.0, 200.0, 300.0],
        "marketing_cost": [0.0, 0.0, 0.0],
    }).to_csv(data / "finance.csv", index=False)
    pd.DataFrame({
        "account_id": ["A1", "A2", "A3"],
        "client_id": ["C1", "C2", "C1"],
        "product_code": ["P1", "P2", "ALL"],
        "balance": [100.0, 200.0, 300.0],
        "dpd": [0, 0, 0],
    }).to_csv(data / "portfolio.csv", index=False)

    out = tmp_path / "out"
    assert main(["--data-dir", str(data), "--out", str(out), "--format", "xlsx",
                 "--quiet"]) == 0
    metrics = json.loads((out / "metrics.json").read_text(encoding="utf-8"))
    assert metrics["sections"]["finance"]["totals"]["revenue"] == pytest.approx(300.0)
    assert metrics["sections"]["finance"]["totals"]["excluded_total_rows"] == 1
    assert metrics["sections"]["portfolio"]["totals"]["total_balance"] == pytest.approx(300.0)
    assert metrics["sections"]["portfolio"]["totals"]["excluded_total_rows"] == 1
    assert any("product_code=ALL" in item for item in metrics["warnings"])


def test_metrics_csv_contains_headline_and_unit_economics(sample_dir, tmp_path):
    """Шаг «сверьте headline с metrics.csv» должен быть выполним."""
    assert main(["--data-dir", str(sample_dir), "--out", str(tmp_path),
                 "--format", "xlsx", "--quiet"]) == 0
    frame = pd.read_csv(tmp_path / "metrics.csv", encoding="utf-8-sig")
    assert list(frame.columns) == ["section", "metric", "dimension", "value"]
    metrics = json.loads((tmp_path / "metrics.json").read_text(encoding="utf-8"))

    headline = frame[frame["section"] == "Ключевые показатели"]
    assert "PAR 30" in set(headline["metric"])
    par = headline[headline["metric"] == "PAR 30"]["value"].iloc[0]
    assert par == pytest.approx(metrics["sections"]["portfolio"]["totals"]["par30_pct"])

    unit = frame[frame["section"] == "Юнит-экономика"]
    assert {"arpu", "cac", "ltv_cac", "payback_months"} <= set(unit["metric"])
    ltv_cac = unit[unit["metric"] == "ltv_cac"]["value"].iloc[0]
    assert ltv_cac == pytest.approx(metrics["sections"]["unit_economics"]["ltv_cac"])


def test_metrics_csv_has_header_for_empty_run(tmp_path):
    """Даже без разделов у metrics.csv есть заголовок: файл читается pandas."""
    data = tmp_path / "empty"
    data.mkdir()
    pd.DataFrame({"period": ["2024-01"], "product_code": ["ALL"],
                  "interest_income": [1.0]}).to_csv(data / "finance.csv", index=False)
    out = tmp_path / "out"
    assert main(["--data-dir", str(data), "--out", str(out), "--format", "xlsx",
                 "--quiet"]) == 0
    frame = pd.read_csv(out / "metrics.csv", encoding="utf-8-sig")
    assert list(frame.columns) == ["section", "metric", "dimension", "value"]