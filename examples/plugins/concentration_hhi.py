"""Пример плагина: Индекс Герфиндаля-Хиршмана (HHI) для концентрации портфеля."""

from __future__ import annotations

import pandas as pd
from finanalyst.plugins import PluginContext, register_plugin


@register_plugin(
    name="concentration_hhi",
    version="1.0.0",
    api_version="1.0",
)
def concentration_hhi(tables: dict[str, pd.DataFrame], ctx: PluginContext) -> pd.DataFrame:
    """Рассчитывает HHI (Herfindahl-Hirschman Index) по типам продуктов иPAR-бакетам.

    HHI = sum(share_i^2), где share_i — доля i-го сегмента в общем балансе.
    Значения: < 1500 — низкая концентрация, 1500-2500 — умеренная, > 2500 — высокая.
    """
    portfolio = tables.get("portfolio")
    if portfolio is None or portfolio.empty:
        return pd.DataFrame({"metric": ["HHI"], "value": [None], "interpretation": ["Нет данных portfolio"]})

    if "balance" not in portfolio.columns:
        return pd.DataFrame({"metric": ["HHI"], "value": [None], "interpretation": ["Нет колонки balance"]})

    total_balance = portfolio["balance"].sum()
    if total_balance == 0:
        return pd.DataFrame({"metric": ["HHI"], "value": [0.0], "interpretation": ["Баланс равен 0"]})

    # HHI по типам продуктов
    results = []

    if "product_type" in portfolio.columns:
        by_product = portfolio.groupby("product_type", as_index=False)["balance"].sum()
        by_product["share"] = by_product["balance"] / total_balance
        by_product["share_sq"] = by_product["share"] ** 2
        hhi_product = by_product["share_sq"].sum() * 10000  # в базисных пунктах
        results.append({
            "dimension": "product_type",
            "metric": "HHI",
            "value": round(hhi_product, 1),
            "interpretation": _interpret_hhi(hhi_product),
        })

    # HHI по PAR-бакетам
    if "par_bucket" in portfolio.columns:
        by_par = portfolio.groupby("par_bucket", as_index=False)["balance"].sum()
        by_par["share"] = by_par["balance"] / total_balance
        by_par["share_sq"] = by_par["share"] ** 2
        hhi_par = by_par["share_sq"].sum() * 10000
        results.append({
            "dimension": "par_bucket",
            "metric": "HHI",
            "value": round(hhi_par, 1),
            "interpretation": _interpret_hhi(hhi_par),
        })

    # HHI по клиентам (топ-N)
    if "client_id" in portfolio.columns:
        by_client = portfolio.groupby("client_id", as_index=False)["balance"].sum()
        by_client = by_client.nlargest(100, "balance")  # топ-100 клиентов
        by_client["share"] = by_client["balance"] / total_balance
        by_client["share_sq"] = by_client["share"] ** 2
        hhi_client = by_client["share_sq"].sum() * 10000
        results.append({
            "dimension": "client_id (top100)",
            "metric": "HHI",
            "value": round(hhi_client, 1),
            "interpretation": _interpret_hhi(hhi_client),
        })

    return pd.DataFrame(results)


def _interpret_hhi(hhi: float) -> str:
    """Интерпретация значения HHI."""
    if hhi < 1500:
        return "Низкая концентрация"
    elif hhi < 2500:
        return "Умеренная концентрация"
    else:
        return "Высокая концентрация"


# Метаданные для декоратора
concentration_hhi._metric_name = "concentration_hhi"
concentration_hhi._display_name = "HHI Concentration Index"
concentration_hhi._description = "Индекс Герфиндаля-Хиршмана для оценки концентрации портфеля"
concentration_hhi._required_tables = ["portfolio"]
concentration_hhi._required_columns = {"portfolio": ["balance"]}
concentration_hhi._output_schema = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "dimension": {"type": "string"},
            "metric": {"type": "string"},
            "value": {"type": "number"},
            "interpretation": {"type": "string"},
        },
        "required": ["dimension", "metric", "value", "interpretation"],
    },
}
