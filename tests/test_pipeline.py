import json

import pandas as pd
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


def test_macro_tables_are_analyzed(tmp_path):
    data = tmp_path / "macro"
    data.mkdir()
    periods = pd.date_range("2023-01-01", periods=18, freq="MS")
    deposits = pd.DataFrame({
        "period": [p.strftime("%Y-%m") for p in periods for _ in (0, 1)],
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
