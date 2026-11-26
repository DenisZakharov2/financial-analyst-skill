"""Пример плагина: Пользовательская метрика рентабельности по продуктам."""

from __future__ import annotations

import pandas as pd
from finanalyst.plugins import PluginContext, register_plugin


@register_plugin(
    name="product_profitability",
    version="1.0.0",
    api_version="1.0",
)
def product_profitability(tables: dict[str, pd.DataFrame], ctx: PluginContext) -> pd.DataFrame:
    """Рассчитывает рентабельность по продуктам: NIM, Cost of Risk, ROA.

    Требует таблицы finance с разбивкой по продуктам и portfolio для баланса.
    """
    finance = tables.get("finance")
    portfolio = tables.get("portfolio")
    tables.get("products")

    if finance is None or finance.empty:
        return pd.DataFrame({"metric": ["profitability"], "value": [None], "note": ["Нет таблицы finance"]})

    if portfolio is None or portfolio.empty:
        return pd.DataFrame({"metric": ["profitability"], "value": [None], "note": ["Нет таблицы portfolio"]})

    # Проверяем нужные колонки в finance
    required_finance = ["period", "interest_income", "interest_expense", "provisions", "fee_income"]
    missing = [c for c in required_finance if c not in finance.columns]
    if missing:
        return pd.DataFrame({
            "metric": ["profitability"],
            "value": [None],
            "note": [f"В finance нет колонок: {missing}"],
        })

    # Последний период
    last_period = finance["period"].max()
    fin_last = finance[finance["period"] == last_period].copy()

    # Баланс по продуктам из portfolio
    if "product_type" not in portfolio.columns:
        return pd.DataFrame({
            "metric": ["profitability"],
            "value": [None],
            "note": ["В portfolio нет колонки product_type"],
        })

    balance_by_product = portfolio.groupby("product_type", as_index=False)["balance"].sum()
    total_balance = balance_by_product["balance"].sum()

    # Если в finance есть product_type — джойним, иначе считаем общие метрики
    if "product_type" in fin_last.columns:
        merged = fin_last.merge(balance_by_product, on="product_type", how="left")
        merged["balance"] = merged["balance"].fillna(0)
    else:
        # Распределяем пропорционально балансу
        merged = balance_by_product.copy()
        for col in ["interest_income", "interest_expense", "provisions", "fee_income"]:
            merged[col] = fin_last[col].sum() * merged["balance"] / total_balance

    # Расчёт метрик
    merged["nim"] = (merged["interest_income"] - merged["interest_expense"]) / merged["balance"].replace(0, pd.NA)
    merged["cost_of_risk"] = merged["provisions"] / merged["balance"].replace(0, pd.NA)
    merged["net_income"] = (merged["interest_income"] - merged["interest_expense"]
                            - merged["provisions"] + merged["fee_income"])
    merged["roa"] = merged["net_income"] / merged["balance"].replace(0, pd.NA)

    # Формируем результат
    result_rows = []
    for _, row in merged.iterrows():
        if pd.isna(row["balance"]) or row["balance"] == 0:
            continue
        result_rows.append({
            "product_type": row["product_type"],
            "balance": round(row["balance"], 2),
            "nim_pct": round(row["nim"] * 100, 2) if pd.notna(row["nim"]) else None,
            "cost_of_risk_pct": round(row["cost_of_risk"] * 100, 2) if pd.notna(row["cost_of_risk"]) else None,
            "roa_pct": round(row["roa"] * 100, 2) if pd.notna(row["roa"]) else None,
        })

    if not result_rows:
        return pd.DataFrame({"metric": ["profitability"], "value": [None], "note": ["Нет данных для расчёта"]})

    return pd.DataFrame(result_rows)


# Метаданные
product_profitability._metric_name = "product_profitability"
product_profitability._display_name = "Product Profitability"
product_profitability._description = "Рентабельность по продуктам: NIM, Cost of Risk, ROA"
product_profitability._required_tables = ["finance", "portfolio"]
product_profitability._required_columns = {
    "finance": ["period", "interest_income", "interest_expense", "provisions", "fee_income"],
    "portfolio": ["balance", "product_type"],
}
product_profitability._output_schema = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "product_type": {"type": "string"},
            "balance": {"type": "number"},
            "nim_pct": {"type": ["number", "null"]},
            "cost_of_risk_pct": {"type": ["number", "null"]},
            "roa_pct": {"type": ["number", "null"]},
        },
        "required": ["product_type", "balance", "nim_pct", "cost_of_risk_pct", "roa_pct"],
    },
}
