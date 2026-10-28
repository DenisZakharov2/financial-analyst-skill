"""Адаптер выгрузки мониторинга предприятий (широкие листы Excel).

Ожидаемый формат: один лист = одна отрасль, строка заголовков в первых
семи строках, первая колонка — период, остальные — показатели.
"""

from __future__ import annotations

import re
from pathlib import Path

import numpy as np
import pandas as pd

SKIP_SHEET_MARKERS = ("спецификац", "участник", "оглавление", "содержание")
HEADER_MARKERS = ("период", "квартал", "отчетн", "дата")
MAX_HEADER_SCAN = 7


def _clean(value) -> str:
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def _find_header_row(raw: pd.DataFrame) -> int | None:
    for index in range(min(MAX_HEADER_SCAN, len(raw))):
        row = [_clean(value).lower() for value in raw.iloc[index].tolist()]
        if row and any(marker in row[0] for marker in HEADER_MARKERS):
            filled = sum(1 for value in row if value)
            if filled >= 2:
                return index
    return None


def _dedupe(headers: list[str]) -> list[str]:
    seen: dict[str, int] = {}
    result = []
    for header in headers:
        base = header or "без_имени"
        if base in seen:
            seen[base] += 1
            result.append(f"{base}_{seen[base]}")
        else:
            seen[base] = 0
            result.append(base)
    return result


def _parse_sheet(raw: pd.DataFrame, sheet: str, source: str) -> pd.DataFrame:
    header_index = _find_header_row(raw)
    if header_index is None:
        return pd.DataFrame()

    headers = _dedupe([_clean(value) for value in raw.iloc[header_index].tolist()])
    data = raw.iloc[header_index + 1:].copy()
    width = min(len(headers), data.shape[1])
    data = data.iloc[:, :width]
    data.columns = headers[:width]

    period_column = data.columns[0]
    data = data.dropna(subset=[period_column])
    if data.empty:
        return pd.DataFrame()

    rows = []
    for _, row in data.iterrows():
        period = row[period_column]
        if isinstance(period, float) and np.isnan(period):
            continue
        for column in data.columns[1:]:
            value = row[column]
            number = pd.to_numeric(value, errors="coerce")
            if pd.isna(number):
                continue
            rows.append({
                "period": period,
                "industry": sheet,
                "indicator": column,
                "value": float(number),
                "unit": "значение показателя",
                "source_file": source,
                "source_sheet": sheet,
            })
    return pd.DataFrame(rows)


def parse_workbook(path: Path) -> pd.DataFrame:
    """Разобрать все листы книги в длинную таблицу macro_survey."""
    workbook = pd.ExcelFile(path)
    frames = []
    for sheet in workbook.sheet_names:
        if any(marker in sheet.lower() for marker in SKIP_SHEET_MARKERS):
            continue
        raw = pd.read_excel(path, sheet_name=sheet, header=None)
        parsed = _parse_sheet(raw, sheet, path.name)
        if not parsed.empty:
            frames.append(parsed)
    if not frames:
        return pd.DataFrame(columns=["period", "industry", "indicator", "value", "unit",
                                     "source_file", "source_sheet"])
    return pd.concat(frames, ignore_index=True)


def choose_indicator(survey: pd.DataFrame) -> str | None:
    """Показатель с наибольшим покрытием: по нему строим графики по умолчанию."""
    if survey is None or survey.empty:
        return None
    coverage = (survey.groupby("indicator")["value"].count()
                      .sort_values(ascending=False))
    return str(coverage.index[0]) if len(coverage) else None
