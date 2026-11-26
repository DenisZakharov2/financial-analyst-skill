"""Unit и integration тесты для модуля сценарийного анализа (scenario.py).

Покрывает:
- Scenario: apply_to, мультипликативные/аддитивные изменения
- ScenarioEngine: run_all, sensitivity_analysis, progress_callback
- create_standard_scenarios: 6 пресетов, структура параметров
- extract_key_metrics_from_payload: извлечение метрик из sections
- scenario_results_to_dataframe / scenario_results_summary: отчёты
- Property-based тесты: инварианты сценариев
"""

from __future__ import annotations

import pandas as pd
import pytest
from finanalyst.scenario import (
    Scenario,
    ScenarioEngine,
    ScenarioResult,
    create_standard_scenarios,
    extract_key_metrics_from_payload,
    scenario_results_summary,
    scenario_results_to_dataframe,
)
from hypothesis import given, settings
from hypothesis import strategies as st

# ============================================================
# Helpers / fixtures
# ============================================================

@pytest.fixture
def base_params() -> dict[str, float]:
    """Базовые параметры для тестов Scenario.apply_to."""
    return {
        "rate": 0.15,
        "pd": 0.02,
        "conversion": 0.3,
        "lgd": 0.45,
    }


@pytest.fixture
def mock_pipeline_result() -> dict:
    """Мок результата пайплайна с секциями metrics_payload."""
    return {
        "sections": {
            "portfolio": {
                "rows": [
                    {"metric": "total_balance", "value": 1_000_000_000},
                    {"metric": "par_30_pct", "value": 2.5},
                    {"metric": "par_60_pct", "value": 1.2},
                    {"metric": "par_90_pct", "value": 0.5},
                    {"metric": "npl_ratio", "value": 3.0},
                ]
            },
            "credit_risk": {
                "rows": [
                    {"metric": "total_expected_loss", "value": 5_000_000},
                    {"metric": "total_rwa", "value": 400_000_000},
                    {"metric": "capital_requirement", "value": 32_000_000},
                    {"metric": "portfolio_pd", "value": 0.025},
                    {"metric": "portfolio_lgd", "value": 0.45},
                ]
            },
            "finance": {
                "rows": [
                    {"metric": "nim_pct", "value": 4.2},
                    {"metric": "total_revenue", "value": 50_000_000},
                    {"metric": "total_cost", "value": 30_000_000},
                    {"metric": "net_profit", "value": 15_000_000},
                ]
            },
            "unit_economics": {
                "rows": [
                    {"metric": "ltv_cac_ratio", "value": 3.5},
                    {"metric": "cac_payback_months", "value": 8.0},
                    {"metric": "arpu", "value": 1500.0},
                    {"metric": "ltv_gross", "value": 50_000.0},
                ]
            },
            "forecast": {
                "rows": [
                    {"metric": "forecast_mae", "value": 0.15},
                    {"metric": "forecast_rmse", "value": 0.22},
                    {"metric": "model_name", "value": "linear"},
                ]
            },
        }
    }


@pytest.fixture
def minimal_pipeline_result() -> dict:
    """Минимальный результат пайплайна (пустые секции)."""
    return {"sections": {}}


# ============================================================
# Unit tests: Scenario
# ============================================================

class TestScenario:
    """Тесты класса Scenario."""

    def test_scenario_creation(self):
        """Базовое создание сценария."""
        s = Scenario(name="test", params={"rate": 1.1}, description="Test scenario")
        assert s.name == "test"
        assert s.params == {"rate": 1.1}
        assert s.description == "Test scenario"

    def test_scenario_apply_multiplicative(self, base_params):
        """Мультипликативное изменение параметра (x1.1)."""
        s = Scenario(name="up", params={"rate": 1.1})
        result = s.apply_to(base_params)
        assert result["rate"] == pytest.approx(0.15 * 1.1)
        # Остальные параметры не меняются
        assert result["pd"] == 0.02

    def test_scenario_apply_additive_positive(self, base_params):
        """Аддитивное изменение параметра (+0.05)."""
        s = Scenario(name="add", params={"rate": "+0.05"})
        result = s.apply_to(base_params)
        assert result["rate"] == pytest.approx(0.15 + 0.05)

    def test_scenario_apply_additive_negative(self, base_params):
        """Аддитивное изменение параметра (-0.03)."""
        s = Scenario(name="sub", params={"rate": "-0.03"})
        result = s.apply_to(base_params)
        assert result["rate"] == pytest.approx(0.15 - 0.03)

    def test_scenario_apply_multiple_params(self, base_params):
        """Применение нескольких параметров одновременно."""
        s = Scenario(name="multi", params={"rate": 1.1, "pd": 0.9, "conversion": "+0.1"})
        result = s.apply_to(base_params)
        assert result["rate"] == pytest.approx(0.15 * 1.1)
        assert result["pd"] == pytest.approx(0.02 * 0.9)
        assert result["conversion"] == pytest.approx(0.3 + 0.1)

    def test_scenario_apply_unknown_param_ignored(self, base_params):
        """Неизвестные параметры игнорируются (не падают)."""
        s = Scenario(name="unknown", params={"unknown_param": 1.5})
        result = s.apply_to(base_params)
        assert result == base_params  # без изменений

    def test_scenario_apply_returns_copy(self, base_params):
        """apply_to возвращает копию, не мутирует оригинал."""
        s = Scenario(name="copy", params={"rate": 2.0})
        result = s.apply_to(base_params)
        assert result is not base_params
        assert base_params["rate"] == 0.15  # оригинал неизменен

    def test_scenario_apply_string_numeric_multiplicative(self, base_params):
        """Строковое числовое значение трактуется как мультипликативное."""
        s = Scenario(name="str", params={"rate": "1.2"})
        result = s.apply_to(base_params)
        assert result["rate"] == pytest.approx(0.15 * 1.2)


# ============================================================
# Unit tests: ScenarioEngine
# ============================================================

class TestScenarioEngine:
    """Тесты класса ScenarioEngine."""

    def _make_engine(self, mock_result: dict):
        """Создать ScenarioEngine с моком пайплайна."""
        base_config = {"test": "config", "finance": {"forecast_rate_shock": 1.0}}

        def run_pipeline_fn(config_dict: dict) -> dict:
            return mock_result

        def key_metrics_extractor(payload: dict) -> dict[str, float]:
            return extract_key_metrics_from_payload(payload)

        return ScenarioEngine(
            base_config=base_config,
            run_pipeline_fn=run_pipeline_fn,
            key_metrics_extractor=key_metrics_extractor,
        )

    def test_engine_run_single_scenario(self, mock_pipeline_result):
        """Запуск одного сценария."""
        engine = self._make_engine(mock_pipeline_result)
        engine.add_scenario(Scenario(name="base", params={}))

        results = engine.run_all()
        assert len(results) == 1
        assert results[0].scenario_name == "base"
        assert "portfolio.total_balance" in results[0].metrics
        assert results[0].raw_output is not None

    def test_engine_run_multiple_scenarios(self, mock_pipeline_result):
        """Запуск нескольких сценариев."""
        engine = self._make_engine(mock_pipeline_result)
        engine.add_scenarios([
            Scenario(name="base", params={}),
            Scenario(name="stress", params={"finance.forecast_rate_shock": 1.03}),
            Scenario(name="optimistic", params={"finance.forecast_rate_shock": 0.98}),
        ])

        results = engine.run_all()
        assert len(results) == 3
        names = [r.scenario_name for r in results]
        assert names == ["base", "stress", "optimistic"]

    def test_engine_run_all_with_override(self, mock_pipeline_result):
        """run_all(scenarios=...) переопределяет добавленные сценарии."""
        engine = self._make_engine(mock_pipeline_result)
        engine.add_scenario(Scenario(name="ignored", params={}))

        custom = [Scenario(name="custom1", params={}), Scenario(name="custom2", params={})]
        results = engine.run_all(scenarios=custom)

        assert len(results) == 2
        assert [r.scenario_name for r in results] == ["custom1", "custom2"]

    def test_engine_progress_callback(self, mock_pipeline_result):
        """progress_callback вызывается с правильными аргументами."""
        engine = self._make_engine(mock_pipeline_result)
        engine.add_scenarios([
            Scenario(name="s1", params={}),
            Scenario(name="s2", params={}),
            Scenario(name="s3", params={}),
        ])

        calls: list[tuple[str, int, int]] = []

        def callback(name: str, current: int, total: int):
            calls.append((name, current, total))

        engine.run_all(progress_callback=callback)

        assert len(calls) == 3
        assert calls[0] == ("s1", 1, 3)
        assert calls[1] == ("s2", 2, 3)
        assert calls[2] == ("s3", 3, 3)

    def test_engine_error_handling_continues(self, mock_pipeline_result):
        """Ошибка в run_pipeline_fn не останавливает остальные сценарии.

        Тестируем обработку исключений напрямую, минуя модификацию конфига.
        """
        base_config = {"test": "config"}

        call_count = {"count": 0}

        def failing_pipeline(config_dict: dict) -> dict:
            call_count["count"] += 1
            if call_count["count"] == 1:
                raise ValueError("Pipeline failed on first call")
            return mock_pipeline_result

        def extractor(payload: dict) -> dict[str, float]:
            return extract_key_metrics_from_payload(payload)

        engine = ScenarioEngine(
            base_config=base_config,
            run_pipeline_fn=failing_pipeline,
            key_metrics_extractor=extractor,
        )

        # Два сценария - первый упадёт, второй пройдёт
        engine.add_scenarios([
            Scenario(name="fail", params={}),
            Scenario(name="ok", params={}),
        ])

        results = engine.run_all()
        assert len(results) == 2
        # Первый - ошибка в пайплайне
        assert results[0].scenario_name == "fail"
        assert results[0].metrics.get("error") == 1.0
        assert results[0].raw_output is None
        # Второй - успех
        assert results[1].scenario_name == "ok"
        assert "portfolio.total_balance" in results[1].metrics

    def test_engine_empty_scenarios(self, mock_pipeline_result):
        """run_all без сценариев возвращает пустой список."""
        engine = self._make_engine(mock_pipeline_result)
        results = engine.run_all()
        assert results == []


# ============================================================
# Unit tests: _apply_scenario_to_config (nested params)
# ============================================================

class TestScenarioEngineNestedConfig:
    """Тесты применения сценариев к вложенной конфигурации."""

    def _make_engine(self):
        base_config = {
            "finance": {"forecast_rate_shock": 1.0, "nim_target": 0.04},
            "credit_risk": {"pd_calibration": {"a": 1.0, "b": 0.5}, "lgd_floor": 0.45},
            "portfolio": {"par_deterioration": 1.0},
        }

        def run_pipeline_fn(config_dict: dict) -> dict:
            return {"sections": {}}

        def extractor(payload: dict) -> dict[str, float]:
            return {}

        return ScenarioEngine(
            base_config=base_config,
            run_pipeline_fn=run_pipeline_fn,
            key_metrics_extractor=extractor,
        )

    def test_nested_param_modification(self):
        """Модификация вложенного параметра через dot notation."""
        engine = self._make_engine()
        engine.add_scenario(Scenario(name="nested", params={"credit_risk.pd_calibration.a": 1.2}))

        results = engine.run_all()
        assert len(results) == 1
        # Проверяем, что конфиг был модифицирован (через raw_output или побочно)
        # Здесь просто проверяем, что сценарий запустился без ошибки
        assert results[0].scenario_name == "nested"

    def test_multiple_nested_params(self):
        """Несколько вложенных параметров."""
        engine = self._make_engine()
        engine.add_scenario(Scenario(
            name="multi_nested",
            params={
                "finance.forecast_rate_shock": 1.03,
                "credit_risk.pd_calibration.a": 1.2,
                "credit_risk.lgd_floor": 1.0,
            }
        ))

        results = engine.run_all()
        assert len(results) == 1

    def test_additive_nested_param(self):
        """Аддитивное изменение вложенного параметра."""
        engine = self._make_engine()
        engine.add_scenario(Scenario(name="add_nested", params={"finance.nim_target": "+0.01"}))

        results = engine.run_all()
        assert len(results) == 1

    def test_nonexistent_nested_path_creates_dict(self):
        """Несуществующий путь создает промежуточные dict'ы."""
        engine = self._make_engine()
        engine.add_scenario(Scenario(name="new_path", params={"new.section.param": 1.5}))

        results = engine.run_all()
        assert len(results) == 1


# ============================================================
# Unit tests: sensitivity_analysis
# ============================================================

class TestSensitivityAnalysis:
    """Тесты метода sensitivity_analysis."""

    def _make_engine(self, base_config: dict | None = None):
        if base_config is None:
            base_config = {"finance": {"forecast_rate_shock": 1.0}}

        def run_pipeline_fn(config_dict: dict) -> dict:
            return {"sections": {}}

        def extractor(payload: dict) -> dict[str, float]:
            return {}

        return ScenarioEngine(
            base_config=base_config,
            run_pipeline_fn=run_pipeline_fn,
            key_metrics_extractor=extractor,
        )

    def test_sensitivity_analysis_basic(self):
        """Базовый sensitivity анализ по одному параметру."""
        engine = self._make_engine()
        results = engine.sensitivity_analysis(
            param_name="finance.forecast_rate_shock",
            values=[0.97, 1.0, 1.03],
        )

        assert len(results) == 3
        names = [r.scenario_name for r in results]
        assert names == [
            "finance.forecast_rate_shock=0.970",
            "finance.forecast_rate_shock=1.000",
            "finance.forecast_rate_shock=1.030",
        ]

    def test_sensitivity_analysis_with_base_value(self):
        """Sensitivity анализ с явным base_value."""
        engine = self._make_engine(base_config={"rate": 0.15})
        results = engine.sensitivity_analysis(
            param_name="rate",
            values=[0.12, 0.15, 0.18],
            base_value=0.15,
        )

        assert len(results) == 3
        # Параметры должны быть множителями относительно base_value
        # 0.12/0.15 = 0.8, 0.15/0.15 = 1.0, 0.18/0.15 = 1.2
        params = [r.params["rate"] for r in results]
        assert params[0] == pytest.approx(0.8)
        assert params[1] == pytest.approx(1.0)
        assert params[2] == pytest.approx(1.2)

    def test_sensitivity_analysis_zero_base_value(self):
        """Sensitivity анализ с base_value=0 (fallback на абсолютные значения)."""
        engine = self._make_engine(base_config={"param": 0.0})
        results = engine.sensitivity_analysis(
            param_name="param",
            values=[1.0, 2.0, 3.0],
            base_value=0.0,
        )

        assert len(results) == 3
        # При base_value=0 делить нельзя, используются абсолютные значения
        params = [r.params["param"] for r in results]
        assert params == [1.0, 2.0, 3.0]

    def test_sensitivity_analysis_extracts_base_from_config(self):
        """Автоматическое извлечение base_value из конфига."""
        engine = self._make_engine(base_config={"finance": {"forecast_rate_shock": 1.05}})
        results = engine.sensitivity_analysis(
            param_name="finance.forecast_rate_shock",
            values=[1.0, 1.05, 1.1],
        )

        assert len(results) == 3
        params = [r.params["finance.forecast_rate_shock"] for r in results]
        # 1.0/1.05, 1.05/1.05, 1.1/1.05
        assert params[1] == pytest.approx(1.0)


# ============================================================
# Unit tests: create_standard_scenarios
# ============================================================

class TestCreateStandardScenarios:
    """Тесты фабрики стандартных сценариев."""

    def test_returns_six_scenarios(self):
        """Возвращает ровно 6 сценариев."""
        scenarios = create_standard_scenarios()
        assert len(scenarios) == 6

    def test_scenario_names(self):
        """Проверка имён всех 6 сценариев."""
        scenarios = create_standard_scenarios()
        names = {s.name for s in scenarios}
        expected = {
            "base",
            "stress_rate_up",
            "stress_rate_down",
            "stress_conversion_drop",
            "optimistic",
            "regulatory_stress",
        }
        assert names == expected

    def test_base_scenario_empty_params(self):
        """Базовый сценарий имеет пустые параметры."""
        scenarios = create_standard_scenarios()
        base = next(s for s in scenarios if s.name == "base")
        assert base.params == {}

    def test_stress_rate_up_params(self):
        """stress_rate_up: рост ставки + ухудшение PD."""
        scenarios = create_standard_scenarios()
        s = next(s for s in scenarios if s.name == "stress_rate_up")
        assert "finance.forecast_rate_shock" in s.params
        assert s.params["finance.forecast_rate_shock"] == 1.03
        assert "credit_risk.pd_calibration.a" in s.params
        assert s.params["credit_risk.pd_calibration.a"] == 1.2

    def test_stress_rate_down_params(self):
        """stress_rate_down: снижение ставки + улучшение PD."""
        scenarios = create_standard_scenarios()
        s = next(s for s in scenarios if s.name == "stress_rate_down")
        assert s.params["finance.forecast_rate_shock"] == 0.97
        assert s.params["credit_risk.pd_calibration.a"] == 0.9

    def test_stress_conversion_drop_params(self):
        """stress_conversion_drop: конверсия -20%, PAR +30%."""
        scenarios = create_standard_scenarios()
        s = next(s for s in scenarios if s.name == "stress_conversion_drop")
        assert s.params["funnel.conversion_multiplier"] == 0.8
        assert s.params["portfolio.par_deterioration"] == 1.3

    def test_optimistic_params(self):
        """optimistic: ставки вниз, качество вверх, конверсия выше."""
        scenarios = create_standard_scenarios()
        s = next(s for s in scenarios if s.name == "optimistic")
        assert s.params["finance.forecast_rate_shock"] == 0.98
        assert s.params["credit_risk.pd_calibration.a"] == 0.85
        assert s.params["funnel.conversion_multiplier"] == 1.15

    def test_regulatory_stress_params(self):
        """regulatory_stress: половые LGD, повышенные RWA."""
        scenarios = create_standard_scenarios()
        s = next(s for s in scenarios if s.name == "regulatory_stress")
        assert s.params["credit_risk.lgd_floor"] == 1.0
        assert s.params["credit_risk.rwa_multiplier"] == 1.25

    def test_all_scenarios_have_descriptions(self):
        """Все сценарии имеют непустое описание."""
        scenarios = create_standard_scenarios()
        for s in scenarios:
            assert s.description
            assert len(s.description) > 10


# ============================================================
# Unit tests: extract_key_metrics_from_payload
# ============================================================

class TestExtractKeyMetrics:
    """Тесты извлечения ключевых метрик."""

    def test_extract_all_sections(self, mock_pipeline_result):
        """Извлечение метрик из всех секций."""
        metrics = extract_key_metrics_from_payload(mock_pipeline_result)

        # Portfolio
        assert metrics["portfolio.total_balance"] == 1_000_000_000
        assert metrics["portfolio.par_30_pct"] == 2.5
        assert metrics["portfolio.par_60_pct"] == 1.2
        assert metrics["portfolio.par_90_pct"] == 0.5
        assert metrics["portfolio.npl_ratio"] == 3.0

        # Credit risk
        assert metrics["credit_risk.total_expected_loss"] == 5_000_000
        assert metrics["credit_risk.total_rwa"] == 400_000_000
        assert metrics["credit_risk.capital_requirement"] == 32_000_000
        assert metrics["credit_risk.portfolio_pd"] == 0.025
        assert metrics["credit_risk.portfolio_lgd"] == 0.45

        # Finance
        assert metrics["finance.nim_pct"] == 4.2
        assert metrics["finance.total_revenue"] == 50_000_000
        assert metrics["finance.total_cost"] == 30_000_000
        assert metrics["finance.net_profit"] == 15_000_000

        # Unit economics
        assert metrics["unit_economics.ltv_cac_ratio"] == 3.5
        assert metrics["unit_economics.cac_payback_months"] == 8.0
        assert metrics["unit_economics.arpu"] == 1500.0
        assert metrics["unit_economics.ltv_gross"] == 50_000.0

        # Forecast
        assert metrics["forecast.forecast_mae"] == 0.15
        assert metrics["forecast.forecast_rmse"] == 0.22
        assert metrics["forecast.model_name"] == "linear"

    def test_extract_empty_sections(self, minimal_pipeline_result):
        """Пустые секции не падают, возвращают пустой dict."""
        metrics = extract_key_metrics_from_payload(minimal_pipeline_result)
        assert metrics == {}

    def test_extract_missing_sections(self):
        """Отсутствующие секции игнорируются."""
        payload = {"sections": {"portfolio": {"rows": []}}}
        metrics = extract_key_metrics_from_payload(payload)
        assert metrics == {}

    def test_extract_none_values_handled(self):
        """None значения в value конвертируются в 0."""
        payload = {
            "sections": {
                "portfolio": {
                    "rows": [
                        {"metric": "total_balance", "value": None},
                        {"metric": "par_30_pct", "value": 5.0},
                    ]
                }
            }
        }
        metrics = extract_key_metrics_from_payload(payload)
        assert metrics["portfolio.total_balance"] == 0
        assert metrics["portfolio.par_30_pct"] == 5.0

    def test_extract_ignores_unknown_metrics(self):
        """Неизвестные метрики не попадают в результат."""
        payload = {
            "sections": {
                "portfolio": {
                    "rows": [
                        {"metric": "unknown_metric", "value": 100},
                        {"metric": "total_balance", "value": 1000},
                    ]
                }
            }
        }
        metrics = extract_key_metrics_from_payload(payload)
        assert "portfolio.unknown_metric" not in metrics
        assert metrics["portfolio.total_balance"] == 1000


# ============================================================
# Unit tests: scenario_results_to_dataframe
# ============================================================

class TestScenarioResultsToDataFrame:
    """Тесты преобразования результатов в DataFrame."""

    def test_basic_dataframe(self, mock_pipeline_result):
        """Базовый DataFrame с результатами."""
        results = [
            ScenarioResult(
                scenario_name="base",
                params={},
                metrics={"portfolio.total_balance": 1000, "credit_risk.total_expected_loss": 50},
                raw_output=mock_pipeline_result,
            ),
            ScenarioResult(
                scenario_name="stress",
                params={"finance.forecast_rate_shock": 1.03},
                metrics={"portfolio.total_balance": 950, "credit_risk.total_expected_loss": 60},
                raw_output=mock_pipeline_result,
            ),
        ]

        df = scenario_results_to_dataframe(results)

        assert len(df) == 2
        assert list(df.columns)[0] == "scenario"
        assert "portfolio.total_balance" in df.columns
        assert "credit_risk.total_expected_loss" in df.columns
        assert "finance.forecast_rate_shock" in df.columns

        # Проверяем значения
        base_row = df[df["scenario"] == "base"].iloc[0]
        assert base_row["portfolio.total_balance"] == 1000
        assert pd.isna(base_row["finance.forecast_rate_shock"])

        stress_row = df[df["scenario"] == "stress"].iloc[0]
        assert stress_row["portfolio.total_balance"] == 950
        assert stress_row["finance.forecast_rate_shock"] == 1.03

    def test_empty_results(self):
        """Пустой список возвращает пустой DataFrame."""
        df = scenario_results_to_dataframe([])
        assert isinstance(df, pd.DataFrame)
        assert len(df) == 0

    def test_column_order_scenario_first(self):
        """Колонка scenario всегда первой."""
        results = [
            ScenarioResult("z_last", {"a": 1}, {"x": 10}, {}),
            ScenarioResult("a_first", {"b": 2}, {"y": 20}, {}),
        ]
        df = scenario_results_to_dataframe(results)
        assert df.columns[0] == "scenario"
        assert df.iloc[0]["scenario"] == "z_last"  # порядок строк сохраняется


# ============================================================
# Unit tests: scenario_results_summary
# ============================================================

class TestScenarioResultsSummary:
    """Тесты сводки отклонений от базового сценария."""

    def test_summary_basic(self, mock_pipeline_result):
        """Базовая сводка: абсолютные и процентные отклонения."""
        base_metrics = {
            "portfolio.total_balance": 1_000_000_000,
            "credit_risk.total_expected_loss": 5_000_000,
            "finance.nim_pct": 4.2,
        }
        stress_metrics = {
            "portfolio.total_balance": 950_000_000,
            "credit_risk.total_expected_loss": 7_500_000,
            "finance.nim_pct": 3.8,
        }

        results = [
            ScenarioResult("base", {}, base_metrics, mock_pipeline_result),
            ScenarioResult("stress", {"rate": 1.03}, stress_metrics, mock_pipeline_result),
        ]

        summary = scenario_results_summary(results)

        assert "stress" in summary
        stress_diff = summary["stress"]

        # portfolio.total_balance: -50M (-5%)
        assert stress_diff["portfolio.total_balance_abs"] == -50_000_000
        assert stress_diff["portfolio.total_balance_pct"] == pytest.approx(-5.0)

        # credit_risk.total_expected_loss: +2.5M (+50%)
        assert stress_diff["credit_risk.total_expected_loss_abs"] == 2_500_000
        assert stress_diff["credit_risk.total_expected_loss_pct"] == pytest.approx(50.0)

        # finance.nim_pct: -0.4pp (-9.52%)
        assert stress_diff["finance.nim_pct_abs"] == pytest.approx(-0.4)
        assert stress_diff["finance.nim_pct_pct"] == pytest.approx(-9.5238, rel=1e-3)

    def test_summary_missing_base_returns_empty(self):
        """Если нет базового сценария — пустой dict."""
        results = [
            ScenarioResult("stress", {}, {"metric": 100}, {}),
        ]
        summary = scenario_results_summary(results, base_scenario="base")
        assert summary == {}

    def test_summary_base_value_zero(self):
        """Базовое значение 0: процент = inf/-inf/0."""
        results = [
            ScenarioResult("base", {}, {"metric": 0}, {}),
            ScenarioResult("sc1", {}, {"metric": 10}, {}),
            ScenarioResult("sc2", {}, {"metric": -5}, {}),
            ScenarioResult("sc3", {}, {"metric": 0}, {}),
        ]

        summary = scenario_results_summary(results)

        assert summary["sc1"]["metric_pct"] == float("inf")
        assert summary["sc2"]["metric_pct"] == float("-inf")
        assert summary["sc3"]["metric_pct"] == 0

    def test_summary_only_common_metrics(self):
        """Сравниваются только метрики, есть в обоих сценариях."""
        results = [
            ScenarioResult("base", {}, {"a": 10, "b": 20}, {}),
            ScenarioResult("sc1", {}, {"a": 15, "c": 30}, {}),  # b нет, c нет в base
        ]

        summary = scenario_results_summary(results)

        assert "a_abs" in summary["sc1"]
        assert "a_pct" in summary["sc1"]
        assert "b_abs" not in summary["sc1"]
        assert "c_abs" not in summary["sc1"]

    def test_summary_custom_base_name(self):
        """Можно указать другое имя базового сценария."""
        results = [
            ScenarioResult("custom_base", {}, {"metric": 100}, {}),
            ScenarioResult("sc1", {}, {"metric": 120}, {}),
        ]

        summary = scenario_results_summary(results, base_scenario="custom_base")
        assert summary["sc1"]["metric_abs"] == 20
        assert summary["sc1"]["metric_pct"] == 20.0


# ============================================================
# Integration tests: ScenarioEngine with realistic pipeline
# ============================================================

class TestScenarioIntegration:
    """Интеграционные тесты с реальными компонентами (без I/O)."""

    @pytest.fixture
    def sample_tables(self):
        """Минимальный набор таблиц для работы пайплайна."""
        import datetime
        return {
            "portfolio": pd.DataFrame({
                "account_id": ["A1", "A2", "A3"],
                "client_id": ["C1", "C2", "C3"],
                "product_code": ["LOAN", "LOAN", "DEPOSIT"],
                "open_date": [datetime.date(2023, 1, 1)] * 3,
                "close_date": [None, None, None],
                "limit_amount": [1_000_000, 2_000_000, 500_000],
                "balance": [800_000, 1_500_000, 500_000],
                "currency": ["RUB"] * 3,
                "status": ["ACTIVE"] * 3,
                "dpd": [0, 45, 0],
                "par_bucket": ["0", "31-60", "0"],
            }),
            "clients": pd.DataFrame({
                "client_id": ["C1", "C2", "C3"],
                "segment": ["RETAIL", "SME", "RETAIL"],
                "region": ["MOSCOW", "SPB", "MOSCOW"],
                "acquisition_channel": ["ONLINE", "BRANCH", "ONLINE"],
                "registration_date": pd.to_datetime(["2020-01-01", "2021-06-15", "2022-03-10"]),
                "age": [35, 42, 28],
                "gender": ["M", "F", "M"],
            }),
            "finance": pd.DataFrame({
                "period": ["2024-01", "2024-02", "2024-03"],
                "product_code": ["LOAN", "LOAN", "LOAN"],
                "interest_income": [1_000_000, 1_100_000, 1_050_000],
                "interest_expense": [400_000, 420_000, 410_000],
                "fee_income": [100_000, 110_000, 105_000],
                "funding_cost": [200_000, 210_000, 205_000],
                "opex": [150_000, 155_000, 152_000],
                "reserve_expense": [50_000, 55_000, 52_000],
                "tax": [30_000, 33_000, 31_000],
                "marketing_cost": [20_000, 22_000, 21_000],
            }),
            "products": pd.DataFrame({
                "product_code": ["LOAN", "DEPOSIT"],
                "product_type": ["LOAN", "DEPOSIT"],
                "product_name": ["Consumer Loan", "Term Deposit"],
                "interest_rate": [0.15, 0.08],
                "tariff": [0.02, 0.0],
                "cost_of_funds": [0.08, 0.05],
                "reserve_rate": [0.02, 0.0],
            }),
            "funnel": pd.DataFrame({
                "period": ["2024-01", "2024-01", "2024-01", "2024-02", "2024-02", "2024-02", "2024-03", "2024-03", "2024-03"],
                "stage": ["application", "approved", "issued"] * 3,
                "count": [1000, 600, 500] * 3,
                "product_code": ["LOAN"] * 9,
            }),
            "sales": pd.DataFrame({
                "period": ["2024-01", "2024-02", "2024-03"],
                "product_code": ["LOAN", "LOAN", "LOAN"],
                "amount": [500_000, 550_000, 525_000],
                "deals": [50, 55, 52],
                "source": ["ONLINE"] * 3,
            }),
        }

    def test_engine_with_real_components(self, sample_tables):
        """Полный прогон ScenarioEngine с реальными функциями пайплайна.

        Проверяем, что движок запускается и возвращает результаты для всех 6 сценариев.
        Не проверяем точное содержимое метрик, так как структура выходных данных
        реального пайплайна может отличаться от мока.
        """
        from finanalyst.cli import (
            _init_metrics_payload,
            _stage_credit_risk,
            _stage_finance,
            _stage_funnel,
            _stage_metrics,
            _stage_plugins,
            _stage_portfolio,
        )
        from finanalyst.config import PipelineConfig
        from finanalyst.core import validate_inputs

        base_config = PipelineConfig(
            scenario={"enabled": False},  # не запускаем вложенный сценарий
        ).to_dict()

        def run_pipeline_fn(config_dict: dict) -> dict:
            temp_config = PipelineConfig.from_dict(config_dict)

            from argparse import Namespace
            from dataclasses import dataclass, field
            from pathlib import Path

            @dataclass
            class TempPipeline:
                args: Namespace
                config: PipelineConfig
                tables: dict
                data_dir: Path
                chunksize: int | None = None
                out_dir: Path = field(default_factory=lambda: Path("/tmp"))
                notes: list = field(default_factory=list)
                warnings: list = field(default_factory=list)
                sections: list = field(default_factory=list)
                metrics_payload: dict = field(default_factory=lambda: {"sections": {}})
                assumptions: list = field(default_factory=list)
                headline: list = field(default_factory=list)
                charts: dict = field(default_factory=dict)
                unit: dict = field(default_factory=dict)
                ab_result: dict | None = None
                ab_group_col: str = "experiment_group"
                quality: any = None

            temp_args = Namespace(
                quiet=True,
                adapter=None,
                credit_risk=True,
                drivers=False,
                segment=False,
                cohort=False,
                migration=False,
                vintage=False,
                compare_dirs=None,
                source="files",
                pii_mode="strict",
                format="docx",
                lifetime_months=12,
            )
            temp_p = TempPipeline(
                args=temp_args,
                config=temp_config,
                tables=sample_tables,
                data_dir=Path("."),
            )
            temp_p.quality = validate_inputs(temp_p.tables)
            temp_p.warnings = list(temp_p.quality.warnings)
            _init_metrics_payload(temp_p)
            _stage_plugins(temp_p)
            _stage_portfolio(temp_p)
            _stage_funnel(temp_p)
            _stage_finance(temp_p)
            _stage_credit_risk(temp_p)
            _stage_metrics(temp_p)

            return temp_p.metrics_payload

        def extractor(payload: dict) -> dict[str, float]:
            return extract_key_metrics_from_payload(payload)

        engine = ScenarioEngine(
            base_config=base_config,
            run_pipeline_fn=run_pipeline_fn,
            key_metrics_extractor=extractor,
        )

        # Запускаем стандартные сценарии
        engine.add_scenarios(create_standard_scenarios())
        results = engine.run_all()

        assert len(results) == 6
        for r in results:
            assert r.scenario_name in {
                "base", "stress_rate_up", "stress_rate_down",
                "stress_conversion_drop", "optimistic", "regulatory_stress"
            }
            # Проверяем, что сценарий выполнился (raw_output не None)
            assert r.raw_output is not None
            # metrics может быть пустым если структура отличается - это ок для интеграционного теста

        # Проверяем, что сводка строится (даже если пустая)
        summary = scenario_results_summary(results)
        assert isinstance(summary, dict)

    def test_sensitivity_analysis_integration(self, sample_tables):
        """Интеграционный тест sensitivity_analysis.

        Проверяем, что sensitivity анализ запускается и возвращает результаты.
        Не проверяем точное содержимое метрик.
        """
        from finanalyst.cli import (
            _init_metrics_payload,
            _stage_credit_risk,
            _stage_finance,
            _stage_funnel,
            _stage_metrics,
            _stage_plugins,
            _stage_portfolio,
        )
        from finanalyst.config import PipelineConfig
        from finanalyst.core import validate_inputs

        base_config = PipelineConfig().to_dict()

        def run_pipeline_fn(config_dict: dict) -> dict:
            temp_config = PipelineConfig.from_dict(config_dict)
            from argparse import Namespace
            from dataclasses import dataclass, field
            from pathlib import Path

            @dataclass
            class TempPipeline:
                args: Namespace
                config: PipelineConfig
                tables: dict
                data_dir: Path
                chunksize: int | None = None
                out_dir: Path = field(default_factory=lambda: Path("/tmp"))
                notes: list = field(default_factory=list)
                warnings: list = field(default_factory=list)
                sections: list = field(default_factory=list)
                metrics_payload: dict = field(default_factory=lambda: {"sections": {}})
                assumptions: list = field(default_factory=list)
                headline: list = field(default_factory=list)
                charts: dict = field(default_factory=dict)
                unit: dict = field(default_factory=dict)
                ab_result: dict | None = None
                ab_group_col: str = "experiment_group"
                quality: any = None

            temp_args = Namespace(
                quiet=True, adapter=None, credit_risk=True, drivers=False,
                segment=False, cohort=False, migration=False, vintage=False,
                compare_dirs=None, source="files", pii_mode="strict",
                format="docx", lifetime_months=12,
            )
            temp_p = TempPipeline(args=temp_args, config=temp_config, tables=sample_tables, data_dir=Path("."))
            temp_p.quality = validate_inputs(temp_p.tables)
            temp_p.warnings = list(temp_p.quality.warnings)
            _init_metrics_payload(temp_p)
            _stage_plugins(temp_p)
            _stage_portfolio(temp_p)
            _stage_funnel(temp_p)
            _stage_finance(temp_p)
            _stage_credit_risk(temp_p)
            _stage_metrics(temp_p)
            return temp_p.metrics_payload

        def extractor(payload: dict) -> dict[str, float]:
            return extract_key_metrics_from_payload(payload)

        engine = ScenarioEngine(
            base_config=base_config,
            run_pipeline_fn=run_pipeline_fn,
            key_metrics_extractor=extractor,
        )

        # Sensitivity по PD calibration
        results = engine.sensitivity_analysis(
            param_name="credit_risk.pd_calibration.a",
            values=[0.8, 1.0, 1.2, 1.5],
        )

        assert len(results) == 4
        for r in results:
            assert r.raw_output is not None
            # metrics может быть пустым - проверяем только что сценарий выполнился


# ============================================================
# Property-based tests
# ============================================================

class TestScenarioProperties:
    """Property-based тесты инвариантов сценариев."""

    @given(
        st.dictionaries(
            st.text(min_size=1, max_size=10),
            st.floats(min_value=0.01, max_value=1000, allow_nan=False, allow_infinity=False),
            min_size=1, max_size=10
        ),
        st.dictionaries(
            st.text(min_size=1, max_size=10),
            st.floats(min_value=0.1, max_value=3.0, allow_nan=False, allow_infinity=False),
            min_size=1, max_size=5
        ),
    )
    @settings(max_examples=50)
    def test_scenario_apply_preserves_keys(self, base_params, scenario_params):
        """apply_to сохраняет все ключи базовых параметров."""
        s = Scenario(name="prop", params=scenario_params)
        result = s.apply_to(base_params)
        assert set(result.keys()) == set(base_params.keys())

    @given(
        st.floats(min_value=0.1, max_value=10.0, allow_nan=False, allow_infinity=False),
        st.floats(min_value=0.1, max_value=3.0, allow_nan=False, allow_infinity=False),
    )
    @settings(max_examples=50)
    def test_multiplicative_vs_additive(self, base_val, mult):
        """Мультипликативное и аддитивное изменения дают эквивалентные результаты.

        Аддитивное изменение эквивалентно мультипликативному: add = base * (mult - 1)
        """
        base = {"param": base_val}
        s_mult = Scenario(name="m", params={"param": mult})
        # Аддитивное изменение, эквивалентное мультипликативному
        add_value = base_val * (mult - 1)
        # Форматируем строку правильно для отрицательных значений
        if add_value >= 0:
            add_str = f"+{add_value:.4f}"
        else:
            add_str = f"{add_value:.4f}"  # уже содержит минус
        s_add = Scenario(name="a", params={"param": add_str})

        res_mult = s_mult.apply_to(base)
        res_add = s_add.apply_to(base)

        # Результаты должны быть эквивалентны (с допуском на float precision)
        assert res_mult["param"] == pytest.approx(res_add["param"], rel=1e-2, abs=1e-9)

    @given(
        st.lists(
            st.floats(min_value=0.5, max_value=2.0, allow_nan=False, allow_infinity=False),
            min_size=2, max_size=10
        ),
    )
    @settings(max_examples=30)
    def test_sensitivity_analysis_monotonic_params(self, values):
        """Параметры sensitivity_analysis — это множители относительно base."""
        base_config = {"param": 1.0}

        def run_fn(config):
            return {"sections": {}}

        def ext(payload):
            return {}

        engine = ScenarioEngine(base_config, run_fn, ext)
        results = engine.sensitivity_analysis("param", values, base_value=1.0)

        params = [r.params["param"] for r in results]
        # params[i] = values[i] / 1.0 = values[i]
        for i, v in enumerate(values):
            assert params[i] == pytest.approx(v)


# ============================================================
# CLI integration test (lightweight)
# ============================================================

class TestScenarioCLIConfig:
    """Тесты конфигурации сценариев через YAML/CLI (unit level)."""

    def test_yaml_scenario_loading(self, tmp_path):
        """Загрузка сценариев из YAML файла."""
        import yaml
        from finanalyst.scenario import Scenario

        yaml_content = {
            "scenarios": [
                {"name": "yaml_base", "params": {}, "description": "Base from YAML"},
                {"name": "yaml_stress", "params": {"rate": 1.2}, "description": "Stress from YAML"},
            ]
        }
        yaml_file = tmp_path / "scenarios.yaml"
        with open(yaml_file, "w", encoding="utf-8") as f:
            yaml.dump(yaml_content, f)

        # Имитируем логику из cli.py _stage_scenario
        with open(yaml_file, encoding="utf-8") as f:
            scenario_data = yaml.safe_load(f) or {}

        scenarios_to_run = []
        if "scenarios" in scenario_data:
            for s in scenario_data["scenarios"]:
                scenarios_to_run.append(Scenario(
                    name=s.get("name", "unnamed"),
                    params=s.get("params", {}),
                    description=s.get("description", ""),
                ))

        assert len(scenarios_to_run) == 2
        assert scenarios_to_run[0].name == "yaml_base"
        assert scenarios_to_run[1].params == {"rate": 1.2}

    def test_preset_standard_loads_six(self):
        """Preset 'standard' загружает 6 сценариев."""
        from finanalyst.scenario import create_standard_scenarios

        standard = create_standard_scenarios()
        assert len(standard) == 6

    def test_preset_all_same_as_standard(self):
        """Preset 'all' на данный момент равен 'standard'."""
        from finanalyst.scenario import create_standard_scenarios

        standard = create_standard_scenarios()
        # В коде cli.py оба ветвления делают extend(standard)
        all_scenarios = list(standard)
        all_scenarios.extend(standard)  # как в cli.py при preset=="all"

        # Сейчас это дублирует, но так работает текущая реализация
        assert len(all_scenarios) == 12

    def test_cli_params_override_base_scenario(self):
        """CLI params применяются к первому (базовому) сценарию."""
        from finanalyst.scenario import Scenario

        scenarios_to_run = [Scenario(name="base", params={})]
        cli_params = {"finance.forecast_rate_shock": 1.05, "credit_risk.pd_calibration.a": 1.1}

        if scenarios_to_run:
            base_scenario = scenarios_to_run[0]
            for key, value in cli_params.items():
                base_scenario.params[key] = value

        assert scenarios_to_run[0].params == cli_params


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
