"""Тесты расширенных модулей версии 2.1.0: прогноз, драйверы, эксперименты, сегментация.

Модули перенесены из прототипов в пакет `finanalyst`, поэтому проверяются
контракты, зафиксированные в `references/metric-contracts.md`: тождества
разложений, блокирующие состояния пилота и совместимость формата ответа.
"""

import numpy as np
import pandas as pd
from finanalyst import drivers, experiments, forecast, segmentation

# ---------------------------------------------------------------------------
# Прогноз
# ---------------------------------------------------------------------------

def _series(periods: int = 18) -> pd.DataFrame:
    dates = pd.date_range("2023-01-01", periods=periods, freq="MS")
    values = 1000.0 + 25.0 * np.arange(periods) + np.array([15.0, -15.0] * (periods // 2))
    return pd.DataFrame({"period": dates.strftime("%Y-%m-%d"), "interest_income": values})


def test_forecast_advanced_keeps_legacy_contract():
    result = forecast.forecast_advanced(_series(), horizon=4, method="auto")
    assert result["available"] is True
    assert len(result["periods"]) == 4
    assert set(result["scenarios"]) == {"базовый", "пессимистичный", "оптимистичный"}
    for key in ("periods", "scenarios", "band_mae", "mae", "slope_per_period", "assumption"):
        assert key in result
    assert result["method_used"] in {"linear", "ets", "arx"}


def test_forecast_band_widens_with_horizon():
    result = forecast.forecast_advanced(_series(), horizon=4, method="linear", band=1.0)
    base = result["scenarios"]["базовый"]
    pessimistic = result["scenarios"]["пессимистичный"]
    optimistic = result["scenarios"]["оптимистичный"]
    # Интервал расширяется пропорционально sqrt(h)
    assert (optimistic[0] - base[0]) < (optimistic[-1] - base[-1])
    assert (base[0] - pessimistic[0]) < (base[-1] - pessimistic[-1])


def test_forecast_reports_backtest_metrics():
    result = forecast.forecast_advanced(_series(), horizon=3, method="linear")
    assert result["mae"] is not None
    assert result["rmse"] is not None
    assert result["smape_pct"] is not None
    assert "linear" in result["backtest_metrics"]


def test_forecast_parses_russian_periods():
    frame = pd.DataFrame({"period": ["январь 2024", "февраль 2024", "март 2024"], "value": [10.0, 12.0, 14.0]})
    result = forecast.forecast_advanced(frame, horizon=2, method="linear")
    assert result["available"] is True
    assert result["history_points"] == 3


def test_forecast_needs_enough_history():
    frame = pd.DataFrame({"period": ["2024-01"], "value": [10.0]})
    result = forecast.forecast_advanced(frame, horizon=2, method="linear")
    assert result["available"] is False
    assert "3" in result["assumption"]


# ---------------------------------------------------------------------------
# Драйверы: PVM и разложение PAR
# ---------------------------------------------------------------------------

def test_pvm_effects_sum_to_total_delta():
    base = pd.DataFrame({"product_code": ["KR-01", "VD-01"], "volume": [100.0, 200.0], "revenue": [20.0, 20.0]})
    target = pd.DataFrame({"product_code": ["KR-01", "VD-01"], "volume": [150.0, 180.0], "revenue": [33.0, 19.8]})
    result = drivers.pvm_decomposition(base, target)
    assert result["available"] is True
    assert abs(sum(result["effects"].values()) - result["total_delta"]) < 0.01


def test_pvm_without_volume_is_not_available():
    base = pd.DataFrame({"product_code": ["KR-01"], "revenue": [20.0]})
    result = drivers.pvm_decomposition(base, base)
    assert result["available"] is False
    assert "volume" in result["assumption"]


def test_par_shift_effects_sum_to_delta():
    base = pd.DataFrame({"product_code": ["KR-01", "VD-01"], "balance": [100.0, 300.0], "dpd": [40, 0]})
    target = pd.DataFrame({"product_code": ["KR-01", "VD-01"], "balance": [200.0, 200.0], "dpd": [40, 0]})
    result = drivers.par_shift_decomposition(base, target, dpd_threshold=30)
    assert result["available"] is True
    total = result["within_risk_effect_pp"] + result["structural_mix_effect_pp"]
    assert abs(total - result["delta_par_pp"]) < 0.01


def test_par_shift_detects_risk_degradation_inside_product():
    """Просрочка внутри одного продукта — это риск, а не сдвиг структуры."""
    base = pd.DataFrame({"product_code": ["KR-01"], "balance": [100.0], "dpd": [0]})
    target = pd.DataFrame({"product_code": ["KR-01"], "balance": [100.0], "dpd": [60]})
    result = drivers.par_shift_decomposition(base, target, dpd_threshold=30)
    assert result["within_risk_effect_pp"] == 100.0
    assert result["structural_mix_effect_pp"] == 0.0
    assert result["delta_par_pp"] == 100.0


def test_granger_finds_leading_indicator():
    rng = np.random.default_rng(123)
    length = 30
    x_sig = np.cumsum(rng.normal(0, 1, length))
    y_sig = np.zeros(length)
    for t in range(1, length):
        y_sig[t] = y_sig[t - 1] + 0.85 * (x_sig[t - 1] - (x_sig[t - 2] if t >= 2 else 0.0)) + rng.normal(0, 0.2)
    periods = pd.date_range("2022-01-01", periods=length, freq="MS").strftime("%Y-%m-%d")
    frame = pd.concat([
        pd.DataFrame({"period": periods, "indicator": "key_rate", "value": x_sig}),
        pd.DataFrame({"period": periods, "indicator": "loan_demand", "value": y_sig}),
    ], ignore_index=True)
    result = drivers.macro_lead_lag(frame, max_lag=2)
    assert result["available"] is True
    top = result["links"].iloc[0]
    assert top["cause_indicator"] == "key_rate"
    assert top["effect_indicator"] == "loan_demand"
    assert bool(top["significant_fdr"])
    assert result["significant_count"] >= 1


def test_benjamini_hochberg_is_monotone():
    adjusted = drivers.benjamini_hochberg([0.001, 0.02, 0.5, 0.9])
    assert all(0.0 <= value <= 1.0 for value in adjusted)
    assert adjusted[0] >= 0.001


# ---------------------------------------------------------------------------
# Эксперименты: A/B-тест и DiD
# ---------------------------------------------------------------------------

def _ab_frame(n_clients: int = 200) -> pd.DataFrame:
    rng = np.random.default_rng(7)
    pre = rng.normal(100.0, 20.0, size=n_clients * 2)
    groups = ["control"] * n_clients + ["treatment"] * n_clients
    effect = np.array([0.0] * n_clients + [8.0] * n_clients)
    values = 0.8 * pre + effect + rng.normal(0.0, 10.0, size=n_clients * 2)
    return pd.DataFrame({
        "client_id": [f"C-{i}" for i in range(n_clients * 2)],
        "experiment_group": groups,
        "value": values,
        "deals": rng.integers(1, 5, size=n_clients * 2),
        "pre_value": pre,
    })


def test_ab_test_detects_effect_and_passes_srm():
    result = experiments.run_ab_test(_ab_frame(), pre_metric_col="pre_value")
    assert result["available"] is True
    assert result["recommendation"] == "launch"
    assert result["srm_check"]["srm_ok"] is True
    assert result["cuped_theta"] is not None
    assert result["mann_whitney_p_value"] is not None


def test_ab_test_blocks_on_srm_violation():
    frame = _ab_frame()
    # Сдвигаем 40 % клиентов в treatment: сплит перестаёт быть 50/50.
    clients = frame["client_id"].unique()
    moved = set(clients[: int(len(clients) * 0.4)])
    frame.loc[frame["client_id"].isin(moved), "experiment_group"] = "treatment"
    result = experiments.run_ab_test(frame)
    assert result["srm_check"]["srm_ok"] is False
    assert result["recommendation"] == "invalid_srm_detected"


def test_ab_test_marks_client_in_both_groups_as_invalid():
    """Клиент в обеих группах означает сломанную рандомизацию."""
    frame = _ab_frame()
    # Клиенту C-0 добавляем вторую сделку в другую группу.
    extra = frame.iloc[[0]].copy()
    extra["experiment_group"] = "treatment" if frame.iloc[0]["experiment_group"] == "control" else "control"
    frame = pd.concat([frame, extra], ignore_index=True)
    result = experiments.run_ab_test(frame)
    assert result["assignment_overlap_clients"] >= 1
    assert result["recommendation"] == "invalid_assignment_overlap"


def test_ab_test_ratio_metric_uses_delta_method():
    result = experiments.run_ab_test(_ab_frame(), denominator_col="deals", metric_type="ratio")
    assert result["available"] is True
    assert result["test_used"] == "delta_method_linearized_welch_t"


def test_ab_test_proportion_metric():
    frame = _ab_frame()
    frame["value"] = (frame["value"] > 100).astype(float)
    result = experiments.run_ab_test(frame, metric_type="proportion")
    assert result["test_used"] == "two_proportion_z_test"
    assert result["p_value"] is not None


def test_did_recovers_treatment_effect_and_checks_pretrends():
    rows = []
    for index, date in enumerate(["2024-01-01", "2024-02-01", "2024-03-01", "2024-04-01"]):
        for region in ("Москва", "Казань", "Новосибирск", "Самара"):
            treated = region in {"Москва", "Казань"}
            post = index >= 2
            value = (100.0 + 5.0 * index + (15.0 if treated else 0.0)
                     + (25.0 if (treated and post) else 0.0))
            rows.append({"region": region, "period": date, "value": value})
    result = experiments.estimate_did(
        pd.DataFrame(rows), treated_units=["Москва", "Казань"], intervention_period="2024-03-01")
    assert result["available"] is True
    assert abs(result["att_effect"] - 25.0) < 1.0
    assert result["parallel_trends_ok"] is True


def test_did_rejects_missing_control_group():
    frame = pd.DataFrame({
        "region": ["Москва"] * 4,
        "period": ["2024-01-01", "2024-02-01", "2024-03-01", "2024-04-01"],
        "value": [1.0, 2.0, 3.0, 4.0],
    })
    result = experiments.estimate_did(frame, treated_units=["Москва"], intervention_period="2024-03-01")
    assert result["available"] is False


# ---------------------------------------------------------------------------
# Сегментация
# ---------------------------------------------------------------------------

def _client_data() -> tuple[pd.DataFrame, pd.DataFrame]:
    clients = pd.DataFrame({"client_id": [f"C-{i}" for i in range(1, 31)]})
    portfolio = pd.DataFrame({
        "account_id": [f"AC-{i}" for i in range(1, 26)],
        "client_id": [f"C-{i}" for i in range(1, 26)],
        "product_code": ["KR-01" if i % 2 == 0 else "VD-01" for i in range(1, 26)],
        "open_date": ["2024-01-15"] * 25,
        "balance": [float(i * 10000) for i in range(1, 26)],
        "dpd": [35 if i == 25 else 0 for i in range(1, 26)],
    })
    return clients, portfolio


def test_segmentation_profiles_cover_all_clients():
    clients, portfolio = _client_data()
    result = segmentation.segment_clients(clients, portfolio, reference_date="2024-06-01")
    assert result["available"] is True
    assert int(result["profiles"]["clients_count"].sum()) == 30
    assert abs(float(result["profiles"]["clients_share_pct"].sum()) - 100.0) < 0.1


def test_segmentation_marks_delinquent_clients():
    clients, portfolio = _client_data()
    result = segmentation.segment_clients(clients, portfolio, reference_date="2024-06-01")
    delinquent = result["client_assignments"][result["client_assignments"]["max_dpd"] > 0]
    assert len(delinquent) == 1
    assert delinquent["rfm_segment"].iloc[0] == "Просроченные (DPD > 0)"


def test_segmentation_clustering_is_deterministic():
    clients, portfolio = _client_data()
    first = segmentation.segment_clients(clients, portfolio, reference_date="2024-06-01")
    second = segmentation.segment_clients(clients, portfolio, reference_date="2024-06-01")
    assert first["client_assignments"]["cluster_id"].tolist() == second["client_assignments"]["cluster_id"].tolist()
    assert first["optimal_k"] == second["optimal_k"]


def test_segmentation_without_clustering_has_single_cluster():
    clients, portfolio = _client_data()
    result = segmentation.segment_clients(clients, portfolio, reference_date="2024-06-01",
                                          enable_clustering=False)
    assert set(result["client_assignments"]["cluster_id"]) == {"CL-1"}


def test_segmentation_needs_portfolio():
    clients, _ = _client_data()
    result = segmentation.segment_clients(clients, pd.DataFrame())
    assert result["available"] is False
