"""Расчёт метрик. Только чистые функции: вход — DataFrame, выход — DataFrame/dict.

Ни одна функция не печатает и не пишет файлы: это позволяет тестировать
арифметику отдельно от отчётов.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

STAGE_ORDER = ["лид", "обращение", "заявка", "одобрение", "договор", "активация"]
STAGE_RANK = {stage: i + 1 for i, stage in enumerate(STAGE_ORDER)}


class MetricError(ValueError):
    """Ошибка входных данных, которую нужно показать пользователю."""


def _num(df: pd.DataFrame, column: str, default: float = np.nan) -> pd.Series:
    if column in df.columns:
        return pd.to_numeric(df[column], errors="coerce")
    return pd.Series(default, index=df.index, dtype="float64")


def _normalize_stage(value: str) -> str:
    text = str(value).strip().lower().replace("ё", "е")
    aliases = {
        "lead": "лид", "лиды": "лид",
        "application": "заявка", "заявки": "заявка",
        "approval": "одобрение", "одобрено": "одобрение",
        "contract": "договор", "сделка": "договор",
        "activation": "активация", "актив": "активация",
    }
    return aliases.get(text, text)


# --------------------------------------------------------------------------
# Портфель
# --------------------------------------------------------------------------

def portfolio_summary(
    portfolio: pd.DataFrame,
    products: pd.DataFrame | None = None,
    clients: pd.DataFrame | None = None,
) -> dict:
    """Показатели портфеля по типу продукта с PAR 30/60/90 по dpd."""
    if portfolio is None or portfolio.empty:
        raise MetricError("Таблица portfolio пуста")

    df = portfolio.copy()
    df["balance"] = _num(df, "balance")
    df["limit_amount"] = _num(df, "limit_amount")
    df["dpd"] = _num(df, "dpd", default=0.0).fillna(0.0)

    if products is not None and "product_code" in df.columns:
        ref = products[["product_code", "product_type"]] if "product_type" in products.columns else None
        if ref is not None:
            df = df.merge(ref.drop_duplicates("product_code"), on="product_code", how="left")
    if "product_type" not in df.columns:
        df["product_type"] = "все продукты"
    df["product_type"] = df["product_type"].fillna("не указан")

    id_col = "account_id" if "account_id" in df.columns else None
    grouped = df.groupby("product_type", dropna=False)
    rows = pd.DataFrame({
        "accounts": grouped.size() if id_col is None else grouped[id_col].count(),
        "clients": grouped["client_id"].nunique() if "client_id" in df.columns else np.nan,
        "total_balance": grouped["balance"].sum(min_count=1),
        "total_limit": grouped["limit_amount"].sum(min_count=1),
        "avg_balance": grouped["balance"].mean(),
        "avg_limit": grouped["limit_amount"].mean(),
        "avg_dpd": grouped["dpd"].mean(),
    }).reset_index()

    balance_sum = rows["total_balance"].replace(0, np.nan)
    for days in (30, 60, 90):
        overdue = (df[df["dpd"] >= days].groupby("product_type")["balance"].sum()
                   .reindex(rows["product_type"]).fillna(0.0))
        rows[f"par{days}_pct"] = (overdue.values / balance_sum.values * 100).round(2)

    if "client_id" in df.columns and df["balance"].notna().any():
        by_client = df.groupby("client_id")["balance"].sum().sort_values(ascending=False)
        top10 = by_client.head(10).sum()
        rows_total = by_client.sum()
        concentration = float(top10 / rows_total * 100) if rows_total else np.nan
    else:
        concentration = np.nan

    totals = {
        "accounts": int(len(df)),
        "clients": int(df["client_id"].nunique()) if "client_id" in df.columns else None,
        "total_balance": float(df["balance"].sum(min_count=1)),
        "total_limit": float(df["limit_amount"].sum(min_count=1)) if df["limit_amount"].notna().any() else None,
        "par30_pct": _overall_par(df, 30),
        "par60_pct": _overall_par(df, 60),
        "par90_pct": _overall_par(df, 90),
        "top10_clients_share_pct": concentration,
    }
    return {"rows": rows, "totals": totals, "rows_total": int(len(df))}


def _overall_par(df: pd.DataFrame, days: int) -> float | None:
    total = df["balance"].sum()
    if not total or np.isnan(total):
        return None
    overdue = df.loc[df["dpd"] >= days, "balance"].sum()
    return float(overdue / total * 100)


# --------------------------------------------------------------------------
# Воронка
# --------------------------------------------------------------------------

def funnel_summary(events: pd.DataFrame, stage_column: str = "stage") -> dict:
    """Конверсия воронки по кумулятивному числу дошедших до этапа.

    Считается максимум достигнутого этапа на одну заявку (application_id),
    а если его нет — на клиента. Это допущение фиксируется в ответе.
    """
    if events is None or events.empty:
        raise MetricError("Таблица событий воронки пуста")
    if stage_column not in events.columns:
        raise MetricError(f"Нет колонки {stage_column!r}: {list(events.columns)}")

    df = events.copy()
    df["_stage"] = df[stage_column].map(_normalize_stage)
    unknown = sorted(set(df["_stage"]) - set(STAGE_ORDER))
    if unknown:
        raise MetricError(
            "Неизвестные этапы воронки: " + ", ".join(map(str, unknown))
            + ". Ожидаются: " + ", ".join(STAGE_ORDER)
        )

    if "application_id" in df.columns:
        entity_col, entity_name = "application_id", "заявка"
        assumption = "Этап заявки — максимальный достигнутый; progression восстанавливается по application_id."
    elif "client_id" in df.columns:
        entity_col, entity_name = "client_id", "клиент"
        assumption = ("Нет application_id: этап считается максимальным достигнутым на клиента. "
                      "Если клиент подавал несколько заявок, конверсия по ним не разделяется.")
    else:
        raise MetricError("Нужна колонка application_id или client_id")

    df["_rank"] = df["_stage"].map(STAGE_RANK)
    rank = df.groupby(entity_col)["_rank"].max()

    reached = {stage: int((rank >= STAGE_RANK[stage]).sum()) for stage in STAGE_ORDER}
    base = reached[STAGE_ORDER[0]]
    rows = []
    previous = None
    for stage in STAGE_ORDER:
        count = reached[stage]
        rows.append({
            "stage": stage,
            "reached": count,
            "from_start_pct": round(count / base * 100, 2) if base else np.nan,
            "stage_pct": round(count / previous * 100, 2) if previous else np.nan,
            "lost": (previous - count) if previous is not None else np.nan,
        })
        previous = count

    result = pd.DataFrame(rows)
    biggest_loss = result.dropna(subset=["lost"]).sort_values("lost", ascending=False).head(1)
    return {
        "rows": result,
        "entity": entity_name,
        "entities": int(rank.size),
        "assumption": assumption,
        "biggest_loss_stage": biggest_loss["stage"].iloc[0] if not biggest_loss.empty else None,
    }


# --------------------------------------------------------------------------
# Юнит-экономика
# --------------------------------------------------------------------------

def finance_summary(finance: pd.DataFrame, portfolio: pd.DataFrame | None = None) -> dict:
    """Выручка, прямые расходы, валовая и contribution margin по периодам."""
    if finance is None or finance.empty:
        raise MetricError("Таблица finance пуста")

    df = finance.copy()
    for column in ("interest_income", "fee_income", "premium", "funding_cost",
                   "reserve_expense", "opex", "tax", "marketing_cost"):
        if column in df.columns:
            df[column] = pd.to_numeric(df[column], errors="coerce")

    def col(name: str) -> pd.Series:
        return df[name].fillna(0.0) if name in df.columns else pd.Series(0.0, index=df.index)

    df["revenue"] = col("interest_income") + col("fee_income") + col("premium")
    df["direct_costs"] = col("funding_cost") + col("reserve_expense")
    df["gross_margin"] = df["revenue"] - df["direct_costs"]
    df["opex"] = col("opex")
    df["contribution"] = df["gross_margin"] - df["opex"]
    df["margin_pct"] = np.where(df["revenue"] > 0, df["gross_margin"] / df["revenue"] * 100, np.nan)

    grain = [c for c in ("period", "product_code") if c in df.columns]
    if grain:
        rows = (df.groupby(grain, dropna=False)[["revenue", "direct_costs", "gross_margin",
                                                 "contribution", "opex"]].sum()
                  .reset_index())
    else:
        rows = df[["revenue", "direct_costs", "gross_margin", "contribution", "opex"]].sum().to_frame().T
    rows["margin_pct"] = np.where(rows["revenue"] > 0,
                                  rows["gross_margin"] / rows["revenue"] * 100, np.nan)

    revenue = float(df["revenue"].sum())
    gross = float(df["gross_margin"].sum())
    totals = {
        "revenue": revenue,
        "direct_costs": float(df["direct_costs"].sum()),
        "gross_margin": gross,
        "opex": float(col("opex").sum()),
        "contribution": float(df["contribution"].sum()),
        "margin_pct": round(gross / revenue * 100, 2) if revenue else None,
        "margin_ratio": (gross / revenue) if revenue else None,
        "marketing_cost": float(col("marketing_cost").sum()) if "marketing_cost" in df.columns else None,
    }

    if portfolio is not None and "balance" in portfolio.columns:
        avg_assets = float(pd.to_numeric(portfolio["balance"], errors="coerce").mean())
        if avg_assets:
            interest = float(col("interest_income").sum())
            funding = float(col("funding_cost").sum())
            totals["nim_pct"] = round((interest - funding) / avg_assets * 100, 4)
    return {"rows": rows, "totals": totals}


def unit_economics(
    finance: pd.DataFrame,
    clients: pd.DataFrame | None = None,
    portfolio: pd.DataFrame | None = None,
    sales: pd.DataFrame | None = None,
    lifetime_months: float = 24.0,
) -> dict:
    """CAC, ARPU, LTV (gross/net), LTV/CAC, окупаемость.

    Если расходов на привлечение нет, CAC = None и ratio не считается:
    подставлять ноль нельзя, это меняет смысл метрики.
    """
    fin = finance_summary(finance, portfolio)["totals"]
    revenue = fin["revenue"]
    margin_pct = fin["margin_pct"]
    margin_ratio = fin.get("margin_ratio") or 0.0

    if portfolio is not None and "client_id" in portfolio.columns:
        active_clients = int(portfolio["client_id"].nunique())
        client_source = "portfolio.client_id"
    elif clients is not None and "client_id" in clients.columns:
        active_clients = int(clients["client_id"].nunique())
        client_source = "clients.client_id"
    elif sales is not None and "client_id" in sales.columns:
        active_clients = int(sales["client_id"].nunique())
        client_source = "sales.client_id"
    else:
        active_clients = 0
        client_source = "нет данных"

    # ARPU — за месяц, а не за весь период: иначе LTV завышается в разы.
    if "period" in finance.columns:
        months = int(pd.to_datetime(finance["period"].astype(str), errors="coerce", format="mixed")
                     .dt.to_period("M").nunique())
    else:
        months = 1
    months = max(months, 1)

    arpu = revenue / active_clients / months if active_clients else None
    marketing = fin.get("marketing_cost")
    cac = marketing / active_clients if marketing and active_clients else None

    lifetime = float(lifetime_months)
    ltv_gross = (arpu * margin_ratio * lifetime) if arpu else None
    ltv_net = (ltv_gross - cac) if (ltv_gross is not None and cac is not None) else None
    ratio = (ltv_gross / cac) if (ltv_gross is not None and cac) else None
    monthly_margin = (arpu * margin_ratio) if arpu else None
    payback = (cac / monthly_margin) if (cac and monthly_margin) else None

    assumptions = [
        f"Срок жизни клиента принят {lifetime:.0f} мес. (параметр lifetime_months).",
        f"ARPU — выручка за месяц на активного клиента; периодов в данных: {months}.",
        f"Активные клиенты: {client_source}; CAC считается на активного клиента.",
    ]
    if cac is None:
        assumptions.append(
            "Расходы на привлечение не найдены (нет колонки marketing_cost в finance): "
            "CAC, LTV/CAC и окупаемость не рассчитываются."
        )

    return {
        "revenue": revenue,
        "months": months,
        "active_clients": active_clients,
        "arpu": arpu,
        "margin_pct": margin_pct,
        "marketing_cost": marketing,
        "cac": cac,
        "lifetime_months": lifetime,
        "ltv_gross": ltv_gross,
        "ltv_net": ltv_net,
        "ltv_cac": ratio,
        "payback_months": payback,
        "assumptions": assumptions,
    }


# --------------------------------------------------------------------------
# Макро
# --------------------------------------------------------------------------

def macro_structure(df: pd.DataFrame, value_col: str = "value_mln_rub",
                    group_col: str = "category") -> pd.DataFrame:
    if value_col not in df.columns:
        raise MetricError(f"Нет колонки {value_col!r}")
    work = df.copy()
    work[value_col] = pd.to_numeric(work[value_col], errors="coerce")
    total = work[value_col].sum()
    rows = (work.groupby(group_col, dropna=False)[value_col].sum(min_count=1)
                .reset_index().sort_values(value_col, ascending=False))
    rows["share_pct"] = (rows[value_col] / total * 100).round(2) if total else np.nan
    return rows


def macro_dynamics(series: pd.DataFrame, value_col: str = "value_mln_rub",
                   period_col: str = "period_start") -> pd.DataFrame:
    """Темп к предыдущему периоду и YoY, если ряд месячный и данных хватает."""
    if period_col not in series.columns:
        raise MetricError(f"Нет колонки {period_col!r}: сначала normalize_periods")
    work = series.sort_values(period_col).copy()
    work[value_col] = pd.to_numeric(work[value_col], errors="coerce")
    work["change_pct"] = (work[value_col].pct_change() * 100).round(2)
    if len(work) >= 13:
        work["yoy_pct"] = (work[value_col].pct_change(12) * 100).round(2)
    return work


def _robust_z(values: pd.Series) -> pd.Series:
    clean = values.dropna()
    if len(clean) < 3:
        return pd.Series(np.nan, index=values.index)
    if len(clean) < 50:
        median = clean.median()
        mad = (clean - median).abs().median()
        if mad == 0:
            std = clean.std(ddof=0)
            if std == 0:
                return pd.Series(0.0, index=values.index)
            return (values - clean.mean()) / std
        return 0.6745 * (values - median) / mad
    return (values - clean.mean()) / clean.std(ddof=0)


def macro_anomalies(df: pd.DataFrame, value_col: str = "value_mln_rub",
                    period_col: str = "period_start",
                    group_cols: tuple[str, ...] = ("category",),
                    threshold: float = 3.0) -> pd.DataFrame:
    """Аномалии внутри каждой однородной серии, а не по всей таблице сразу."""
    missing = [c for c in (value_col, period_col, *group_cols) if c not in df.columns]
    if missing:
        raise MetricError(f"Нет колонок: {', '.join(missing)}")
    work = df.copy()
    work[value_col] = pd.to_numeric(work[value_col], errors="coerce")
    if len(group_cols) == 0:
        work["_z"] = _robust_z(work[value_col])
        return work[work["_z"].abs() > threshold].copy()
    work["_z"] = work.groupby(list(group_cols), dropna=False)[value_col].transform(_robust_z)
    result = work[work["_z"].abs() > threshold].copy()
    return result.sort_values("_z", key=lambda s: s.abs(), ascending=False)


def _permutation_p(x: np.ndarray, y: np.ndarray, observed: float,
                   iterations: int = 2000, seed: int = 42) -> float:
    rng = np.random.default_rng(seed)
    count = 0
    for _ in range(iterations):
        shuffled = rng.permutation(y)
        if abs(np.corrcoef(x, shuffled)[0, 1]) >= abs(observed):
            count += 1
    return (count + 1) / (iterations + 1)


def macro_correlation(left: pd.DataFrame, right: pd.DataFrame,
                      left_col: str, right_col: str,
                      period_col: str = "period_start") -> dict:
    """Пирсон и Спирмен по совпадающим периодам + перестановочный p-value."""
    merged = (left[[period_col, left_col]].dropna()
              .merge(right[[period_col, right_col]].dropna(), on=period_col, how="inner"))
    n = len(merged)
    if n < 3:
        return {"n": n, "pearson": None, "spearman": None, "p_value": None}
    x = merged[left_col].astype(float).to_numpy()
    y = merged[right_col].astype(float).to_numpy()
    if np.std(x) == 0 or np.std(y) == 0:
        return {"n": n, "pearson": None, "spearman": None, "p_value": None}
    pearson = float(np.corrcoef(x, y)[0, 1])
    spearman = float(pd.Series(x).corr(pd.Series(y), method="spearman"))
    return {
        "n": n,
        "pearson": round(pearson, 3),
        "spearman": round(spearman, 3),
        "p_value": round(_permutation_p(x, y, pearson), 4),
    }


def forecast_trend(series: pd.DataFrame, value_col: str = "value",
                   period_col: str = "period_start", horizon: int = 4,
                   band: float = 1.0) -> dict:
    """Линейный тренд с бэктестом и границами неопределённости.

    Границы — базовая линия ± band × MAE бэктеста. Это честнее множителей:
    для убывающего или отрицательного ряда множитель меняет знак смысла.
    """
    data = series.dropna(subset=[value_col, period_col]).sort_values(period_col)
    if len(data) < 6:
        return {"available": False, "reason": "нужно минимум 6 периодов"}

    y = data[value_col].astype(float).to_numpy()
    x = np.arange(len(y), dtype=float)
    slope, intercept = np.polyfit(x, y, 1)

    backtest_n = min(6, max(2, len(y) // 4))
    fit_slope, fit_intercept = np.polyfit(x[:-backtest_n], y[:-backtest_n], 1)
    predicted = fit_slope * x[-backtest_n:] + fit_intercept
    actual = y[-backtest_n:]
    mae = float(np.mean(np.abs(actual - predicted)))
    nonzero = actual != 0
    mape = float(np.mean(np.abs((actual[nonzero] - predicted[nonzero]) / actual[nonzero])) * 100) if nonzero.any() else None

    last_period = pd.Timestamp(data[period_col].iloc[-1])
    step = _infer_step(data[period_col])
    future = [last_period + step * (i + 1) for i in range(horizon)]
    base = np.polyval([slope, intercept], np.arange(len(y), len(y) + horizon, dtype=float))
    delta = band * mae

    return {
        "available": True,
        "periods": [p.strftime("%Y-%m-%d") for p in future],
        "scenarios": {
            "пессимистичный": [round(float(v - delta), 2) for v in base],
            "базовый": [round(float(v), 2) for v in base],
            "оптимистичный": [round(float(v + delta), 2) for v in base],
        },
        "band_mae": round(delta, 4),
        "mae": round(mae, 3),
        "mape_pct": round(mape, 2) if mape is not None else None,
        "slope_per_period": round(float(slope), 4),
        "history_points": int(len(y)),
        "assumption": ("Линейный тренд по истории; границы сценариев = базовая линия ± "
                       f"{band:g} × MAE бэктеста. Это интервал ошибки модели, а не прогноз факторов."),
    }


def _infer_step(periods: pd.Series) -> pd.DateOffset:
    diffs = pd.Series(pd.to_datetime(periods)).diff().dropna().dt.days
    median = float(diffs.median()) if len(diffs) else 30.0
    if median >= 300:
        return pd.DateOffset(years=1)
    if median >= 80:
        return pd.DateOffset(months=3)
    return pd.DateOffset(months=1)
