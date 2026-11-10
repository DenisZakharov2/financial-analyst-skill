"""Факторный (драйверный) анализ изменений: PVM-декомпозиция, сдвиг PAR, лаговая причинность.

Модуль отвечает на вопрос «почему изменилось»: разлагает изменение выручки или
выдач на эффекты объёма, продуктового микса и ставки (Price-Volume-Mix),
разлагает изменение доли просрочки PAR на деградацию риска внутри продуктов и
сдвиг структуры портфеля (симметричное разложение Шепли), а также оценивает
опережающие связи между макропоказателями (F-тест Грейнджера с поправкой
Бенджамини — Хохберга).

Все разложения — тождества: сумма эффектов строго равна общему отклонению.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

try:
    from scipy import stats
except ImportError:  # pragma: no cover - зависит от окружения
    stats = None

SCIPY_REQUIRED = "Для лагового теста Грейнджера нужен scipy: pip install 'financial-analyst-skill[analysis]'."


def pvm_decomposition(
    df_base: pd.DataFrame,
    df_target: pd.DataFrame,
    segment_col: str = "product_code",
    volume_col: str = "volume",
    value_col: str = "revenue",
) -> dict:
    """Разложить изменение выручки (дохода, выдач) на эффекты объёма, микса и ставки.

    Тождество без нераспределённого остатка::

        ΔTotal = Volume Effect + Mix Effect + Rate (Price) Effect + Interaction Effect
    """
    if df_base is None or df_target is None or df_base.empty or df_target.empty:
        return {
            "available": False,
            "by_segment": pd.DataFrame(),
            "assumption": "Базовая или целевая выборка для PVM-анализа пуста.",
        }
    for frame, label in ((df_base, "базовой"), (df_target, "целевой")):
        for column in (segment_col, volume_col, value_col):
            if column not in frame.columns:
                return {
                    "available": False,
                    "by_segment": pd.DataFrame(),
                    "assumption": f"В {label} выборке нет колонки «{column}».",
                }

    base = df_base.groupby(segment_col, as_index=False).agg(
        q0=(volume_col, "sum"),
        v0=(value_col, "sum"),
    )
    target = df_target.groupby(segment_col, as_index=False).agg(
        q1=(volume_col, "sum"),
        v1=(value_col, "sum"),
    )
    merged = pd.merge(base, target, on=segment_col, how="outer").fillna(0.0)

    total_q0 = float(merged["q0"].sum())
    total_q1 = float(merged["q1"].sum())
    total_v0 = float(merged["v0"].sum())
    total_v1 = float(merged["v1"].sum())

    if total_q0 <= 0 or total_q1 <= 0:
        return {
            "available": False,
            "by_segment": pd.DataFrame(),
            "assumption": "Суммарный объём (знаменатель ставки) в одном из периодов равен нулю.",
        }

    avg_rate_0 = total_v0 / total_q0
    merged["share_0"] = merged["q0"] / total_q0
    merged["share_1"] = merged["q1"] / total_q1
    merged["rate_0"] = np.where(
        merged["q0"] > 0, merged["v0"] / merged["q0"],
        np.where(merged["q1"] > 0, merged["v1"] / merged["q1"], 0.0),
    )
    merged["rate_1"] = np.where(merged["q1"] > 0, merged["v1"] / merged["q1"], merged["rate_0"])

    merged["volume_effect"] = (total_q1 - total_q0) * merged["share_0"] * avg_rate_0
    merged["mix_effect"] = total_q1 * (merged["share_1"] - merged["share_0"]) * (merged["rate_0"] - avg_rate_0)
    merged["rate_effect"] = merged["q0"] * (merged["rate_1"] - merged["rate_0"])
    merged["total_delta"] = merged["v1"] - merged["v0"]
    merged["interaction_effect"] = merged["total_delta"] - (
        merged["volume_effect"] + merged["mix_effect"] + merged["rate_effect"]
    )

    effects = {
        "volume_effect": round(float(merged["volume_effect"].sum()), 2),
        "mix_effect": round(float(merged["mix_effect"].sum()), 2),
        "rate_effect": round(float(merged["rate_effect"].sum()), 2),
        "interaction_effect": round(float(merged["interaction_effect"].sum()), 2),
    }

    for column in ("q0", "q1", "v0", "v1", "volume_effect", "mix_effect",
                   "rate_effect", "interaction_effect", "total_delta"):
        merged[column] = merged[column].round(2)

    return {
        "available": True,
        "base_total": round(total_v0, 2),
        "target_total": round(total_v1, 2),
        "total_delta": round(total_v1 - total_v0, 2),
        "effects": effects,
        "by_segment": merged,
        "assumption": (
            "Факторное разложение PVM выполнено без нераспределённого остатка: "
            "сумма эффектов (объём + микс + ставка + взаимодействие) строго равна "
            "общему отклонению."
        ),
    }


def par_shift_decomposition(
    portfolio_base: pd.DataFrame,
    portfolio_target: pd.DataFrame,
    dpd_threshold: int = 30,
    segment_col: str = "product_code",
) -> dict:
    """Разложить изменение доли просрочки PAR (п.п.) на риск внутри продуктов и сдвиг структуры.

    Используется симметричное разложение Шепли (Кивигава — Оаксака): перекрёстный
    остаток отсутствует, сумма двух эффектов равна изменению PAR.
    """
    for frame in (portfolio_base, portfolio_target):
        if (frame is None or frame.empty or "balance" not in frame.columns
                or "dpd" not in frame.columns):
            return {
                "available": False,
                "by_segment": pd.DataFrame(),
                "assumption": "Отсутствуют таблицы портфеля или колонки balance / dpd.",
            }

    def _prep(frame: pd.DataFrame) -> pd.DataFrame:
        tmp = frame.copy()
        tmp["bal"] = pd.to_numeric(tmp["balance"], errors="coerce").fillna(0.0)
        tmp["dpd_num"] = pd.to_numeric(tmp["dpd"], errors="coerce").fillna(0.0)
        tmp["par_bal"] = np.where(tmp["dpd_num"] >= dpd_threshold, tmp["bal"], 0.0)
        if segment_col not in tmp.columns:
            tmp[segment_col] = "все продукты"
        return tmp.groupby(segment_col, as_index=False).agg(
            bal=("bal", "sum"), par_bal=("par_bal", "sum"))

    base = _prep(portfolio_base).rename(columns={"bal": "bal_0", "par_bal": "par_0"})
    target = _prep(portfolio_target).rename(columns={"bal": "bal_1", "par_bal": "par_1"})
    merged = pd.merge(base, target, on=segment_col, how="outer").fillna(0.0)

    tot_0 = float(merged["bal_0"].sum())
    tot_1 = float(merged["bal_1"].sum())
    if tot_0 <= 0 or tot_1 <= 0:
        return {
            "available": False,
            "by_segment": pd.DataFrame(),
            "assumption": "Нулевой баланс портфеля.",
        }

    par_pct_0 = float(merged["par_0"].sum() / tot_0 * 100.0)
    par_pct_1 = float(merged["par_1"].sum() / tot_1 * 100.0)

    merged["w0"] = merged["bal_0"] / tot_0
    merged["w1"] = merged["bal_1"] / tot_1
    merged["r0_pct"] = np.where(merged["bal_0"] > 0, merged["par_0"] / merged["bal_0"] * 100.0, 0.0)
    merged["r1_pct"] = np.where(merged["bal_1"] > 0, merged["par_1"] / merged["bal_1"] * 100.0, 0.0)

    merged["within_risk_effect_pp"] = 0.5 * (merged["w0"] + merged["w1"]) * (merged["r1_pct"] - merged["r0_pct"])
    merged["structural_mix_effect_pp"] = 0.5 * (merged["r0_pct"] + merged["r1_pct"]) * (merged["w1"] - merged["w0"])
    merged["total_contribution_pp"] = (
        merged["within_risk_effect_pp"] + merged["structural_mix_effect_pp"]
    )

    within = round(float(merged["within_risk_effect_pp"].sum()), 2)
    structural = round(float(merged["structural_mix_effect_pp"].sum()), 2)

    return {
        "available": True,
        "dpd_threshold": int(dpd_threshold),
        "par_base_pct": round(par_pct_0, 2),
        "par_target_pct": round(par_pct_1, 2),
        "delta_par_pp": round(par_pct_1 - par_pct_0, 2),
        "within_risk_effect_pp": within,
        "structural_mix_effect_pp": structural,
        "by_segment": merged.round(3),
        "assumption": (
            f"Изменение PAR {dpd_threshold}+ разложено по методу Шепли на вклад изменения риска "
            f"внутри продуктов ({within} п.п.) и вклад сдвига структуры портфеля "
            f"({structural} п.п.)."
        ),
    }


def _granger_f_test(x: np.ndarray, y: np.ndarray, lag: int) -> tuple[float, float]:
    """Парный F-тест Грейнджера: улучшают ли лаги X прогноз Y сверх собственных лагов Y."""
    n = len(y)
    eff_n = n - lag
    if eff_n <= 2 * lag + 2 or stats is None:
        return 0.0, 1.0

    y_target = y[lag:]
    r_cols = [np.ones(eff_n)]
    u_cols = [np.ones(eff_n)]
    for k in range(1, lag + 1):
        y_lag = y[lag - k: n - k]
        x_lag = x[lag - k: n - k]
        r_cols.append(y_lag)
        u_cols.extend([y_lag, x_lag])

    X_r = np.column_stack(r_cols)
    X_u = np.column_stack(u_cols)

    beta_r, _, _, _ = np.linalg.lstsq(X_r, y_target, rcond=None)
    beta_u, _, _, _ = np.linalg.lstsq(X_u, y_target, rcond=None)

    rss_r = float(np.sum((y_target - X_r @ beta_r) ** 2))
    rss_u = float(np.sum((y_target - X_u @ beta_u) ** 2))

    df_num = lag
    df_den = eff_n - (2 * lag + 1)
    if rss_u <= 1e-12 or df_den <= 0 or rss_r < rss_u:
        return 0.0, 1.0

    f_stat = ((rss_r - rss_u) / df_num) / (rss_u / df_den)
    p_val = float(1.0 - stats.f.cdf(f_stat, df_num, df_den))
    return float(f_stat), p_val


def benjamini_hochberg(p_values: list[float]) -> list[float]:
    """Поправка Бенджамини — Хохберга (FDR) для множественного тестирования."""
    m = len(p_values)
    if m == 0:
        return []
    order = np.argsort(p_values)
    adjusted = np.zeros(m, dtype=float)
    prev = 1.0
    for rank_rev in range(m - 1, -1, -1):
        idx = order[rank_rev]
        value = min(prev, p_values[idx] * m / (rank_rev + 1))
        adjusted[idx] = value
        prev = value
    return adjusted.tolist()


def macro_lead_lag(
    series_df: pd.DataFrame,
    period_col: str = "period",
    indicator_col: str = "indicator",
    value_col: str = "value",
    max_lag: int = 3,
    alpha: float = 0.05,
    differencing: bool = True,
) -> dict:
    """Оценить лаговые связи между макропоказателями (тест Грейнджера + FDR).

    Максимальный лаг ограничен ``floor(T / 5)``, чтобы не вырождались степени
    свободы. Связь означает прогностическое опережение, а не изолированную
    интервенцию: причинный вывод из одного ряда не следует.
    """
    if series_df is None or series_df.empty:
        return {
            "available": False,
            "links": pd.DataFrame(),
            "assumption": "Таблица макропоказателей пуста.",
        }

    for required in (period_col, indicator_col, value_col):
        if required not in series_df.columns:
            return {
                "available": False,
                "links": pd.DataFrame(),
                "assumption": f"В таблице отсутствует колонка '{required}'.",
            }

    if stats is None:
        return {
            "available": False,
            "links": pd.DataFrame(),
            "assumption": SCIPY_REQUIRED,
        }

    tmp = series_df.copy()
    tmp["_val"] = pd.to_numeric(tmp[value_col], errors="coerce")
    tmp = tmp.dropna(subset=["_val", indicator_col])

    pivot = (tmp.pivot_table(index=period_col, columns=indicator_col, values="_val", aggfunc="mean")
               .sort_index())
    pivot = pivot.dropna(axis=1, how="all").dropna(axis=0, how="any")

    n_periods, n_vars = pivot.shape
    if n_periods < 8 or n_vars < 2:
        return {
            "available": False,
            "links": pd.DataFrame(),
            "assumption": (
                f"Недостаточно общих периодов ({n_periods}) или показателей ({n_vars}) "
                "для лагового анализа."
            ),
        }

    work_df = pivot.diff().dropna() if differencing else pivot
    eff_t = len(work_df)
    safe_max_lag = max(1, min(int(max_lag), eff_t // 5))

    columns = list(work_df.columns)
    raw_rows: list[dict] = []
    for cause in columns:
        for effect in columns:
            if cause == effect:
                continue
            x = work_df[cause].to_numpy(dtype=float)
            y = work_df[effect].to_numpy(dtype=float)
            if np.std(x) < 1e-12 or np.std(y) < 1e-12:
                continue
            best_lag = 1
            best_p = 1.0
            best_f = 0.0
            best_corr = 0.0
            for lag in range(1, safe_max_lag + 1):
                f_stat, p_val = _granger_f_test(x, y, lag)
                corr = float(np.corrcoef(x[:-lag], y[lag:])[0, 1])
                if p_val <= best_p:
                    best_p = p_val
                    best_f = f_stat
                    best_lag = lag
                    best_corr = corr
            raw_rows.append(
                {
                    "cause_indicator": str(cause),
                    "effect_indicator": str(effect),
                    "best_lag": int(best_lag),
                    "lag_corr": round(best_corr, 4),
                    "granger_f": round(best_f, 4),
                    "p_value_raw": round(best_p, 5),
                }
            )

    if not raw_rows:
        return {
            "available": False,
            "links": pd.DataFrame(),
            "assumption": "Не удалось оценить лаговые связи.",
        }

    links = pd.DataFrame(raw_rows)
    links["p_value_fdr"] = np.round(benjamini_hochberg(links["p_value_raw"].tolist()), 5)
    links["significant_fdr"] = links["p_value_fdr"] < alpha
    links = links.sort_values(["p_value_fdr", "granger_f"], ascending=[True, False]).reset_index(drop=True)

    return {
        "available": True,
        "max_lag_used": int(safe_max_lag),
        "periods_analyzed": int(eff_t),
        "links": links,
        "significant_count": int(links["significant_fdr"].sum()),
        "assumption": (
            f"Лаговый анализ Грейнджера выполнен на "
            f"{'первых разностях' if differencing else 'уровнях'} (T={eff_t}, max_lag={safe_max_lag}) "
            f"с поправкой Бенджамини — Хохберга (FDR < {alpha}). Отражает прогностическое "
            "опережение, а не изолированную интервенцию."
        ),
    }
