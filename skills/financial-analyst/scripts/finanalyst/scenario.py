"""Сценарный анализ: what-if моделирование и sensitivity analysis."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

import pandas as pd


@dataclass
class Scenario:
    """Один сценарий: имя, параметры, описание."""

    name: str
    params: dict[str, float]  # имя параметра -> множитель/значение
    description: str = ""

    def apply_to(self, base_params: dict[str, float]) -> dict[str, float]:
        """Применить сценарий к базовым параметрам (модифицирует копию)."""
        result = base_params.copy()
        for key, value in self.params.items():
            if key in result:
                # Поддерживаем мультипликативные (x1.1) и аддитивные (+0.05) изменения
                if isinstance(value, str) and value.startswith(("+", "-")):
                    result[key] += float(value)
                else:
                    result[key] *= float(value)
        return result


@dataclass
class ScenarioResult:
    """Результат расчёта по одному сценарию."""

    scenario_name: str
    params: dict[str, float]
    metrics: dict[str, float]  # ключевые метрики (EL, RWA, NIM, PAR30, LTV/CAC, etc.)
    raw_output: Any  # полный результат пайплайна (если нужно)


class ScenarioEngine:
    """Движок сценарийного анализа."""

    def __init__(
        self,
        base_config: dict[str, Any],
        run_pipeline_fn: Callable[[dict[str, Any]], dict[str, Any]],
        key_metrics_extractor: Callable[[dict[str, Any]], dict[str, float]],
    ):
        """
        Args:
            base_config: базовая конфигурация пайплайна (dict)
            run_pipeline_fn: функция запуска пайплайна, принимает config -> возвращает metrics_payload
            key_metrics_extractor: функция извлечения ключевых метрик из результата
        """
        self.base_config = base_config
        self.run_pipeline_fn = run_pipeline_fn
        self.key_metrics_extractor = key_metrics_extractor
        self.scenarios: list[Scenario] = []

    def add_scenario(self, scenario: Scenario) -> None:
        """Добавить сценарий."""
        self.scenarios.append(scenario)

    def add_scenarios(self, scenarios: list[Scenario]) -> None:
        """Добавить несколько сценариев."""
        self.scenarios.extend(scenarios)

    def run_all(
        self,
        scenarios: list[Scenario] | None = None,
        progress_callback: Callable[[str, int, int], None] | None = None,
    ) -> list[ScenarioResult]:
        """Запустить все сценарии последовательно."""
        scenarios_to_run = scenarios if scenarios is not None else self.scenarios
        results: list[ScenarioResult] = []
        total = len(scenarios_to_run)

        for i, scenario in enumerate(scenarios_to_run):
            if progress_callback:
                progress_callback(scenario.name, i + 1, total)

            # Применяем сценарий к конфигу
            modified_config = self.base_config.copy()
            # Сценарий может модифицировать параметры в nested структурах
            modified_config = self._apply_scenario_to_config(modified_config, scenario)

            # Запускаем пайплайн
            try:
                pipeline_result = self.run_pipeline_fn(modified_config)
                key_metrics = self.key_metrics_extractor(pipeline_result)

                results.append(ScenarioResult(
                    scenario_name=scenario.name,
                    params=scenario.params,
                    metrics=key_metrics,
                    raw_output=pipeline_result,
                ))
            except Exception:
                # Записываем ошибку, но продолжаем
                results.append(ScenarioResult(
                    scenario_name=scenario.name,
                    params=scenario.params,
                    metrics={"error": 1.0},  # placeholder float value
                    raw_output=None,
                ))

        return results

    def _apply_scenario_to_config(self, config: dict, scenario: Scenario) -> dict:
        """Рекурсивно применить параметры сценария к конфигу."""
        import copy
        config = copy.deepcopy(config)

        for param_path, value in scenario.params.items():
            # param_path может быть вложенным: "credit_risk.pd_calibration.a"
            keys = param_path.split(".")
            target = config
            for key in keys[:-1]:
                if key not in target:
                    target[key] = {}
                target = target[key]

            final_key = keys[-1]
            if final_key in target:
                base_val = target[final_key]
                if isinstance(value, str) and value.startswith(("+", "-")):
                    target[final_key] = base_val + float(value)
                else:
                    target[final_key] = base_val * float(value)

        return config

    def sensitivity_analysis(
        self,
        param_name: str,
        values: list[float],
        base_value: float | None = None,
    ) -> list[ScenarioResult]:
        """Быстрый sensitivity анализ по одному параметру: прогнать серию значений."""
        if base_value is None:
            # Попробуем извлечь из базового конфига
            keys = param_name.split(".")
            base_value_: float = 1.0
            target: Any = self.base_config
            for k in keys:
                if isinstance(target, dict):
                    target = target.get(k, 1.0)
                if isinstance(target, (int, float)):
                    base_value_ = float(target)
                    break
            base_value = base_value_

        scenarios = [
            Scenario(
                name=f"{param_name}={v:.3f}",
                params={param_name: v / base_value if base_value != 0 else v},
                description=f"Sensitivity: {param_name} = {v}",
            )
            for v in values
        ]

        return self.run_all(scenarios=scenarios)


def create_standard_scenarios() -> list[Scenario]:
    """Создать набор стандартных сценариев для банковского портфеля."""
    return [
        Scenario(
            name="base",
            params={},
            description="Базовый сценарий (текущие параметры)",
        ),
        Scenario(
            name="stress_rate_up",
            params={
                "finance.forecast_rate_shock": 1.03,  # +3% к ставке
                "credit_risk.pd_calibration.a": 1.2,   # ухудшение PD
            },
            description="Стресс: рост ключевой ставки +3%, ухудшение качества заёмщиков",
        ),
        Scenario(
            name="stress_rate_down",
            params={
                "finance.forecast_rate_shock": 0.97,   # -3% к ставке
                "credit_risk.pd_calibration.a": 0.9,   # улучшение PD
            },
            description="Стресс: снижение ключевой ставки -3%, улучшение качества",
        ),
        Scenario(
            name="stress_conversion_drop",
            params={
                "funnel.conversion_multiplier": 0.8,   # -20% конверсия
                "portfolio.par_deterioration": 1.3,    # PAR ухудшается на 30%
            },
            description="Стресс: падение конверсии воронки -20%, рост просрочки",
        ),
        Scenario(
            name="optimistic",
            params={
                "finance.forecast_rate_shock": 0.98,
                "credit_risk.pd_calibration.a": 0.85,
                "funnel.conversion_multiplier": 1.15,
            },
            description="Оптимистичный: ставки вниз, качество вверх, конверсия выше",
        ),
        Scenario(
            name="regulatory_stress",
            params={
                "credit_risk.lgd_floor": 1.0,          # LGD пол = 45% (Basel)
                "credit_risk.rwa_multiplier": 1.25,    # RWA буфер +25%
            },
            description="Регуляторный стресс: половые LGD, повышенные RWA",
        ),
    ]


def extract_key_metrics_from_payload(payload: dict[str, Any]) -> dict[str, float]:
    """Извлечь ключевые метрики из metrics_payload для сравнения сценариев."""
    metrics: dict[str, float] = {}
    sections = payload.get("sections", {})

    # Портфель
    if "portfolio" in sections:
        port = sections["portfolio"]
        if isinstance(port, dict) and "rows" in port:
            for row in port["rows"]:
                if row.get("metric") in ("total_balance", "par_30_pct", "par_60_pct", "par_90_pct", "npl_ratio"):
                    metrics[f"portfolio.{row['metric']}"] = row.get("value", 0) or 0

    # Кредитный риск
    if "credit_risk" in sections:
        cr = sections["credit_risk"]
        if isinstance(cr, dict) and "rows" in cr:
            for row in cr["rows"]:
                if row.get("metric") in ("total_expected_loss", "total_rwa", "capital_requirement", "portfolio_pd", "portfolio_lgd"):
                    metrics[f"credit_risk.{row['metric']}"] = row.get("value", 0) or 0

    # Финансы
    if "finance" in sections:
        fin = sections["finance"]
        if isinstance(fin, dict) and "rows" in fin:
            for row in fin["rows"]:
                if row.get("metric") in ("nim_pct", "total_revenue", "total_cost", "net_profit"):
                    metrics[f"finance.{row['metric']}"] = row.get("value", 0) or 0

    # Юнит-экономика
    if "unit_economics" in sections:
        ue = sections["unit_economics"]
        if isinstance(ue, dict) and "rows" in ue:
            for row in ue["rows"]:
                if row.get("metric") in ("ltv_cac_ratio", "cac_payback_months", "arpu", "ltv_gross"):
                    metrics[f"unit_economics.{row['metric']}"] = row.get("value", 0) or 0

    # Прогноз
    if "forecast" in sections:
        fc = sections["forecast"]
        if isinstance(fc, dict) and "rows" in fc:
            for row in fc["rows"]:
                if row.get("metric") in ("forecast_mae", "forecast_rmse", "model_name"):
                    metrics[f"forecast.{row['metric']}"] = row.get("value", 0) or 0

    return metrics


def scenario_results_to_dataframe(results: list[ScenarioResult]) -> pd.DataFrame:
    """Преобразовать результаты сценариев в DataFrame для табличного отчёта."""
    rows = []
    for r in results:
        base_row: dict[str, Any] = {"scenario": r.scenario_name}
        base_row.update(r.params)
        base_row.update(r.metrics)
        rows.append(base_row)

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    # Перемещаем scenario в начало
    cols = ["scenario"] + [c for c in df.columns if c != "scenario"]
    return df[cols]


def scenario_results_summary(results: list[ScenarioResult], base_scenario: str = "base") -> dict[str, dict[str, float]]:
    """Сводка отклонений от базового сценария."""
    base = next((r for r in results if r.scenario_name == base_scenario), None)
    if not base:
        return {}

    summary: dict[str, dict[str, float]] = {}
    base_metrics = base.metrics

    for r in results:
        if r.scenario_name == base_scenario:
            continue
        diffs = {}
        for key, val in r.metrics.items():
            if key in base_metrics and isinstance(val, (int, float)) and isinstance(base_metrics[key], (int, float)):
                base_val = base_metrics[key]
                if base_val != 0:
                    diffs[f"{key}_abs"] = val - base_val
                    diffs[f"{key}_pct"] = (val - base_val) / abs(base_val) * 100
                else:
                    diffs[f"{key}_abs"] = val
                    diffs[f"{key}_pct"] = float("inf") if val > 0 else (float("-inf") if val < 0 else 0)
        summary[r.scenario_name] = diffs

    return summary
