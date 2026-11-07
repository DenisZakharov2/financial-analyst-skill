"""Расчёт метрик. Только чистые функции: вход — DataFrame, выход — DataFrame/dict.

Ни одна функция не печатает и не пишет файлы: это позволяет тестировать
арифметику отдельно от отчётов.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .core import canonical_period, drop_total_rows, parse_period

STAGE_ORDER = ["лид", "обращение", "заявка", "одобрение", "договор", "активация"]
STAGE_RANK = {stage: i + 1 for i, stage in enumerate(STAGE_ORDER)}

# Сколько месяцев покрывает один период каждого типа.
PERIOD_MONTHS = {"M": 1, "D": 1, "Q": 3, "Y": 12}


class MetricError(ValueError):
    """Ошибка входных данных, которую нужно показать пользователю."""


def _num(df: pd.DataFrame, column: str, default: float = np.nan) -> pd.Series:
    if column in df.columns:
        return pd.to_numeric(df[column], errors="coerce")
    return pd.Series(default, index=df.index, dtype="float64")


def _months_in_periods(values: pd.Series) -> int:
    """Сколько календарных месяцев покрывает набор периодов.

    Месяц учитывается один раз, квартал — как три месяца, год — как двенадцать.
    Так ARPU и NIM делят поток на длительность выгрузки, а не на число строк,
    и «2024-01», «01.2024», «январь 2024», «1к2024» дают одинаковый результат.
    """
    spans: dict[pd.Period, int] = {}
    for value in values:
        start, kind = parse_period(value)
        if pd.isna(start):
            continue
        month = pd.Timestamp(start).to_period("M")
        spans[month] = max(spans.get(month, 1), PERIOD_MONTHS.get(kind, 1))
    return int(sum(spans.values()))


def _new_clients(clients: pd.DataFrame | None, finance: pd.DataFrame,
                 portfolio: pd.DataFrame | None = None) -> int | None:
    """Новые клиенты за период выгрузки — знаменатель CAC.

    Нужна дата регистрации: без неё отличить нового клиента от активного
    невозможно, и вызывающий код честно откатывается на активных клиентов.
    Если есть портфель, считаются только клиенты со счётом: иначе CAC делится
    на всех зарегистрированных, ARPU — на активных, и метрики несопоставимы.
    Возвращает число клиентов (0, если за период новых нет) или None, если
    метрика не считается.
    """
    if clients is None or "client_id" not in clients.columns or "period" not in finance.columns:
        return None
    date_column = next((column for column in ("registration_date", "signup_date", "first_product_date")
                        if column in clients.columns), None)
    if date_column is None:
        return None
    dates = pd.to_datetime(clients[date_column], errors="coerce")
    if dates.notna().sum() == 0:
        return None
    parsed_periods = [
        (start, kind)
        for start, kind in (parse_period(value) for value in finance["period"])
        if pd.notna(start)
    ]
    if not parsed_periods:
        return None
    begin = min(start for start, _ in parsed_periods)
    end = max(
        start + (pd.Timedelta(days=1) if kind == "D" else pd.DateOffset(months=PERIOD_MONTHS.get(kind, 1)))
        for start, kind in parsed_periods
    )
    window = (dates >= begin) & (dates < end)
    if portfolio is not None and "client_id" in portfolio.columns:
        window &= clients["client_id"].isin(portfolio["client_id"].dropna())
    return int(clients.loc[window, "client_id"].nunique())


def _normalize_stage(value: str) -> str:
    text = str(value).strip().lower().replace("ё", "е")
    aliases = {
        "lead": "лид", "лиды": "лид",
        "application": "заявка", "заявки": "заявка",
        "approval": "одобрение", "одобрено": "одобрение", "approve": "одобрение", "approved": "одобрение",
        "contract": "договор", "сделка": "договор", "issued": "договор",
        "activation": "активация", "актив": "активация",
        "contact": "обращение", "inquiry": "обращение",
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
    # Итоговую строку product_code=ALL не суммируем с детализацией (словарь данных, §3).
    df, excluded = drop_total_rows(df, group=())

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
        # accounts в разрезе и в итогах — один и тот же счётчик (уникальные счета),
        # иначе сумма по продуктам не сходится с итогом.
        "accounts": grouped.size() if id_col is None else grouped[id_col].nunique(),
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
        "accounts": int(df[id_col].nunique()) if id_col else int(len(df)),
        "clients": int(df["client_id"].nunique()) if "client_id" in df.columns else None,
        "total_balance": float(df["balance"].sum(min_count=1)),
        "total_limit": float(df["limit_amount"].sum(min_count=1)) if df["limit_amount"].notna().any() else None,
        "par30_pct": _overall_par(df, 30),
        "par60_pct": _overall_par(df, 60),
        "par90_pct": _overall_par(df, 90),
        "top10_clients_share_pct": concentration,
        "excluded_total_rows": excluded,
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
        assumption = "Этап заявки — максимальный достигнутый; прогрессия восстанавливается по application_id."
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

def finance_summary(finance: pd.DataFrame, portfolio: pd.DataFrame | None = None,
                    products: pd.DataFrame | None = None) -> dict:
    """Выручка, прямые расходы, валовая и contribution margin по периодам."""
    if finance is None or finance.empty:
        raise MetricError("Таблица finance пуста")

    df = finance.copy()
    if "period" in df.columns:
        # «2024-01», «01.2024» и «январь 2024» — один и тот же период.
        df["period"] = df["period"].map(canonical_period)
    for column in ("interest_income", "fee_income", "premium", "funding_cost",
                   "reserve_expense", "opex", "tax", "marketing_cost"):
        if column in df.columns:
            df[column] = pd.to_numeric(df[column], errors="coerce")

    # Итоговую строку product_code=ALL не суммируем с детализацией (словарь данных, §7).
    df, excluded = drop_total_rows(df, group=("period",))

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
        "excluded_total_rows": excluded,
    }

    if portfolio is not None and "balance" in portfolio.columns:
        # Итоговую строку product_code=ALL не суммируем с детализацией.
        clean_port, _ = drop_total_rows(portfolio, group=())
        # В знаменатель NIM попадают только работающие (кредитные) активы:
        # депозиты и страхование — пассивы, не кредитные остатки.
        work_port = clean_port
        if products is not None and "product_code" in clean_port.columns and "product_type" in products.columns:
            ref = products[["product_code", "product_type"]].drop_duplicates("product_code")
            work_port = clean_port.merge(ref, on="product_code", how="left")
            credit_types = {"кредит_потребительский", "кредитная_карта", "кредитная карта",
                            "кредит", "overdraft", "кредитная линия"}
            work_port = work_port[work_port["product_type"].isin(credit_types) |
                                  work_port["product_type"].isna()]
        avg_assets = float(pd.to_numeric(work_port["balance"], errors="coerce").sum())
        months = max(_months_in_periods(df["period"]), 1) if "period" in df.columns else 1
        if avg_assets and months:
            interest = float(col("interest_income").sum())
            funding = float(col("funding_cost").sum())
            # NIM — годовая ставка: поток за месяцы выгрузки приводим к месяцу,
            # затем к году, иначе 6 месяцев выручки делятся на остаток одного дня.
            totals["nim_pct"] = round((interest - funding) / months / avg_assets * 100 * 12, 2)
            totals["nim_months"] = months
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

    work_portfolio = portfolio
    if portfolio is not None and "client_id" in portfolio.columns:
        clean_port, _ = drop_total_rows(portfolio, group=())
        if "status" in clean_port.columns:
            clean_port = clean_port[clean_port["status"].astype(str).str.strip().str.lower() != "закрыт"]
        work_portfolio = clean_port
    if work_portfolio is not None and "client_id" in work_portfolio.columns:
        active_clients = int(work_portfolio["client_id"].nunique())
        client_source = "portfolio.client_id"
    elif clients is not None and "client_id" in clients.columns:
        active_clients = int(clients["client_id"].nunique())
        client_source = "clients.client_id"
    elif sales is not None and "client_id" in sales.columns:
        active_clients = int(sales["client_id"].nunique())
        client_source = "sales.client_id"
    else:
        active_clients = None
        client_source = "нет данных"

    # ARPU — за месяц, а не за весь период: иначе LTV завышается в разы.
    months = max(_months_in_periods(finance["period"]), 1) if "period" in finance.columns else 1

    arpu = revenue / active_clients / months if active_clients else None
    marketing = fin.get("marketing_cost")
    portfolio_base = portfolio is not None and "client_id" in portfolio.columns
    acquired = _new_clients(clients, finance, portfolio if portfolio_base else None)
    # Знаменатель CAC — та же база, что у ARPU: клиент со счётом. Ноль новых
    # клиентов не подменяется активными: это разные метрики.
    cac_base = acquired if acquired is not None else active_clients
    cac = marketing / cac_base if marketing and cac_base else None

    lifetime = float(lifetime_months)
    ltv_gross = (arpu * margin_ratio * lifetime) if arpu else None
    ltv_net = (ltv_gross - cac) if (ltv_gross is not None and cac is not None) else None
    ratio = (ltv_gross / cac) if (ltv_gross is not None and cac) else None
    monthly_margin = (arpu * margin_ratio) if arpu else None
    # Отрицательная или нулевая валовая маржа означает убыток на каждого клиента:
    # привлечение не окупается, срок окупаемости и LTV/CAC не определяются.
    payback = (cac / monthly_margin) if (cac and monthly_margin and monthly_margin > 0) else None

    assumptions = [
        f"Срок жизни клиента принят {lifetime:.0f} мес. (параметр lifetime_months).",
        f"ARPU — выручка за месяц на активного клиента; периодов в данных: {months}.",
        f"Активные клиенты: {client_source}; ARPU считается на активного клиента.",
    ]
    if cac is not None:
        if acquired:
            scope = "со счётом в портфеле" if portfolio_base else "по дате регистрации"
            assumptions.append(
                f"CAC = расходы на привлечение / новые клиенты за период ({acquired} чел. {scope})."
            )
        else:
            assumptions.append(
                "Нет даты регистрации клиентов: CAC считается на активного клиента — "
                "это занижает CAC, если база растёт."
            )
    else:
        if not marketing:
            assumptions.append(
                "Расходы на привлечение не найдены (нет колонки marketing_cost в finance): "
                "CAC, LTV/CAC и окупаемость не рассчитываются."
            )
        elif acquired == 0:
            assumptions.append(
                "За окно finance нет новых клиентов с датой регистрации: делить не на кого, "
                "CAC, LTV/CAC и окупаемость не рассчитываются."
            )
        else:
            assumptions.append(
                "Нет данных о клиентах (client_id в portfolio, clients или sales): "
                "CAC, LTV/CAC и окупаемость не рассчитываются."
            )

    return {
        "revenue": revenue,
        "months": months,
        "active_clients": active_clients,
        "new_clients": acquired,
        "cac_base": cac_base,
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


# Шаг регулярного ряда в днях → сколько месяцев он покрывает.
STEP_MONTHS = ((27, 32, 1), (85, 95, 3), (175, 190, 6), (360, 370, 12))


def _median_step_days(periods: pd.Series) -> float | None:
    """Медианный шаг ряда в днях; None, если разбираемых дат меньше двух."""
    dates = pd.to_datetime(pd.Series(periods), errors="coerce").dropna()
    if len(dates) < 2:
        return None
    diffs = dates.sort_values().diff().dropna().dt.days
    return float(diffs.median()) if len(diffs) else None


def _step_months(periods: pd.Series) -> int | None:
    """Шаг ряда в месяцах (1, 3, 6, 12) или None для неровного и дневного ряда."""
    days = _median_step_days(periods)
    if days is None:
        return None
    for low, high, months in STEP_MONTHS:
        if low <= days <= high:
            return months
    return None


def _pct_change(current: pd.Series, previous: pd.Series) -> pd.Series:
    """Темп в процентах; нулевая или отсутствующая база → null, а не бесконечность."""
    base = previous.replace(0, np.nan)
    return (current - base) / base * 100


def _yoy_pct(work: pd.DataFrame, value_col: str, period_col: str) -> pd.Series | None:
    """YoY по календарю: значение того же периода год назад.

    None — если медианный шаг ряда не месяц, квартал и не полугодие:
    для дневного и недельного ряда «год назад» не выражается шагом строк.
    """
    if _step_months(work[period_col]) not in (1, 3, 6):
        return None
    dates = pd.to_datetime(work[period_col], errors="coerce")
    lookup = pd.Series(work[value_col].to_numpy(dtype="float64"), index=dates)
    lookup = lookup[lookup.index.notna()]
    lookup = lookup[~lookup.index.duplicated(keep="last")].sort_index()
    if lookup.empty:
        return None
    shifted = lookup.reindex(lookup.index - pd.DateOffset(months=12))
    previous = pd.Series(shifted.to_numpy(), index=lookup.index)
    return _pct_change(work[value_col].astype("float64"), dates.map(previous))


def macro_dynamics(series: pd.DataFrame, value_col: str = "value_mln_rub",
                   period_col: str = "period_start") -> pd.DataFrame:
    """Темп к предыдущему периоду и YoY по календарю, когда шаг ряда это позволяет.

    YoY считается только для регулярных месячных, квартальных и полугодовых рядов
    и сравнивается с тем же периодом год назад, а не со сдвигом на 12 строк:
    на квартальном ряду сдвиг на 12 строк — это три года, а не один.
    Нулевая база даёт ``null``, а не бесконечность (контракт ``change_pct``).
    """
    if period_col not in series.columns:
        raise MetricError(f"Нет колонки {period_col!r}: сначала normalize_periods")
    work = series.sort_values(period_col).copy()
    work[value_col] = pd.to_numeric(work[value_col], errors="coerce")
    work["change_pct"] = _pct_change(work[value_col], work[value_col].shift(1))
    yoy = _yoy_pct(work, value_col, period_col)
    if yoy is not None:
        work["yoy_pct"] = yoy
    return work


def robust_z(values: pd.Series) -> pd.Series:
    """Робастный Z-score серии: MAD при n < 50, обычный Z при n ≥ 50.

    Единственная реализация на весь скилл: ею пользуются раздел аномалий
    (``macro_anomalies``) и интерактивный дашборд. Раньше у дашборда была своя
    копия, и на одном и том же ряду отчёт показывал аномалию, а дашборд — нет.
    Порядок фолбэков при вырожденном MAD: MAD → стандартное отклонение → ноль
    для константного ряда (пропуски остаются пропусками).
    """
    clean = values.dropna()
    if len(clean) < 3:
        return pd.Series(np.nan, index=values.index)
    if len(clean) < 50:
        median = clean.median()
        mad = (clean - median).abs().median()
        if mad > 0:
            return 0.6745 * (values - median) / mad
    std = clean.std(ddof=0)
    if std == 0:
        # Константный ряд: отклонений нет, но пропуски остаются пропусками.
        return values.where(values.isna(), 0.0)
    return (values - clean.mean()) / std


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
        work["_z"] = robust_z(work[value_col])
        return work[work["_z"].abs() > threshold].copy()
    work["_z"] = work.groupby(list(group_cols), dropna=False)[value_col].transform(robust_z)
    result = work[work["_z"].abs() > threshold].copy()
    return result.sort_values("_z", key=lambda s: s.abs(), ascending=False)


def _permutation_p(x: np.ndarray, y: np.ndarray, observed: float,
                   iterations: int = 2000, seed: int = 42) -> float:
    rng = np.random.default_rng(seed)
    x_std = (x - x.mean()) / (x.std() * np.sqrt(len(x)))
    y_std = (y - y.mean()) / (y.std() * np.sqrt(len(y)))
    # Все перестановки матрицей (iterations, n); корреляции — одним матричным умножением.
    tiled = np.broadcast_to(y_std, (iterations, len(y_std))).copy()
    permuted = rng.permuted(tiled, axis=1)
    corrs = np.abs(permuted @ x_std)
    count = int(np.sum(corrs >= abs(observed)))
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
    # Спирмен = Пирсон по рангам: scipy в зависимостях нет, метод pandas потребовал бы её.
    spearman = float(pd.Series(x).rank().corr(pd.Series(y).rank()))
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
    data = series.copy()
    data[period_col] = pd.to_datetime(data[period_col], errors="coerce")
    data = data.dropna(subset=[value_col, period_col]).sort_values(period_col)
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
    assumption = ("Линейный тренд по истории; границы сценариев = базовая линия ± "
                  f"{band:g} × MAE бэктеста. Это интервал ошибки модели, а не прогноз факторов.")
    if delta <= 1e-9:
        assumption += (" MAE бэктеста = 0: границы совпадают с базовой линией, "
                       "история описывается линейным трендом без остатка.")

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
        "assumption": assumption,
    }


def _infer_step(periods: pd.Series) -> pd.DateOffset:
    days = _median_step_days(periods) or 30.0
    if days >= 300:
        return pd.DateOffset(years=1)
    if days >= 170:
        return pd.DateOffset(months=6)
    if days >= 80:
        return pd.DateOffset(months=3)
    if days <= 2:
        return pd.DateOffset(days=1)
    if days <= 10:
        return pd.DateOffset(weeks=1)
    return pd.DateOffset(months=1)
