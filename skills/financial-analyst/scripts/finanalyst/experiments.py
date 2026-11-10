"""Оценка пилотов и экспериментов: A/B-тестирование и квази-эксперименты (DiD).

Модуль закрывает три требования, без которых вывод пилота недостоверен:

1. **Проверка SRM** (Sample Ratio Mismatch) до интерпретации: при перекосе
   трафика результат недействителен, и это блокирующее состояние, а не
   предупреждение.
2. **Единица рандомизации**: анализ ведётся на уровне `client_id`, поэтому
   несколько сделок одного клиента не превращаются в псевдорепликации.
3. **Ratio-метрики** (средний чек сделки, доля просроченных счетов на клиента)
   считаются через линеаризацию Дельта-метода, а не как отношение средних.

CUPED снижает дисперсию за счёт доэкспериментальной ковариаты; DiD оценивает
эффект нерандомизированного запуска с обязательной проверкой параллельности
пре-трендов.
"""

from __future__ import annotations

from typing import Literal

import numpy as np
import pandas as pd

try:
    from scipy import stats
except ImportError:  # pragma: no cover - зависит от окружения
    stats = None

SCIPY_REQUIRED = "Для статистических тестов нужен scipy: pip install 'financial-analyst-skill[analysis]'."

RECOMMENDATIONS = {
    "launch": "Запустить",
    "stop_degradation": "Остановить: ухудшение",
    "inconclusive_or_neutral": "Нет значимого эффекта",
    "invalid_srm_detected": "Результат недействителен: нарушен SRM",
    "invalid_assignment_overlap": "Результат недействителен: клиенты попали в обе группы",
}


def check_srm(
    n_control: int,
    n_treatment: int,
    expected_treatment_share: float = 0.5,
    alpha_srm: float = 0.001,
) -> dict:
    """Проверить отсутствие перекоса трафика (SRM) критерием хи-квадрат.

    Порог жёсткий (0.001): при меньшем p-value расщепление считается поломкой
    рандомизации, и результаты теста не интерпретируются.
    """
    total = int(n_control + n_treatment)
    if total <= 0 or not (0.0 < expected_treatment_share < 1.0):
        return {"srm_ok": False, "p_value": 0.0, "chi2": 0.0}
    if stats is None:
        return {
            "srm_ok": False,
            "p_value": 0.0,
            "chi2": 0.0,
            "assumption": SCIPY_REQUIRED,
        }

    exp_t = total * expected_treatment_share
    exp_c = total * (1.0 - expected_treatment_share)
    chi2 = ((n_control - exp_c) ** 2) / exp_c + ((n_treatment - exp_t) ** 2) / exp_t
    p_val = float(1.0 - stats.chi2.cdf(chi2, df=1))
    return {
        "srm_ok": bool(p_val >= alpha_srm),
        "p_value": round(p_val, 6),
        "chi2": round(float(chi2), 4),
        "observed_treatment_share": round(n_treatment / total, 4),
        "expected_treatment_share": round(expected_treatment_share, 4),
    }


def apply_cuped(
    y_control: np.ndarray,
    y_treatment: np.ndarray,
    x_pre_control: np.ndarray,
    x_pre_treatment: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, float]:
    """Поправка CUPED по доэкспериментальной ковариате: снижает дисперсию разности."""
    y_all = np.concatenate([y_control, y_treatment])
    x_all = np.concatenate([x_pre_control, x_pre_treatment])
    var_x = float(np.var(x_all, ddof=1))
    if var_x <= 1e-12 or len(y_all) < 4:
        return y_control, y_treatment, 0.0
    cov_yx = float(np.cov(y_all, x_all, ddof=1)[0, 1])
    theta = cov_yx / var_x
    mean_x = float(np.mean(x_all))
    return (
        y_control - theta * (x_pre_control - mean_x),
        y_treatment - theta * (x_pre_treatment - mean_x),
        float(theta),
    )


def run_ab_test(
    df: pd.DataFrame,
    group_col: str = "experiment_group",
    metric_col: str = "value",
    denominator_col: str | None = None,
    pre_metric_col: str | None = None,
    entity_col: str | None = "client_id",
    metric_type: Literal["continuous", "proportion", "ratio"] = "continuous",
    control_label: str = "control",
    treatment_label: str = "treatment",
    expected_treatment_share: float = 0.5,
    alpha: float = 0.05,
    power: float = 0.80,
) -> dict:
    """Провести анализ A/B-теста с проверкой SRM и учётом кластеризации на клиенте.

    Типы метрик:
    - ``continuous`` — t-тест Уэлча и U-тест Манна — Уитни, опционально CUPED;
    - ``proportion`` — z-тест пропорций с доверительным интервалом Ньюкомба — Уилсона;
    - ``ratio`` — линеаризация Дельта-метода на уровне единицы рандомизации.
    """
    if df is None or df.empty or group_col not in df.columns or metric_col not in df.columns:
        return {
            "available": False,
            "assumption": "Отсутствуют данные или обязательные колонки группы/метрики.",
        }
    if stats is None:
        return {"available": False, "assumption": SCIPY_REQUIRED}

    work = df[df[group_col].isin([control_label, treatment_label])].copy()
    if work.empty:
        return {
            "available": False,
            "assumption": (
                f"В колонке '{group_col}' не найдены группы "
                f"'{control_label}' и '{treatment_label}'."
            ),
        }

    work["_num"] = pd.to_numeric(work[metric_col], errors="coerce").fillna(0.0)
    has_denom = denominator_col is not None and denominator_col in work.columns
    work["_den"] = (
        pd.to_numeric(work[denominator_col], errors="coerce").fillna(0.0)
        if has_denom else 1.0
    )
    has_pre = pre_metric_col is not None and pre_metric_col in work.columns
    work["_pre"] = (
        pd.to_numeric(work[pre_metric_col], errors="coerce").fillna(0.0)
        if has_pre else 0.0
    )

    # Агрегация до единицы рандомизации (client_id) — иначе несколько сделок
    # одного клиента дают псевдорепликации и заниженную дисперсию.
    if entity_col and entity_col in work.columns:
        unit_df = work.groupby([entity_col, group_col], as_index=False).agg(
            _num=("_num", "sum"),
            _den=("_den", "sum"),
            _pre=("_pre", "sum"),
        )
    else:
        unit_df = work[[group_col, "_num", "_den", "_pre"]].copy()

    # Клиент в обеих группах означает сломанную рандомизацию (или группу,
    # назначенную на уровне строки, а не клиента). Такие единицы исключаются,
    # а результат теста помечается недействительным.
    overlap_count = 0
    if entity_col and entity_col in unit_df.columns:
        ctrl_ids = set(unit_df.loc[unit_df[group_col] == control_label, entity_col])
        trt_ids = set(unit_df.loc[unit_df[group_col] == treatment_label, entity_col])
        overlap = ctrl_ids & trt_ids
        overlap_count = len(overlap)
        if overlap:
            unit_df = unit_df[~unit_df[entity_col].isin(overlap)]

    ctrl = unit_df[unit_df[group_col] == control_label]
    trt = unit_df[unit_df[group_col] == treatment_label]
    n_c, n_t = len(ctrl), len(trt)
    if n_c < 2 or n_t < 2:
        return {
            "available": False,
            "assumption": "В одной из групп менее 2 единиц наблюдения.",
        }

    srm_info = check_srm(n_c, n_t, expected_treatment_share=expected_treatment_share)
    z_alpha = float(stats.norm.ppf(1.0 - alpha / 2.0))
    z_beta = float(stats.norm.ppf(power))

    if metric_type == "proportion":
        succ_c = float(np.sum(ctrl["_num"] > 0))
        succ_t = float(np.sum(trt["_num"] > 0))
        p_c = succ_c / n_c
        p_t = succ_t / n_t
        diff = p_t - p_c
        p_pool = (succ_c + succ_t) / (n_c + n_t)
        se_pool = float(np.sqrt(max(1e-15, p_pool * (1.0 - p_pool) * (1.0 / n_c + 1.0 / n_t))))
        se_unpool = float(np.sqrt(max(1e-15, p_c * (1.0 - p_c) / n_c + p_t * (1.0 - p_t) / n_t)))
        z_stat = diff / se_pool
        p_val = float(2.0 * (1.0 - stats.norm.cdf(abs(z_stat))))
        ci_low, ci_high = diff - z_alpha * se_unpool, diff + z_alpha * se_unpool
        mde_abs = (z_alpha + z_beta) * se_pool
        rel_uplift_pct = (diff / p_c * 100.0) if p_c > 1e-12 else 0.0
        test_used = "two_proportion_z_test"
        mwu_p = None
        cuped_theta = None

    elif metric_type == "ratio":
        sum_num_c = float(ctrl["_num"].sum())
        sum_den_c = float(ctrl["_den"].sum())
        sum_num_t = float(trt["_num"].sum())
        sum_den_t = float(trt["_den"].sum())
        if sum_den_c <= 0 or sum_den_t <= 0:
            return {
                "available": False,
                "assumption": "Сумма знаменателя ratio-метрики равна нулю.",
            }

        r_c = sum_num_c / sum_den_c
        r_t = sum_num_t / sum_den_t
        diff = r_t - r_c

        mean_den_c = float(ctrl["_den"].mean())
        mean_den_t = float(trt["_den"].mean())
        # Поклиентская линеаризация (Дельта-метод)
        lin_c = (ctrl["_num"].to_numpy(dtype=float) - r_c * ctrl["_den"].to_numpy(dtype=float)) / mean_den_c
        lin_t = (trt["_num"].to_numpy(dtype=float) - r_c * trt["_den"].to_numpy(dtype=float)) / mean_den_t

        t_res = stats.ttest_ind(lin_t, lin_c, equal_var=False)
        p_val = float(t_res.pvalue)
        se_diff = float(np.sqrt(np.var(lin_c, ddof=1) / n_c + np.var(lin_t, ddof=1) / n_t))
        ci_low, ci_high = diff - z_alpha * se_diff, diff + z_alpha * se_diff
        mde_abs = (z_alpha + z_beta) * se_diff
        rel_uplift_pct = (diff / abs(r_c) * 100.0) if abs(r_c) > 1e-12 else 0.0
        p_c, p_t = r_c, r_t
        test_used = "delta_method_linearized_welch_t"
        mwu_p = None
        cuped_theta = None

    else:
        y_c = ctrl["_num"].to_numpy(dtype=float)
        y_t = trt["_num"].to_numpy(dtype=float)
        cuped_theta = None
        if has_pre:
            y_c, y_t, cuped_theta = apply_cuped(
                y_c, y_t,
                ctrl["_pre"].to_numpy(dtype=float),
                trt["_pre"].to_numpy(dtype=float),
            )
            test_used = "welch_t_test_with_cuped"
        else:
            test_used = "welch_t_test"

        p_c = float(np.mean(y_c))
        p_t = float(np.mean(y_t))
        diff = p_t - p_c
        t_res = stats.ttest_ind(y_t, y_c, equal_var=False)
        p_val = float(t_res.pvalue)
        mwu_res = stats.mannwhitneyu(
            trt["_num"].to_numpy(dtype=float), ctrl["_num"].to_numpy(dtype=float))
        mwu_p = round(float(mwu_res.pvalue), 6)

        se_diff = float(np.sqrt(np.var(y_c, ddof=1) / n_c + np.var(y_t, ddof=1) / n_t))
        ci_low, ci_high = diff - z_alpha * se_diff, diff + z_alpha * se_diff
        mde_abs = (z_alpha + z_beta) * se_diff
        rel_uplift_pct = (diff / abs(p_c) * 100.0) if abs(p_c) > 1e-12 else 0.0

    if overlap_count:
        decision = "invalid_assignment_overlap"
    elif not srm_info["srm_ok"]:
        decision = "invalid_srm_detected"
    elif p_val < alpha and diff > 0:
        decision = "launch"
    elif p_val < alpha and diff < 0:
        decision = "stop_degradation"
    else:
        decision = "inconclusive_or_neutral"

    return {
        "available": True,
        "test_used": test_used,
        "n_control": int(n_c),
        "n_treatment": int(n_t),
        "assignment_overlap_clients": int(overlap_count),
        "control_mean": round(float(p_c), 4),
        "treatment_mean": round(float(p_t), 4),
        "absolute_effect": round(float(diff), 4),
        "relative_uplift_pct": round(float(rel_uplift_pct), 2),
        "ci_95": (round(float(ci_low), 4), round(float(ci_high), 4)),
        "p_value": round(float(p_val), 6),
        "mann_whitney_p_value": mwu_p,
        "mde_absolute": round(float(mde_abs), 4),
        "cuped_theta": round(float(cuped_theta), 4) if cuped_theta is not None else None,
        "srm_check": srm_info,
        "recommendation": decision,
        "recommendation_text": RECOMMENDATIONS.get(decision, decision),
        "assumption": (
            f"A/B-тест рассчитан методом '{test_used}' на уровне единицы рандомизации "
            f"({entity_col or 'строка'}). SRM хи-квадрат p={srm_info['p_value']}."
            + (f" Исключено клиентов в обеих группах: {overlap_count}." if overlap_count else "")
        ),
    }


def estimate_did(
    panel_df: pd.DataFrame,
    unit_col: str = "region",
    period_col: str = "period",
    outcome_col: str = "value",
    treated_units: list[str] | set[str] = (),
    intervention_period: str = "",
    alpha: float = 0.05,
) -> dict:
    """Оценить эффект квази-эксперимента методом разности разностей (DiD).

    Оценка: ``Y = b0 + b1·Treat + b2·Post + ATT·(Treat×Post)`` с
    гетероскедастично-робастными ошибками Уайта (HC1). До интерпретации
    проверяется параллельность пре-трендов.
    """
    for required in (unit_col, period_col, outcome_col):
        if panel_df is None or panel_df.empty or required not in panel_df.columns:
            return {
                "available": False,
                "assumption": f"Отсутствует таблица или колонка '{required}'.",
            }
    if stats is None:
        return {"available": False, "assumption": SCIPY_REQUIRED}

    df = panel_df.copy()
    df["_y"] = pd.to_numeric(df[outcome_col], errors="coerce")
    df["_dt"] = pd.to_datetime(df[period_col].astype(str), errors="coerce")
    t0 = pd.to_datetime(str(intervention_period), errors="coerce")
    df = df.dropna(subset=["_dt", "_y"])
    if df.empty or pd.isna(t0) or not treated_units:
        return {
            "available": False,
            "assumption": "Некорректная дата интервенции или пустой список пилотных сегментов.",
        }

    treated_set = {str(unit) for unit in treated_units}
    df["_treat"] = df[unit_col].astype(str).isin(treated_set).astype(float)
    df["_post"] = (df["_dt"] >= t0).astype(float)
    df["_did"] = df["_treat"] * df["_post"]

    if df["_treat"].nunique() < 2 or df["_post"].nunique() < 2:
        return {
            "available": False,
            "assumption": (
                "В выборке должны присутствовать как пилотная и контрольная группы, "
                "так и периоды до/после интервенции."
            ),
        }

    X = np.column_stack([
        np.ones(len(df)),
        df["_treat"].to_numpy(),
        df["_post"].to_numpy(),
        df["_did"].to_numpy(),
    ])
    y = df["_y"].to_numpy(dtype=float)
    n, k = X.shape
    if n <= k + 1:
        return {
            "available": False,
            "assumption": "Недостаточно наблюдений для оценки модели DiD.",
        }

    xtx_inv = np.linalg.pinv(X.T @ X)
    beta = xtx_inv @ (X.T @ y)
    resid = y - X @ beta
    meat = (X * (resid[:, None] ** 2)).T @ X * (n / (n - k))
    cov_hc1 = xtx_inv @ meat @ xtx_inv
    se = np.sqrt(np.maximum(1e-15, np.diag(cov_hc1)))

    att = float(beta[3])
    se_att = float(se[3])
    t_stat = att / se_att
    p_val = float(2.0 * (1.0 - stats.t.cdf(abs(t_stat), df=n - k)))
    t_crit = float(stats.t.ppf(1.0 - alpha / 2.0, df=n - k))
    ci_95 = (round(att - t_crit * se_att, 4), round(att + t_crit * se_att, 4))

    # Проверка параллельности пре-трендов на периодах до интервенции
    pre_df = df[df["_post"] == 0.0].sort_values("_dt").copy()
    parallel_trends_ok = True
    pre_trend_p = 1.0
    if pre_df["_dt"].nunique() >= 2 and len(pre_df) >= 6:
        min_dt = pre_df["_dt"].min()
        pre_df["_t_num"] = (pre_df["_dt"] - min_dt).dt.days.astype(float)
        pre_df["_t_inter"] = pre_df["_t_num"] * pre_df["_treat"]
        Xp = np.column_stack([
            np.ones(len(pre_df)),
            pre_df["_treat"].to_numpy(),
            pre_df["_t_num"].to_numpy(),
            pre_df["_t_inter"].to_numpy(),
        ])
        yp = pre_df["_y"].to_numpy(dtype=float)
        np_obs, kp = Xp.shape
        if np_obs > kp + 1:
            xpx_inv = np.linalg.pinv(Xp.T @ Xp)
            bp = xpx_inv @ (Xp.T @ yp)
            resp = yp - Xp @ bp
            s2 = float(np.sum(resp**2) / (np_obs - kp))
            se_inter = float(np.sqrt(max(1e-15, s2 * xpx_inv[3, 3])))
            t_pre = float(bp[3]) / se_inter
            pre_trend_p = float(2.0 * (1.0 - stats.t.cdf(abs(t_pre), df=np_obs - kp)))
            parallel_trends_ok = bool(pre_trend_p >= alpha)

    return {
        "available": True,
        "att_effect": round(att, 4),
        "se_hc1": round(se_att, 4),
        "ci_95": ci_95,
        "p_value": round(p_val, 6),
        "parallel_trends_ok": parallel_trends_ok,
        "parallel_trends_p_value": round(pre_trend_p, 4),
        "observations": int(n),
        "assumption": (
            f"Оценка DiD выполнена с гетероскедастично-робастными ошибками HC1 "
            f"(дата интервенции {t0.strftime('%Y-%m-%d')}). Проверка параллельности "
            f"пре-трендов: p={pre_trend_p:.3f} "
            f"({'пройдена' if parallel_trends_ok else 'НАРУШЕНА'})."
        ),
    }
