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
    """Уникализировать заголовки столбцов.

    Если в исходнике уже есть «Итого_1», а «Итого» повторяется, простое
    добавление суффикса дало бы две колонки с одинаковым именем.
    """
    seen: dict[str, int] = {}
    used: set[str] = set()
    result: list[str] = []
    for header in headers:
        base = header or "без_имени"
        if base not in used:
            seen[base] = 0
            used.add(base)
            result.append(base)
            continue
        index = seen.get(base, 0) + 1
        candidate = f"{base}_{index}"
        while candidate in used:
            index += 1
            candidate = f"{base}_{index}"
        seen[base] = index
        used.add(candidate)
        result.append(candidate)
    return result


def _to_float_series(series: pd.Series) -> pd.Series:
    """Привести значения к числам.

    В книгах ЦБ РФ дробные числа записаны с десятичной запятой и пробелами
    («1 234,56»): без нормализации они превращались в NaN и выпадали из
    выборки вместе со строкой.
    """
    if pd.api.types.is_numeric_dtype(series):
        return pd.to_numeric(series, errors="coerce")
    cleaned = (
        series.astype(str)
        .str.replace("\u00a0", "", regex=False)
        .str.replace(" ", "", regex=False)
        .str.replace(",", ".", regex=False)
        .str.replace("−", "-", regex=False)
        .str.strip()
    )
    return pd.to_numeric(cleaned, errors="coerce")


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

    melted = data.melt(id_vars=[period_column], var_name="indicator",
                       value_name="value")
    melted["value"] = _to_float_series(melted["value"])
    melted = melted.dropna(subset=["value", period_column])
    melted = melted.assign(
        period=melted[period_column],
        industry=sheet,
        unit="значение показателя",
        source_file=source,
        source_sheet=sheet,
    )
    return melted[["period", "industry", "indicator", "value", "unit",
                   "source_file", "source_sheet"]].copy()


def parse_workbook(path: Path) -> pd.DataFrame:
    """Разобрать все листы книги в длинную таблицу macro_survey."""
    frames = []
    # Контекстный менеджер закрывает книгу: без него на Windows остаётся
    # незакрытый дескриптор, а файл читается заново для каждого листа.
    with pd.ExcelFile(path) as workbook:
        for sheet in workbook.sheet_names:
            if any(marker in sheet.lower() for marker in SKIP_SHEET_MARKERS):
                continue
            raw = pd.read_excel(workbook, sheet_name=sheet, header=None)
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
