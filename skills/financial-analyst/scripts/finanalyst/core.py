"""Загрузка данных, нормализация периодов и контроль качества.

Модуль не зависит от конкретного источника: адаптеры приводят выгрузку
к таблицам из references/data-dictionary.md, дальше всё считается здесь.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

# --------------------------------------------------------------------------
# Загрузка
# --------------------------------------------------------------------------

TEXT_SUFFIXES = {".csv", ".xls", ".xlsx"}
WORKBOOK_SUFFIXES = {".xls", ".xlsx"}

KNOWN_TABLES = frozenset({
    "clients", "products", "portfolio", "accounts", "sales", "applications",
    "funnel_events", "transactions", "finance", "macro_deposits", "macro_loans",
    "macro_profit", "macro_securities", "macro_survey",
})


def load_tables(data_dir: Path) -> dict[str, pd.DataFrame]:
    """Прочитать все CSV/Excel из каталога. Имя файла = имя таблицы.

    Ошибка чтения не глотается: файл попадает в ``errors`` и возвращается
    вызывающему, чтобы CLI мог завершиться с кодом 2.
    """
    tables: dict[str, pd.DataFrame] = {}
    errors: list[str] = []
    if not data_dir.exists():
        raise FileNotFoundError(f"Каталог с данными не найден: {data_dir}")

    for path in sorted(data_dir.iterdir()):
        if path.suffix.lower() not in TEXT_SUFFIXES or not path.is_file():
            continue
        name = path.stem
        try:
            if path.suffix.lower() == ".csv":
                tables[name] = pd.read_csv(path, sep=None, engine="python", encoding="utf-8-sig")
            else:
                tables[name] = pd.read_excel(path, sheet_name=0)
        except Exception as exc:  # noqa: BLE001 - сообщаем пользователю как есть
            errors.append(f"{path.name}: {type(exc).__name__}: {exc}")

    if errors:
        raise DataLoadError(errors, tables)
    return tables


class DataLoadError(RuntimeError):
    def __init__(self, errors: list[str], tables: dict[str, pd.DataFrame]):
        super().__init__("; ".join(errors))
        self.errors = errors
        self.tables = tables


# --------------------------------------------------------------------------
# Периоды
# --------------------------------------------------------------------------

MONTHS_RU = {
    "январ": 1, "феврал": 2, "март": 3, "апрел": 4, "ма": 5, "июн": 6,
    "июл": 7, "август": 8, "сентябр": 9, "октябр": 10, "ноябр": 11, "декабр": 12,
}


def parse_period(value) -> tuple[pd.Timestamp | pd.NaT, str]:
    """Разобрать период в дату начала и тип (D/M/Q/Y).

    Поддерживаются: 2026-01, 2026-01-15, 01.2026, 1к02, 1кв02, 2020 год,
    январь 2020, даты Excel и pandas.
    """
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return pd.NaT, ""

    if isinstance(value, (pd.Timestamp, np.datetime64)):
        ts = pd.Timestamp(value)
        return ts.normalize(), "D" if ts.day != 1 else "M"

    if hasattr(value, "year") and hasattr(value, "month") and not isinstance(value, str):
        ts = pd.Timestamp(value.year, value.month, 1)
        return ts, "M"

    text = str(value).strip().lower().replace("ё", "е")
    if not text:
        return pd.NaT, ""

    match = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", text)
    if match:
        return pd.Timestamp(int(match[1]), int(match[2]), int(match[3])), "D"

    match = re.fullmatch(r"(\d{4})-(\d{2})", text)
    if match:
        return pd.Timestamp(int(match[1]), int(match[2]), 1), "M"

    match = re.fullmatch(r"(\d{1,2})[./](\d{4})", text)
    if match:
        return pd.Timestamp(int(match[2]), int(match[1]), 1), "M"

    match = re.fullmatch(r"(\d)\s*[кk][вb]?\s*(\d{2,4})", text)
    if match:
        quarter, year = int(match[1]), int(match[2])
        year = year + 2000 if year < 100 else year
        if 1 <= quarter <= 4:
            return pd.Timestamp(year, 3 * (quarter - 1) + 1, 1), "Q"

    match = re.fullmatch(r"(\d{4})\s*(год|г\.?)?", text)
    if match:
        return pd.Timestamp(int(match[1]), 1, 1), "Y"

    match = re.fullmatch(r"([а-я]+)\s*(\d{4})", text)
    if match:
        stem, year = match[1], int(match[2])
        month = next((num for key, num in MONTHS_RU.items() if stem.startswith(key)), None)
        if month:
            return pd.Timestamp(year, month, 1), "M"

    match = re.fullmatch(r"(\d{4})\s*[-/]\s*([а-я]+)", text)
    if match:
        year, stem = int(match[1]), match[2]
        month = next((num for key, num in MONTHS_RU.items() if stem.startswith(key)), None)
        if month:
            return pd.Timestamp(year, month, 1), "M"

    parsed = pd.to_datetime(text, errors="coerce", dayfirst=True)
    if pd.notna(parsed):
        return pd.Timestamp(parsed).normalize(), "D"
    return pd.NaT, ""


def canonical_period(value) -> str:
    """Привести период к ``YYYY-MM``; неразобранное значение вернуть как есть.

    Нужно, чтобы «2024-01», «01.2024» и «январь 2024» не расходились по разным
    строкам одной и той же таблицы.
    """
    start, _ = parse_period(value)
    if pd.isna(start):
        return str(value)
    return pd.Timestamp(start).strftime("%Y-%m")


def normalize_periods(df: pd.DataFrame, column: str = "period") -> pd.DataFrame:
    """Добавить period_start и period_type, отсортировать по дате."""
    if column not in df.columns:
        return df.copy()
    result = df.copy()
    pairs = result[column].map(parse_period)
    result["period_start"] = [p[0] for p in pairs]
    result["period_type"] = [p[1] for p in pairs]
    result = result.sort_values("period_start", kind="stable").reset_index(drop=True)
    return result


# --------------------------------------------------------------------------
# Контроль качества
# --------------------------------------------------------------------------

@dataclass
class Check:
    check_id: str
    status: str  # pass | warn | fail
    detail: str


@dataclass
class QualityReport:
    checks: list[Check] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def failed(self) -> list[Check]:
        return [c for c in self.checks if c.status == "fail"]

    @property
    def warned(self) -> list[Check]:
        return [c for c in self.checks if c.status == "warn"]

    def add(self, check_id: str, ok: bool, detail: str, *, warn_only: bool = False) -> None:
        status = "pass" if ok else ("warn" if warn_only else "fail")
        self.checks.append(Check(check_id, status, detail))

    def to_dict(self) -> dict:
        return {
            "checks": [c.__dict__ for c in self.checks],
            "failed": len(self.failed),
            "warned": len(self.warned),
        }


def _numeric(df: pd.DataFrame, column: str) -> pd.Series:
    return pd.to_numeric(df[column], errors="coerce") if column in df.columns else pd.Series(dtype=float)


REQUIRED_COLUMNS = {
    "portfolio": ("account_id", "client_id", "balance", "dpd"),
    "clients": ("client_id",),
    "products": ("product_code", "product_type"),
    "sales": ("application_id", "client_id", "funnel_stage"),
    "funnel_events": ("stage",),
    "finance": ("period", "product_code"),
}

TABLE_ALIASES = {"portfolio": "accounts", "sales": "applications"}


def validate_inputs(tables: dict[str, pd.DataFrame]) -> QualityReport:
    """Проверить входные таблицы до расчётов.

    Правило: fail останавливает расчёт в --strict, warn только логируется.
    Если обязательной колонки нет, проверка невозможна: добавляется warn
    ``<таблица>_required_columns``, чтобы пропуск не был тихим.
    """
    report = QualityReport()

    unknown = sorted(set(tables) - KNOWN_TABLES)
    if unknown:
        report.add("unknown_tables", False, f"Нераспознанные таблицы: {', '.join(unknown)}", warn_only=True)

    for name, columns in REQUIRED_COLUMNS.items():
        table = tables.get(name)
        if table is None:
            alias = TABLE_ALIASES.get(name)
            table = tables.get(alias) if alias else None
        if table is None:
            continue
        missing = sorted(set(columns) - set(table.columns))
        if missing:
            report.add(f"{name}_required_columns", False,
                       f"Нет колонок {', '.join(missing)}: часть проверок и разделов недоступна.",
                       warn_only=True)

    portfolio = tables.get("portfolio")
    if portfolio is None:
        portfolio = tables.get("accounts")
    clients = tables.get("clients")
    products = tables.get("products")
    sales = tables.get("sales")
    if sales is None:
        sales = tables.get("applications")
    finance = tables.get("finance")

    if portfolio is not None:
        if "account_id" in portfolio.columns:
            dup = int(portfolio["account_id"].duplicated().sum())
            report.add("portfolio_pk_unique", dup == 0, f"Дубли account_id: {dup}")
        balance = _numeric(portfolio, "balance")
        if len(balance):
            negatives = int((balance < 0).sum())
            report.add("portfolio_balance_non_negative", negatives == 0,
                       f"Отрицательных остатков: {negatives}", warn_only=True)
        dpd = _numeric(portfolio, "dpd")
        if len(dpd):
            report.add("portfolio_dpd_non_negative", bool((dpd >= 0).all()),
                       "Отрицательный dpd встречается" if (dpd < 0).any() else "OK")
        if clients is not None and "client_id" in portfolio.columns and "client_id" in clients.columns:
            coverage = portfolio["client_id"].isin(clients["client_id"]).mean()
            report.add("portfolio_fk_clients", coverage >= 0.99,
                       f"Покрытие client_id: {coverage:.1%}", warn_only=True)
        if products is not None and "product_code" in portfolio.columns:
            coverage = portfolio["product_code"].isin(products["product_code"]).mean()
            report.add("portfolio_fk_products", coverage >= 0.99,
                       f"Покрытие product_code: {coverage:.1%}", warn_only=True)
        if {"dpd", "par_bucket"} <= set(portfolio.columns):
            dpd = _numeric(portfolio, "dpd")
            bucket = portfolio["par_bucket"].astype(str).str.strip()
            expected = pd.cut(dpd, [-1, 0, 30, 60, 10**9], labels=["current", "30+", "60+", "90+"])
            mismatched = int((bucket != expected.astype(str)).sum())
            report.add("portfolio_dpd_bucket_consistent", mismatched == 0,
                       f"Несогласованных строк dpd/par_bucket: {mismatched}", warn_only=True)
        if "currency" in portfolio.columns:
            currencies = sorted(portfolio["currency"].dropna().astype(str).unique())
            if len(currencies) > 1:
                report.warnings.append(
                    "В портфеле несколько валют (%s): суммы не сопоставимы без курса." % ", ".join(currencies)
                )

    if clients is not None and "client_id" in clients.columns:
        dup = int(clients["client_id"].duplicated().sum())
        report.add("clients_pk_unique", dup == 0, f"Дубли client_id: {dup}")

    if products is not None and "product_code" in products.columns:
        dup = int(products["product_code"].duplicated().sum())
        report.add("products_pk_unique", dup == 0, f"Дубли product_code: {dup}")

    if sales is not None:
        key = "application_id" if "application_id" in sales.columns else None
        if key:
            dup = int(sales[key].duplicated().sum())
            report.add("sales_pk_unique", dup == 0,
                       f"Строк на заявку больше одной: {dup} (нужно для TAT и воронки)")
        if "amount" in sales.columns:
            amount = _numeric(sales, "amount")
            report.add("sales_amount_non_negative", bool((amount.fillna(0) >= 0).all()),
                       "Отрицательные суммы в заявках", warn_only=True)

    if finance is not None:
        if {"period", "product_code"} <= set(finance.columns):
            grain = finance[["period", "product_code"]].copy()
            grain["period"] = grain["period"].map(canonical_period)
            dup = int(grain.duplicated().sum())
            report.add("finance_grain_unique", dup == 0,
                       f"Дублей period+product_code: {dup}")
        if "product_code" in finance.columns:
            all_rows = int((finance["product_code"].astype(str).str.upper() == "ALL").sum())
            if all_rows:
                report.warnings.append(
                    f"В finance {all_rows} строк с product_code=ALL: при суммировании они удвоят итог."
                )
        if "period" in finance.columns:
            normalized = normalize_periods(finance)
            bad = int(normalized["period_start"].isna().sum())
            report.add("finance_periods_parsed", bad == 0, f"Неразобранных периодов: {bad}")

    survey = tables.get("macro_survey")
    if survey is not None:
        report.add("macro_survey_rows", len(survey) > 0, f"Строк: {len(survey)}")
        if "period_start" in survey.columns:
            bad = int(survey["period_start"].isna().sum())
            report.add("macro_survey_periods", bad == 0,
                       f"Неразобранных периодов: {bad}")
        if "value" in survey.columns:
            values = pd.to_numeric(survey["value"], errors="coerce")
            report.add("macro_survey_values", values.notna().mean() > 0.9,
                       f"Доля числовых значений: {values.notna().mean():.1%}", warn_only=True)

    for name in ("macro_deposits", "macro_loans", "macro_profit", "macro_securities"):
        table = tables.get(name)
        if table is None or "period" not in table.columns:
            continue
        normalized = normalize_periods(table)
        bad = int(normalized["period_start"].isna().sum())
        report.add(f"{name}_periods", bad == 0, f"Неразобранных периодов: {bad}")

    if not report.checks:
        report.add("inputs_present", False, "Не найдено ни одной известной таблицы")
    return report


def assert_no_blocking_failures(report: QualityReport) -> None:
    if report.failed:
        names = ", ".join(c.check_id for c in report.failed)
        raise ValueError(f"Контроль качества не пройден: {names}")
