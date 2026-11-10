"""Клиентская выгрузка: CSV/Excel с таблицами clients, products, portfolio, sales…"""

from __future__ import annotations

import pandas as pd

ALIASES = {
    "accounts": "portfolio",
    "applications": "sales",
    "funnel": "funnel_events",
    "transactions": "transactions",
}


def normalize(tables: dict[str, pd.DataFrame]) -> tuple[dict[str, pd.DataFrame], list[str]]:
    """Переименовать синонимы таблиц и привести имена колонок к нижнему регистру.

    Канонические имена (``portfolio``, ``sales``, ``funnel_events``) имеют
    приоритет над синонимами (``accounts``, ``applications``, ``funnel``)
    независимо от алфавитного порядка файлов: иначе ``accounts.csv``
    вытеснял бы ``portfolio.csv``, а ``applications.csv`` — ``sales.csv``.
    """
    notes: list[str] = []
    result: dict[str, pd.DataFrame] = {}

    def _is_alias(name: str) -> bool:
        target = ALIASES.get(name)
        return target is not None and target != name

    # Сначала канонические таблицы, затем синонимы.
    ordered = sorted(tables.items(), key=lambda item: (_is_alias(item[0]), item[0]))

    for name, frame in ordered:
        key = ALIASES.get(name, name)
        if key in result:
            notes.append(
                f"Таблица {key} встречается дважды ({name} и каноническая); "
                "использована каноническая таблица."
            )
            continue
        work = frame.copy()
        work.columns = [str(column).strip().lower() for column in work.columns]
        result[key] = work

    return result, notes
