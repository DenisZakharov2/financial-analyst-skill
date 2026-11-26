"""Пример плагина: Roll-rate анализ (переходы между этапами просрочки)."""

from __future__ import annotations

import pandas as pd
from finanalyst.plugins import PluginContext, register_plugin


@register_plugin(
    name="roll_rate_analysis",
    version="1.0.0",
    api_version="1.0",
)
def roll_rate_analysis(tables: dict[str, pd.DataFrame], ctx: PluginContext) -> pd.DataFrame:
    """Рассчитывает roll-rate матрицу: вероятность перехода из одного PAR-бакета в другой.

    Roll-rate = доля контрактов, перешедших из бакета A в бакет B за период.
    Требует двух срезов портфеля (--compare-dirs).
    """
    # Для roll-rate нужны два среза — проверяем, есть ли сравнение
    # В текущей архитектуре таблицы загружены только для текущего среза
    # Поэтому возвращаем информативное сообщение
    return pd.DataFrame({
        "metric": ["roll_rate"],
        "value": [None],
        "note": ["Roll-rate требует два среза портфеля (--compare-dirs). Текущий срез: один."],
    })


# Метаданные
roll_rate_analysis._metric_name = "roll_rate_analysis"
roll_rate_analysis._display_name = "Roll-Rate Analysis"
roll_rate_analysis._description = "Матрица переходов между PAR-бакетами (roll-rate)"
roll_rate_analysis._required_tables = ["portfolio"]
roll_rate_analysis._required_columns = {"portfolio": ["balance", "par_bucket"]}
roll_rate_analysis._output_schema = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "metric": {"type": "string"},
            "value": {"type": ["number", "null"]},
            "note": {"type": "string"},
        },
        "required": ["metric", "value", "note"],
    },
}
