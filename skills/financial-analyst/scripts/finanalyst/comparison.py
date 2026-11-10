"""Сравнение нескольких выгрузок (Multi-Dataset Comparison): сверка схем и портфельный Bridge.

Модуль предназначен для пакета `finanalyst` (skills/financial-analyst/scripts/finanalyst/comparison.py).
Не ломает сигнатуру `load_tables()` и вместо некорректных двухвыборочных p-value на связанных
годовых портфелях строит точный клиентский Bridge: New, Churned, Retained Expansion, Retained Contraction.
"""

from __future__ import annotations

import pandas as pd

from .core import drop_total_rows


def compare_schemas(
    tables_base: dict[str, pd.DataFrame],
    tables_target: dict[str, pd.DataFrame],
) -> dict:
    """Сверить список таблиц и колонок между базовой и целевой выгрузками."""
    base_names = set(tables_base.keys())
    target_names = set(tables_target.keys())
    common = sorted(base_names & target_names)

    diff_rows: list[dict] = []
    for name in common:
        df_b = tables_base[name]
        df_t = tables_target[name]
        cols_b = set(df_b.columns) if df_b is not None else set()
        cols_t = set(df_t.columns) if df_t is not None else set()
        diff_rows.append(
            {
                "table": name,
                "rows_base": int(len(df_b)) if df_b is not None else 0,
                "rows_target": int(len(df_t)) if df_t is not None else 0,
                "rows_delta": (int(len(df_t)) if df_t is not None else 0)
                - (int(len(df_b)) if df_b is not None else 0),
                "missing_in_target": ", ".join(sorted(cols_b - cols_t)) or "нет",
                "added_in_target": ", ".join(sorted(cols_t - cols_b)) or "нет",
                "schema_compatible": cols_b == cols_t,
            }
        )
    return {
        "common_tables": common,
        "only_in_base": sorted(base_names - target_names),
        "only_in_target": sorted(target_names - base_names),
        "table_summary": pd.DataFrame(diff_rows),
    }


def portfolio_bridge(
    portfolio_base: pd.DataFrame,
    portfolio_target: pd.DataFrame,
    entity_col: str = "client_id",
) -> dict:
    """Разложить изменение баланса портфеля между двумя выгрузками на клиентские корзины (Bridge)."""
    for df in (portfolio_base, portfolio_target):
        if df is None or df.empty or entity_col not in df.columns or "balance" not in df.columns:
            return {
                "available": False,
                "bridge_table": pd.DataFrame(),
                "assumption": f"В таблицах портфеля отсутствуют колонки '{entity_col}' или 'balance'.",
            }

    b, _ = drop_total_rows(portfolio_base.copy(), group=())
    t, _ = drop_total_rows(portfolio_target.copy(), group=())
    if "status" in b.columns:
        b = b[b["status"].astype(str).str.strip().str.lower() != "закрыт"]
    if "status" in t.columns:
        t = t[t["status"].astype(str).str.strip().str.lower() != "закрыт"]

    b["bal_0"] = pd.to_numeric(b["balance"], errors="coerce").fillna(0.0)
    t["bal_1"] = pd.to_numeric(t["balance"], errors="coerce").fillna(0.0)

    b_agg = b.groupby(entity_col, as_index=False)["bal_0"].sum()
    t_agg = t.groupby(entity_col, as_index=False)["bal_1"].sum()
    b_agg = b_agg[b_agg["bal_0"] > 0]
    t_agg = t_agg[t_agg["bal_1"] > 0]

    merged = pd.merge(b_agg, t_agg, on=entity_col, how="outer", indicator=True)
    merged["bal_0"] = merged["bal_0"].fillna(0.0)
    merged["bal_1"] = merged["bal_1"].fillna(0.0)
    merged["delta"] = merged["bal_1"] - merged["bal_0"]

    cond_new = merged["_merge"] == "right_only"
    cond_churn = merged["_merge"] == "left_only"
    cond_exp = (merged["_merge"] == "both") & (merged["delta"] > 0.0)
    cond_con = (merged["_merge"] == "both") & (merged["delta"] < 0.0)
    cond_flat = (merged["_merge"] == "both") & (merged["delta"] == 0.0)

    buckets = [
        ("1. Новые клиенты (New)", merged[cond_new]),
        ("2. Ушедшие / Закрытые (Churned)", merged[cond_churn]),
        ("3. Рост удержанных (Retained Expansion)", merged[cond_exp]),
        ("4. Сжатие удержанных (Retained Contraction)", merged[cond_con]),
        ("5. Без изменений удержанных (Retained Flat)", merged[cond_flat]),
    ]

    total_0 = float(merged["bal_0"].sum())
    total_1 = float(merged["bal_1"].sum())
    total_delta = total_1 - total_0

    rows: list[dict] = []
    for label, sub in buckets:
        rows.append(
            {
                "bucket": label,
                "clients_count": int(len(sub)),
                "balance_base": round(float(sub["bal_0"].sum()), 2),
                "balance_target": round(float(sub["bal_1"].sum()), 2),
                "balance_delta": round(float(sub["delta"].sum()), 2),
                "contribution_to_growth_pct": (
                    round(float(sub["delta"].sum()) / total_0 * 100.0, 2) if total_0 > 0 else 0.0
                ),
            }
        )

    retained_count = int((merged["_merge"] == "both").sum())
    base_count = int(len(b_agg))
    retention_rate_pct = round(retained_count / base_count * 100.0, 2) if base_count > 0 else 0.0
    nrr_pct = (
        round(float(merged[merged["_merge"] == "both"]["bal_1"].sum()) / total_0 * 100.0, 2)
        if total_0 > 0
        else 0.0
    )

    return {
        "available": True,
        "balance_base": round(total_0, 2),
        "balance_target": round(total_1, 2),
        "balance_delta": round(total_delta, 2),
        "balance_growth_pct": round(total_delta / total_0 * 100.0, 2) if total_0 > 0 else 0.0,
        "client_retention_pct": retention_rate_pct,
        "net_dollar_retention_pct": nrr_pct,
        "bridge_table": pd.DataFrame(rows),
        "assumption": (
            f"Bridge-анализ выполнен по ключу '{entity_col}' после исключения итоговых строк ALL и закрытых счетов. "
            f"Сумма пяти корзин точно равна общему изменению портфеля ({total_delta:,.2f})."
        ),
    }


def _safe_delta_pct(v0: float, v1: float) -> float | None:
    """Относительное изменение с честной обработкой нулевой базы.

    Если в базовом срезе показатель был нулевым, а в целевом — нет, процент
    изменения не определён: считать его нулём нельзя, это читается как
    «динамики нет». В этом случае возвращается ``None`` (в отчёте — прочерк),
    а сам факт появления показателя фиксируется в ``delta_abs``.
    """
    if abs(v0) <= 1e-9:
        return 0.0 if abs(v1) <= 1e-9 else None
    return round((v1 - v0) / abs(v0) * 100.0, 2)


def compare_snapshots(
    tables_base: dict[str, pd.DataFrame],
    tables_target: dict[str, pd.DataFrame],
    label_base: str = "base",
    label_target: str = "target",
) -> dict:
    """Комплексное сравнение двух выгрузок: схема, портфельный Bridge и ключевые финансовые дельты."""
    schema_report = compare_schemas(tables_base, tables_target)
    port_b = tables_base.get("portfolio")
    if port_b is None:
        port_b = tables_base.get("accounts", pd.DataFrame())
    port_t = tables_target.get("portfolio")
    if port_t is None:
        port_t = tables_target.get("accounts", pd.DataFrame())
    bridge_report = portfolio_bridge(port_b, port_t)

    fin_b = tables_base.get("finance")
    fin_t = tables_target.get("finance")
    fin_deltas: list[dict] = []
    if fin_b is not None and fin_t is not None and not fin_b.empty and not fin_t.empty:
        fin_b_clean, _ = drop_total_rows(fin_b, group=("period",) if "period" in fin_b.columns else ())
        fin_t_clean, _ = drop_total_rows(fin_t, group=("period",) if "period" in fin_t.columns else ())
        num_cols = [
            c
            for c in (
                "interest_income", "fee_income", "premium",
                "funding_cost", "opex", "reserve_expense", "tax", "marketing_cost",
            )
            if c in fin_b_clean.columns and c in fin_t_clean.columns
        ]
        for col in num_cols:
            v0 = float(pd.to_numeric(fin_b_clean[col], errors="coerce").fillna(0.0).sum())
            v1 = float(pd.to_numeric(fin_t_clean[col], errors="coerce").fillna(0.0).sum())
            delta = v1 - v0
            pct = _safe_delta_pct(v0, v1)
            fin_deltas.append(
                {
                    "metric": col,
                    f"value_{label_base}": round(v0, 2),
                    f"value_{label_target}": round(v1, 2),
                    "delta_abs": round(delta, 2),
                    "delta_pct": pct,
                    "delta_pct_note": "" if pct is not None else "в базовом срезе показатель был нулевым",
                }
            )

    return {
        "available": True,
        "labels": (label_base, label_target),
        "schema": schema_report,
        "portfolio_bridge": bridge_report,
        "finance_comparison": pd.DataFrame(fin_deltas),
        "assumption": (
            f"Сравнение срезов '{label_base}' и '{label_target}' выполнено детерминированно (SchemaDiff + "
            "Portfolio Bridge + P&L Variance) без искажения связанных выборок независимыми тестами."
        ),
    }
