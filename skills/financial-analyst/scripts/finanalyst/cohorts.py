"""Когортный анализ: старт когорты и retention-кривые.

Соглашения — references/cohorts.md:

* старт когорты — месяц первого продукта (``portfolio.open_date``), иначе
  ``registration_date`` из ``clients``;
* клиент активен в месяце, если у него есть счёт с ненулевым остатком, который
  ещё не закрыт к концу месяца;
* результат — доля активных клиентов когорты по возрасту в месяцах (retention %,
  возраст 0 = месяц старта).
"""

from __future__ import annotations

import pandas as pd

from .core import parse_period


def _period_ym(value) -> str | None:
    """Перевести дату/строку в ``YYYY-MM``, ``None`` если не разобралось."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    start, _ = parse_period(value)
    if pd.isna(start):
        return None
    return f"{int(start.year):04d}-{int(start.month):02d}"


def _ym_to_int(ym: str) -> int:
    """Перевести ``YYYY-MM`` в число месяцев с начала эры."""
    year, month = map(int, ym.split("-"))
    return year * 12 + month - 1


def cohort_starts(
    clients: pd.DataFrame | None,
    portfolio: pd.DataFrame | None,
) -> pd.Series:
    """Месяц когорты каждого клиента (``YYYY-MM``).

    Приоритет: первый продукт из ``portfolio`` (минимальный ``open_date``);
    если портфеля нет — ``registration_date`` из ``clients``. Клиенты без
    разбираемой даты старта в результат не попадают.
    """
    starts: dict[object, str] = {}
    if portfolio is not None and "client_id" in portfolio.columns and "open_date" in portfolio.columns:
        work = portfolio.dropna(subset=["client_id", "open_date"])
        first = (
            work.assign(ym=[_period_ym(value) for value in work["open_date"]])
            .dropna(subset=["ym"])
            .groupby("client_id")["ym"]
            .min()
        )
        starts.update(first.to_dict())
    if clients is not None and "client_id" in clients.columns and "registration_date" in clients.columns:
        for client, reg_date in zip(clients["client_id"], clients["registration_date"]):
            if client in starts:
                continue
            ym = _period_ym(reg_date)
            if ym is not None:
                starts[client] = ym
    return pd.Series(starts, dtype="object")


def retention_curves(
    starts: pd.Series,
    portfolio: pd.DataFrame | None,
    max_age: int = 12,
) -> pd.DataFrame:
    """Доля активных клиентов когорты по возрасту в месяцах.

    Строки — возраст в месяцах (0 — месяц старта), столбцы — месяцы когорт,
    значения — retention в процентах. Пустой фрейм, если нет когорт или
    портфеля. Когорты без полного горизонта считаются на доступных месяцах
    (целзурирование «показать с пометкой»).
    """
    if starts.empty or portfolio is None or "client_id" not in portfolio.columns:
        return pd.DataFrame()

    work = portfolio.dropna(subset=["client_id"]).copy()
    has_open = "open_date" in work.columns
    has_close = "close_date" in work.columns
    if has_balance := "balance" in work.columns:
        work["balance"] = pd.to_numeric(work["balance"], errors="coerce")

    horizon = max(_ym_to_int(ym) for ym in starts) + max_age

    client_months: dict[object, set[int]] = {}
    for _, row in work.iterrows():
        open_ym = _period_ym(row["open_date"]) if has_open else None
        if open_ym is None:
            continue
        if has_balance and (pd.isna(row["balance"]) or float(row["balance"]) == 0.0):
            continue
        close_ym = None
        if has_close and pd.notna(row["close_date"]):
            close_ym = _period_ym(row["close_date"])
        month = _ym_to_int(open_ym)
        close_n = _ym_to_int(close_ym) if close_ym is not None else None
        months = client_months.setdefault(row["client_id"], set())
        while month <= horizon:
            months.add(month)
            if close_n is not None and month >= close_n:
                break
            month += 1

    cohort_of: dict[object, str] = {}
    cohort_size: dict[str, int] = {}
    for client, ym in starts.items():
        cohort_of[client] = ym
        cohort_size[ym] = cohort_size.get(ym, 0) + 1

    retained: dict[int, dict[str, float]] = {age: {} for age in range(max_age + 1)}
    for client, months in client_months.items():
        if client not in cohort_of:
            continue
        cohort_ym = cohort_of[client]
        base = _ym_to_int(cohort_ym)
        for age in range(max_age + 1):
            if base + age in months:
                retained[age][cohort_ym] = retained[age].get(cohort_ym, 0) + 1

    frame = pd.DataFrame(retained).T
    if frame.empty:
        return frame
    frame = frame / pd.Series(cohort_size) * 100
    frame.index.name = "age_months"
    return frame.sort_index()
