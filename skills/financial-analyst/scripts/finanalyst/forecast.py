"""Каскадное прогнозирование временных рядов с walk-forward бэктестом.

Модуль расширяет `metrics.forecast_trend` (линейный тренд) моделями Damped ETS
(Хольт — Уинтерс с затухающим трендом) и ARX с экзогенными драйверами.
Модель выбирается на скользящем бэктесте по MAE; дополнительно считаются
RMSE, sMAPE и MASE — MAPE не определён при нулевых фактических значениях и
поэтому не используется для выбора модели.

Контракт ответа совместим с `metrics.forecast_trend`: те же ключи `available`,
`periods`, `scenarios`, плюс `method_used`, `backtest_metrics`, `rmse`, `smape_pct`,
`mase`. Разбор периодов — через `core.parse_period`, поэтому понимаются
«2024-05», «1к24», «январь 2024» и даты Excel.
"""

from __future__ import annotations

from typing import Literal

import numpy as np
import pandas as pd

from .core import parse_period

try:  # scipy нужен только для оптимизации параметров ETS
    from scipy import optimize
except ImportError:  # pragma: no cover - зависит от окружения
    optimize = None


def _to_datetime(values) -> pd.Series:
    """Разобрать значения периода в даты через общий парсер пакета."""
    parsed = []
    for value in values:
        ts, _ = parse_period(value)
        parsed.append(pd.NaT if pd.isna(ts) else ts)
    return pd.Series(parsed, index=range(len(parsed)), dtype="datetime64[ns]")


def _fit_predict_linear(y_train: np.ndarray, h: int) -> np.ndarray:
    n = len(y_train)
    if n == 1:
        return np.repeat(float(y_train[0]), h)
    x = np.arange(n, dtype=float)
    slope, intercept = np.polyfit(x, y_train, 1)
    future_x = np.arange(n, n + h, dtype=float)
    return intercept + slope * future_x


def _fit_predict_damped_holt(
    y_train: np.ndarray,
    h: int,
    seasonal_period: int | None = None,
) -> np.ndarray:
    """Аддитивное экспоненциальное сглаживание с затухающим трендом (Damped ETS)."""
    n = len(y_train)
    if n < 3:
        return _fit_predict_linear(y_train, h)

    use_seasonal = seasonal_period is not None and n >= 2 * seasonal_period
    m = int(seasonal_period) if use_seasonal else 1

    def _simulate(params: np.ndarray) -> tuple[np.ndarray, float, float, np.ndarray]:
        alpha, beta, phi = params[0], params[1], params[2]
        gamma = params[3] if use_seasonal else 0.0
        if use_seasonal:
            level = float(np.mean(y_train[:m]))
            trend = float((np.mean(y_train[m: 2 * m]) - np.mean(y_train[:m])) / m)
            seasonals = np.array(y_train[:m] - level, dtype=float)
        else:
            level = float(y_train[0])
            trend = float(y_train[1] - y_train[0])
            seasonals = np.zeros(1, dtype=float)

        fitted = np.zeros(n, dtype=float)
        for t in range(n):
            s_prev = seasonals[t % m] if use_seasonal else 0.0
            fitted[t] = level + phi * trend + s_prev
            err = y_train[t] - fitted[t]
            prev_level = level
            level = prev_level + phi * trend + alpha * err
            trend = phi * trend + beta * alpha * err
            if use_seasonal:
                seasonals[t % m] = s_prev + gamma * (1.0 - alpha) * err
        return fitted, level, trend, seasonals

    def _loss(params: np.ndarray) -> float:
        fitted, _, _, _ = _simulate(params)
        return float(np.mean((y_train - fitted) ** 2))

    if optimize is None:
        # Без scipy параметры ETS фиксируются: метод не оптимизируется, но
        # прогноз остаётся детерминированным и пригодным для сравнения.
        params = np.array([0.4, 0.15, 0.9, 0.2] if use_seasonal else [0.4, 0.15, 0.9])
    else:
        init = np.array([0.4, 0.15, 0.9, 0.2] if use_seasonal else [0.4, 0.15, 0.9])
        bounds = (
            [(0.05, 0.95), (0.01, 0.5), (0.8, 0.99), (0.01, 0.6)]
            if use_seasonal
            else [(0.05, 0.95), (0.01, 0.5), (0.8, 0.99)]
        )
        res = optimize.minimize(_loss, init, bounds=bounds, method="L-BFGS-B")
        params = res.x
    _, level, trend, seasonals = _simulate(params)
    phi = float(params[2])

    preds = np.zeros(h, dtype=float)
    phi_sum = 0.0
    for step in range(1, h + 1):
        phi_sum += phi**step
        s_val = seasonals[(n + step - 1) % m] if use_seasonal else 0.0
        preds[step - 1] = level + phi_sum * trend + s_val
    return preds


def _fit_predict_arx(
    y_train: np.ndarray,
    h: int,
    exog_train: np.ndarray | None = None,
    exog_future: np.ndarray | None = None,
) -> np.ndarray:
    """Авторегрессия с трендом и экзогенными драйверами (ARX(1) с гребневой регуляризацией)."""
    n = len(y_train)
    if n < 5:
        return _fit_predict_linear(y_train, h)

    y_target = y_train[1:]
    y_lag1 = y_train[:-1]
    t_idx = np.arange(1, n, dtype=float)

    features = [np.ones(n - 1), t_idx, y_lag1]
    has_exog = (
        exog_train is not None
        and exog_future is not None
        and len(exog_train) == n
        and len(exog_future) >= h
    )
    if has_exog:
        features.append(np.asarray(exog_train[1:], dtype=float))

    X = np.column_stack(features)
    reg = 1e-4 * np.eye(X.shape[1])
    reg[0, 0] = 0.0
    weights = np.linalg.pinv(X.T @ X + reg) @ (X.T @ y_target)

    preds = np.zeros(h, dtype=float)
    last_y = float(y_train[-1])
    for step in range(h):
        row = [1.0, float(n + step), last_y]
        if has_exog:
            row.extend(np.asarray(exog_future[step], dtype=float).ravel().tolist())
        pred = float(np.dot(np.asarray(row, dtype=float), weights))
        preds[step] = pred
        last_y = pred
    return preds


def _compute_metrics(actual: np.ndarray, predicted: np.ndarray, y_insample: np.ndarray) -> dict:
    """MAE, RMSE, sMAPE, MASE и MAPE на бэктесте.

    MAPE возвращается ``None``, если хотя бы один факт нулевой: в финансовых
    рядах около нуля MAPE не определён и для выбора модели не используется.
    """
    errors = np.abs(actual - predicted)
    mae = float(np.mean(errors))
    rmse = float(np.sqrt(np.mean((actual - predicted) ** 2)))

    denom_smape = np.abs(actual) + np.abs(predicted)
    valid_smape = denom_smape > 1e-9
    smape = (
        float(np.mean(2.0 * errors[valid_smape] / denom_smape[valid_smape]) * 100.0)
        if np.any(valid_smape)
        else None
    )

    naive_scale = float(np.mean(np.abs(np.diff(y_insample)))) if len(y_insample) > 1 else 0.0
    mase = float(mae / naive_scale) if naive_scale > 1e-9 else None

    non_zero = np.abs(actual) > 1e-6
    mape = (
        float(np.mean(errors[non_zero] / np.abs(actual[non_zero])) * 100.0)
        if np.all(non_zero) and np.any(non_zero)
        else None
    )

    return {
        "mae": round(mae, 4),
        "rmse": round(rmse, 4),
        "smape_pct": round(smape, 2) if smape is not None else None,
        "mase": round(mase, 4) if mase is not None else None,
        "mape_pct": round(mape, 2) if mape is not None else None,
    }


def forecast_advanced(
    series: pd.DataFrame,
    value_col: str = "value",
    period_col: str = "period_start",
    horizon: int = 4,
    band: float = 1.0,
    method: Literal["auto", "linear", "ets", "arx"] = "auto",
    exog_cols: list[str] | None = None,
    future_exog: pd.DataFrame | None = None,
) -> dict:
    """Построить каскадный прогноз временного ряда с выбором модели на бэктесте.

    Возвращает словарь, совместимый с `metrics.forecast_trend`, дополненный
    полями `method_used`, `backtest_metrics`, `rmse`, `smape_pct` и `mase`.
    """
    if series is None or series.empty:
        return {"available": False, "assumption": "Входная таблица временного ряда пуста."}

    df = series.copy()
    if period_col not in df.columns:
        for alt in ("period", "period_ym", "report_date", "date"):
            if alt in df.columns:
                period_col = alt
                break

    if value_col not in df.columns and "interest_income" in df.columns:
        df["_val"] = pd.to_numeric(df["interest_income"], errors="coerce").fillna(0.0)
        if "fee_income" in df.columns:
            df["_val"] += pd.to_numeric(df["fee_income"], errors="coerce").fillna(0.0)
        if "premium" in df.columns:
            df["_val"] += pd.to_numeric(df["premium"], errors="coerce").fillna(0.0)
        value_col = "_val"

    if period_col not in df.columns or value_col not in df.columns:
        return {
            "available": False,
            "assumption": f"Отсутствуют обязательные колонки ({period_col}, {value_col}).",
        }

    df["_dt"] = _to_datetime(df[period_col].to_numpy())
    df["_y"] = pd.to_numeric(df[value_col], errors="coerce")
    df = df.dropna(subset=["_dt", "_y"]).sort_values("_dt")

    agg_dict: dict[str, str] = {"_y": "sum"}
    valid_exog = [c for c in (exog_cols or []) if c in df.columns]
    for column in valid_exog:
        df[column] = pd.to_numeric(df[column], errors="coerce").fillna(0.0)
        agg_dict[column] = "mean"

    agg = df.groupby("_dt", as_index=False).agg(agg_dict).sort_values("_dt")
    y = agg["_y"].to_numpy(dtype=float)
    dates = agg["_dt"].tolist()
    n = len(y)

    if n < 3:
        return {
            "available": False,
            "history_points": int(n),
            "assumption": "Для построения прогноза и бэктеста требуется минимум 3 исторических периода.",
        }

    gaps_days = np.diff(agg["_dt"].to_numpy()).astype("timedelta64[D]").astype(float)
    median_gap = float(np.median(gaps_days)) if len(gaps_days) else 30.0
    if median_gap > 300:
        freq_offset = pd.DateOffset(years=1)
        seasonal_p = None
    elif median_gap > 150:
        freq_offset = pd.DateOffset(months=6)
        seasonal_p = 2
    elif median_gap > 60:
        freq_offset = pd.DateOffset(months=3)
        seasonal_p = 4
    elif median_gap > 20:
        freq_offset = pd.DateOffset(months=1)
        seasonal_p = 12
    else:
        freq_offset = pd.DateOffset(days=int(max(1.0, round(median_gap))))
        seasonal_p = 7

    exog_mat = agg[valid_exog].to_numpy(dtype=float) if valid_exog else None
    exog_fut_mat = (
        future_exog[valid_exog].to_numpy(dtype=float)
        if (valid_exog and future_exog is not None
            and all(c in future_exog.columns for c in valid_exog))
        else None
    )

    candidate_methods: list[str]
    if method != "auto":
        candidate_methods = [method]
    else:
        candidate_methods = ["linear"]
        if n >= 6:
            candidate_methods.append("ets")
        if n >= 8 or (exog_mat is not None and exog_fut_mat is not None):
            candidate_methods.append("arx")

    folds = min(4, max(1, n // 3))
    start_train = n - folds
    # Сезонность и ARX на бэктесте должны быть той же моделью, что и финальный
    # прогноз. Иначе MAE выбирает несезонный ETS, а в отчёт уходит сезонный.
    if seasonal_p and start_train < 2 * seasonal_p:
        seasonal_p = None
    if method == "auto" and "arx" in candidate_methods and start_train < 5:
        candidate_methods.remove("arx")

    def _run_candidate(name: str, train_y: np.ndarray, step_h: int, idx_end: int) -> np.ndarray:
        if name == "ets":
            return _fit_predict_damped_holt(train_y, step_h, seasonal_period=seasonal_p)
        if name == "arx":
            ex_tr = exog_mat[:idx_end] if exog_mat is not None else None
            ex_fu = (
                exog_mat[idx_end: idx_end + step_h]
                if (exog_mat is not None and idx_end + step_h <= n)
                else exog_fut_mat
            )
            return _fit_predict_arx(train_y, step_h, ex_tr, ex_fu)
        return _fit_predict_linear(train_y, step_h)

    model_scores: dict[str, dict] = {}
    for name in candidate_methods:
        preds_bt: list[float] = []
        actuals_bt: list[float] = []
        for split_idx in range(start_train, n):
            pred_1 = _run_candidate(name, y[:split_idx], 1, split_idx)[0]
            preds_bt.append(pred_1)
            actuals_bt.append(y[split_idx])
        model_scores[name] = _compute_metrics(
            np.asarray(actuals_bt, dtype=float),
            np.asarray(preds_bt, dtype=float),
            y[:start_train],
        )

    best_method = min(candidate_methods, key=lambda name: float(model_scores[name]["mae"] or 1e18))
    best_metrics = model_scores[best_method]

    if best_method == "ets":
        base_forecast = _fit_predict_damped_holt(y, horizon, seasonal_period=seasonal_p)
    elif best_method == "arx":
        base_forecast = _fit_predict_arx(y, horizon, exog_mat, exog_fut_mat)
    else:
        base_forecast = _fit_predict_linear(y, horizon)

    mae_val = float(best_metrics["mae"] or 0.0)
    delta = float(band * mae_val)
    slope, _ = np.polyfit(np.arange(n, dtype=float), y, 1)

    last_dt = pd.Timestamp(dates[-1])
    future_dates = [last_dt + freq_offset * (i + 1) for i in range(horizon)]

    # Интервал расширяется пропорционально sqrt(h): ошибка накапливается с горизонтом.
    pessimistic = [round(float(base_forecast[i] - delta * np.sqrt(i + 1)), 2) for i in range(horizon)]
    baseline = [round(float(base_forecast[i]), 2) for i in range(horizon)]
    optimistic = [round(float(base_forecast[i] + delta * np.sqrt(i + 1)), 2) for i in range(horizon)]

    scipy_note = ""
    if optimize is None and "ets" in candidate_methods:
        scipy_note = " scipy не установлен: параметры ETS зафиксированы и не оптимизировались."

    return {
        "available": True,
        "method_used": best_method,
        "periods": [p.strftime("%Y-%m-%d") for p in future_dates],
        "scenarios": {
            "пессимистичный": pessimistic,
            "базовый": baseline,
            "оптимистичный": optimistic,
        },
        "band_mae": round(delta, 4),
        "mae": round(mae_val, 3),
        "rmse": best_metrics["rmse"],
        "smape_pct": best_metrics["smape_pct"],
        "mase": best_metrics["mase"],
        "mape_pct": best_metrics["mape_pct"],
        "backtest_metrics": model_scores,
        "slope_per_period": round(float(slope), 4),
        "history_points": int(n),
        "assumption": (
            f"Модель '{best_method}' выбрана по минимуму MAE ({mae_val:.2f}, "
            f"sMAPE={best_metrics['smape_pct']}%) на скользящем бэктесте ({folds} фолда). "
            f"Интервал сценариев: ±{band}*MAE*sqrt(h). "
            "RMSE, sMAPE и MASE считаются, но модель по ним не выбирается."
            f"{scipy_note}"
        ),
    }
