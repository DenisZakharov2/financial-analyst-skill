"""Smoke-тесты модулей v2.2.0: regulatory, BI-экспорт, memory-efficient.

Тесты не подменяют интеграционные прогоны CLI, а фиксируют контракты новых
модулей: набор регуляторных форм, сохранение файлов, плоские BI-таблицы и
инкрементальные агрегаторы, которые должны совпадать с полным проходом.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd
import pytest
from finanalyst.bi_integration import (
    BIExportConfig,
    export_metrics_to_bi,
    export_to_files,
    run_bi_export,
)
from finanalyst.core import load_tables_streaming
from finanalyst.memory_efficient import (
    ClientsAggregator,
    FinanceAggregator,
    FunnelAggregator,
    PortfolioAggregator,
    estimate_memory_usage,
    resolve_source_tables,
    run_streaming_pipeline,
    update_aggregators,
)
from finanalyst.regulatory import (
    RegulatoryConfig,
    generate_regulatory_reports,
    save_regulatory_reports,
)

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "skills" / "financial-analyst" / "assets" / "sample"


@pytest.fixture(scope="module")
def sample_tables() -> dict[str, pd.DataFrame]:
    return {
        path.stem: pd.read_csv(path)
        for path in sorted(SAMPLE.glob("*.csv"))
    }


# ---------------------------------------------------------------------------
# Регуляторные отчёты
# ---------------------------------------------------------------------------

def test_regulatory_reports_include_all_forms(sample_tables):
    reports = generate_regulatory_reports(sample_tables, RegulatoryConfig())

    assert set(reports) == {"cof_101", "cof_102", "nbki", "basel_pillar3", "_meta"}
    assert reports["cof_101"]["form_code"] == "101"
    assert reports["cof_102"]["form_code"] == "102"
    assert reports["nbki"]["form_code"] == "NBKI_135FZ"
    assert reports["basel_pillar3"]["form_code"] == "BASEL_PILLAR3"
    assert reports["_meta"]["included_reports"] == [
        "cof_101", "cof_102", "nbki", "basel_pillar3",
    ]
    # Ключевые секции формы 101 не пустые
    assert reports["cof_101"]["sections"]["assets"]
    assert reports["cof_101"]["sections"]["liabilities"]


def test_regulatory_reports_save_json_and_xlsx(tmp_path, sample_tables):
    reports = generate_regulatory_reports(sample_tables, RegulatoryConfig())
    saved = save_regulatory_reports(reports, tmp_path / "regulatory", formats=["json", "xlsx"])

    assert "cof_101_json" in saved and saved["cof_101_json"].exists()
    assert "cof_101_xlsx" in saved and saved["cof_101_xlsx"].exists()
    assert "basel_pillar3_xlsx" in saved and saved["basel_pillar3_xlsx"].exists()

    payload = json.loads(saved["cof_101_json"].read_text(encoding="utf-8"))
    assert payload["form_code"] == "101"
    assert "sections" in payload


def test_regulatory_reports_handle_empty_tables(tmp_path):
    reports = generate_regulatory_reports({})
    assert reports["_meta"]["included_reports"]
    for key in ("cof_101", "cof_102", "nbki", "basel_pillar3"):
        assert reports[key]["available"] is False
        assert "error" in reports[key]

    saved = save_regulatory_reports(reports, tmp_path, formats=["json", "xlsx"])
    assert list(saved) == ["meta_json"]


# ---------------------------------------------------------------------------
# BI-экспорт
# ---------------------------------------------------------------------------

def _metrics_payload() -> dict:
    return {
        "headline": [
            {"label": "Total Balance", "value": "16.97M", "raw": 16969410, "unit": "руб"},
        ],
        "sections": {
            "portfolio": {
                "rows": [{"metric": "total_balance", "value": 16969410}],
                "summary": {"total_accounts": 120, "total_balance": 16969410},
            },
            "scenarios": {
                "summary": {
                    "base": {"par30": 0.0},
                    "stress_rate_up": {"par30": 1.5},
                },
            },
        },
        "quality": {
            "checks": [{"check_id": "pk_unique", "status": "pass"}],
        },
        "assumptions": [{"id": "a1", "text": "Тестовое допущение"}],
    }


def test_bi_export_metrics_to_files(tmp_path):
    config = BIExportConfig(
        output_dir=tmp_path / "bi",
        file_formats=["csv", "jsonl"],
        clean_output_dir=True,
    )
    exported = export_metrics_to_bi(_metrics_payload(), config)

    assert exported
    for path in exported.values():
        assert path.exists() and path.stat().st_size > 0

    # summary секции с числовыми значениями не должен падать и не должен
    # превращаться в «сценарную» таблицу.
    assert "csv_summary_portfolio" in exported
    assert "csv_scenario_summary_scenarios" in exported
    assert "csv_scenario_summary_portfolio" not in exported

    headline = pd.read_csv(exported["csv_headline"])
    assert headline.loc[0, "label"] == "Total Balance"
    assert headline.loc[0, "raw"] == 16969410


def test_bi_export_to_files_accepts_explicit_tables(tmp_path):
    tables = {
        "portfolio": pd.DataFrame({"account_id": ["A1", "A2"], "balance": [10.0, 20.0]}),
    }
    config = BIExportConfig(
        output_dir=tmp_path / "bi",
        file_formats=["csv"],
        clean_output_dir=True,
    )
    exported = export_to_files(tables, config)
    assert exported["csv_portfolio"].exists()
    frame = pd.read_csv(exported["csv_portfolio"])
    assert frame["balance"].sum() == 30.0


def test_bi_run_export_returns_file_paths(tmp_path):
    result = run_bi_export(
        _metrics_payload(),
        output_dir=tmp_path / "bi",
        formats=["csv"],
    )
    assert result["files"]
    assert all(Path(path).exists() for path in result["files"].values())
    assert result["dwh"] == {}


# ---------------------------------------------------------------------------
# Memory-efficient режим
# ---------------------------------------------------------------------------

def test_portfolio_aggregator_matches_full_sum(sample_tables):
    portfolio = sample_tables["portfolio"]
    aggregator = PortfolioAggregator()
    aggregator.update(portfolio.iloc[:60])
    aggregator.update(portfolio.iloc[60:])
    result = aggregator.finalize()

    assert result["total_rows"] == len(portfolio)
    assert result["total_balance"] == pytest.approx(portfolio["balance"].sum())
    assert result["total_limit"] == pytest.approx(portfolio["limit_amount"].sum())


def test_finance_aggregator_matches_full_sum(sample_tables):
    finance = sample_tables["finance"]
    aggregator = FinanceAggregator()
    aggregator.update(finance)
    result = aggregator.finalize()

    expected_income = (finance["interest_income"] + finance["fee_income"]).sum()
    assert result["total_revenue"] == pytest.approx(expected_income)
    assert result["period_count"] == finance["period"].nunique()


def test_funnel_aggregator_merge_matches_single_update(sample_tables):
    events = sample_tables["funnel_events"]
    whole = FunnelAggregator()
    whole.update(events)

    left = FunnelAggregator()
    left.update(events.iloc[:100])
    right = FunnelAggregator()
    right.update(events.iloc[100:])
    left.merge(right)

    whole_counts = {row["stage"]: row["count"] for row in whole.finalize()["stages"]}
    merged_counts = {row["stage"]: row["count"] for row in left.finalize()["stages"]}
    assert merged_counts == whole_counts
    assert left.finalize()["total_start"] == whole.finalize()["total_start"] > 0


def test_alias_sources_are_not_double_counted(tmp_path):
    events = pd.DataFrame({"stage": ["лид", "обращение"], "product_code": ["P1", "P1"]})
    events.to_csv(tmp_path / "funnel_events.csv", index=False)
    events.to_csv(tmp_path / "sales.csv", index=False)

    portfolio = pd.DataFrame({"balance": [10.0, 20.0]})
    portfolio.to_csv(tmp_path / "portfolio.csv", index=False)
    portfolio.to_csv(tmp_path / "accounts.csv", index=False)

    sources = resolve_source_tables(tmp_path)
    assert sources == {"portfolio": "portfolio", "funnel": "funnel_events"}

    funnel = FunnelAggregator()
    update_aggregators(
        {"funnel": funnel},
        {"funnel_events": events, "sales": events},
        sources,
    )
    assert funnel.finalize()["total_start"] == 1

    portfolio_agg = PortfolioAggregator()
    update_aggregators(
        {"portfolio": portfolio_agg},
        {"portfolio": portfolio, "accounts": portfolio},
        sources,
    )
    assert portfolio_agg.finalize()["total_rows"] == 2


def test_clients_aggregator_counts_rows(sample_tables):
    clients = sample_tables["clients"]
    aggregator = ClientsAggregator()
    aggregator.update(clients)
    result = aggregator.finalize()
    assert result["total_clients"] == len(clients)
    assert sum(result["segments"].values()) == len(clients)


def test_streaming_pipeline_uses_funnel_events(sample_tables):
    result = run_streaming_pipeline(
        SAMPLE,
        chunksize=25,
        n_workers=1,
        partition_by_time=False,
        enable_forecast=False,
    )

    funnel = result["base_aggregations"]["funnel"]
    expected = sample_tables["funnel_events"]["stage"].value_counts().to_dict()
    actual = {row["stage"]: row["count"] for row in funnel["stages"]}
    # sales.csv — синоним funnel_events, его нельзя суммировать повторно.
    assert actual == expected
    assert funnel["total_start"] == expected["лид"]
    assert [row["stage"] for row in funnel["stages"]][:2] == ["лид", "обращение"]
    assert result["stats"]["total_chunks"] > 0
    assert result["stats"]["total_rows"] == len(sample_tables["portfolio"])
    assert result["metrics"]["funnel"]["total_start"] == funnel["total_start"]
    # CLI сохраняет результат в metrics.json: ключи-кортежи недопустимы.
    json.dumps(result, ensure_ascii=False)


@pytest.mark.skipif(sys.platform != "linux", reason="ProcessPool-путь проверяется на Linux")
def test_streaming_pipeline_with_processes(sample_tables):
    result = run_streaming_pipeline(
        SAMPLE, chunksize=50, n_workers=2, use_processes=True,
        partition_by_time=False, enable_forecast=False,
    )
    assert result["base_aggregations"]["portfolio"]["total_rows"] == len(
        sample_tables["portfolio"]
    )
    assert result["base_aggregations"]["funnel"]["total_start"] == (
        sample_tables["funnel_events"]["stage"].value_counts()["лид"]
    )


def test_streaming_cli_writes_metrics_json(tmp_path):
    from finanalyst.cli import main

    out = tmp_path / "out"
    assert main([
        "--data-dir", str(SAMPLE), "--out", str(out),
        "--format", "xlsx", "--quiet",
        "--streaming", "--chunksize", "50", "--mem-workers", "1",
    ]) == 0

    payload = json.loads((out / "metrics.json").read_text(encoding="utf-8"))
    streaming = payload["sections"]["streaming"]
    assert streaming["enabled"] is True
    assert streaming["stats"]["total_rows"] > 0
    assert streaming["base_aggregations"]["funnel"]["total_start"] > 0


def test_streaming_partitioning_does_not_duplicate_rows(sample_tables):
    base = run_streaming_pipeline(
        SAMPLE, chunksize=25, n_workers=1,
        partition_by_time=False, partition_by_product=False,
        enable_forecast=False,
    )
    partitioned = run_streaming_pipeline(
        SAMPLE, chunksize=25, n_workers=1,
        partition_by_time=True, partition_by_product=True,
        enable_forecast=False,
    )

    for result in (base, partitioned):
        portfolio = result["base_aggregations"]["portfolio"]
        assert portfolio["total_rows"] == len(sample_tables["portfolio"])
        assert portfolio["total_balance"] == pytest.approx(
            sample_tables["portfolio"]["balance"].sum()
        )
        assert result["base_aggregations"]["clients"]["total_clients"] == len(
            sample_tables["clients"]
        )

    assert (
        partitioned["base_aggregations"]["funnel"]["total_start"]
        == base["base_aggregations"]["funnel"]["total_start"]
    )


def test_estimate_memory_usage_returns_plan(sample_tables):
    plan = estimate_memory_usage(SAMPLE, chunksize=1000)
    assert plan["total_files"] >= 1
    assert plan["estimated_rows"] > 0
    assert plan["chunksize"] == 1000
    assert plan["estimated_chunk_memory_mb"] > 0


def test_load_tables_streaming_yields_excel_once(tmp_path):
    pd.DataFrame({
        "client_id": ["C1", "C2"],
        "segment": ["mass", "mass"],
    }).to_excel(tmp_path / "clients.xlsx", index=False)
    pd.DataFrame({
        "period": ["2024-01", "2024-02"],
        "product_code": ["P1", "P1"],
        "interest_income": [10.0, 20.0],
        "fee_income": [1.0, 2.0],
    }).to_csv(tmp_path / "finance.csv", index=False)

    chunks = list(load_tables_streaming(tmp_path, chunksize=1))

    # Excel-таблица не должна повторяться в каждом чанке.
    assert sum("clients" in chunk for chunk in chunks) == 1
    assert sum(len(chunk.get("finance", [])) for chunk in chunks) == 2
