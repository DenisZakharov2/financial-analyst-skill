"""Клиентская выгрузка: CSV/Excel с таблицами clients, products, portfolio, sales…"""

from __future__ import annotations

import pandas as pd

from ..core import REQUIRED_COLUMNS

ALIASES = {
    "accounts": "portfolio",
    "applications": "sales",
    "funnel": "funnel_events",
    "transactions": "transactions",
}


def normalize(tables: dict[str, pd.DataFrame]) -> tuple[dict[str, pd.DataFrame], list[str]]:
    """Переименовать синонимы таблиц и привести имена колонок к нижнему регистру."""
    notes: list[str] = []
    result: dict[str, pd.DataFrame] = {}

    for name, frame in tables.items():
        key = ALIASES.get(name, name)
        if key in result:
            notes.append(f"Таблица {key} встречается дважды ({name}); использована первая.")
            continue
        work = frame.copy()
        work.columns = [str(column).strip().lower() for column in work.columns]
        result[key] = work

    for table, columns in REQUIRED_COLUMNS.items():
        if table not in result:
            continue
        missing = sorted(set(columns) - set(result[table].columns))
        if missing:
            notes.append(f"{table}: нет колонок {', '.join(missing)} — раздел будет пропущен.")
    return result, notes
