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
    "funnel_events", "funnel", "transactions", "finance", "macro_deposits",
    "macro_loans", "macro_profit", "macro_securities", "macro_survey",
})

# Кодировки CSV в порядке убывания вероятности: выгрузка UTF-8 с BOM, потом
# cp1251 (типичная выгрузка 1С/АБС), потом utf-8 без BOM.
CSV_ENCODINGS = ("utf-8-sig", "cp1251", "utf-8")
CSV_SEPARATORS = (",", ";", "\t", "|")


def _detect_separator(path: Path) -> str | None:
    """Определить разделитель CSV по строке заголовка.

    Если в заголовке нет ни одного разделителя, файл одноколоночный:
    возвращается ``None``, и файл читается без разделителя. Раньше для таких
    файлов применялся ``sep=None`` с движком Python, который «находил» в
    одноколоночном файле вымышленный разделитель и портил шапку
    (``value`` превращался в ``alue`` плюс колонка ``Unnamed: 0``).
    """
    header = ""
    try:
        raw = path.read_bytes().split(b"\n", 1)[0]
    except OSError:
        return None
    for encoding in CSV_ENCODINGS:
        try:
            header = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if not header:
        return None
    counts = {sep: header.count(sep) for sep in CSV_SEPARATORS}
    best = max(counts, key=lambda sep: counts[sep])
    return best if counts[best] > 0 else None


def _read_csv_flexible(path: Path) -> pd.DataFrame:
    """Прочитать CSV с автоопределением кодировки и разделителя.

    Порядок кодировок: ``utf-8-sig`` → ``cp1251`` → ``utf-8``. Файл в cp1251
    раньше приводил к ``UnicodeDecodeError`` и ронял весь прогон.
    """
    separator = _detect_separator(path)
    last_error: Exception | None = None
    for encoding in CSV_ENCODINGS:
        try:
            if separator:
                return pd.read_csv(path, sep=separator, encoding=encoding)
            return pd.read_csv(path, encoding=encoding)
        except UnicodeDecodeError as exc:
            last_error = exc
            continue
        except pd.errors.EmptyDataError:
            return pd.DataFrame()
        except Exception as exc:  # noqa: BLE001 - сообщаем пользователю как есть
            last_error = exc
            break
    if last_error is not None:
        raise last_error
    return pd.read_csv(path, encoding=CSV_ENCODINGS[0])


def load_tables(data_dir: Path, skip_patterns: tuple[str, ...] = ()) -> dict[str, pd.DataFrame]:
    """Прочитать все CSV/Excel из каталога. Имя файла = имя таблицы.

    Ошибка чтения не глотается: файл попадает в ``errors`` и возвращается
    вызывающему, чтобы CLI мог завершиться с кодом 2.

    ``skip_patterns`` — префиксы имён файлов (без расширения), которые нужно пропустить.
    """
    tables: dict[str, pd.DataFrame] = {}
    errors: list[str] = []
    if not data_dir.exists():
        raise FileNotFoundError(f"Каталог с данными не найден: {data_dir}")

    for path in sorted(data_dir.iterdir()):
        if path.suffix.lower() not in TEXT_SUFFIXES or not path.is_file():
            continue
        if any(path.stem.startswith(pattern) for pattern in skip_patterns):
            continue
        name = path.stem
        try:
            if path.suffix.lower() == ".csv":
                tables[name] = _read_csv_flexible(path)
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
    "январ": 1, "янв": 1,
    "феврал": 2, "фев": 2,
    "март": 3, "мар": 3,
    "апрел": 4, "апр": 4,
    "май": 5, "мая": 5, "мае": 5, "маем": 5,
    "июн": 6, "июль": 7, "июл": 7,
    "август": 8, "авг": 8,
    "сентябр": 9, "сент": 9, "сен": 9,
    "октябр": 10, "окт": 10,
    "ноябр": 11, "ноя": 11,
    "декабр": 12, "дек": 12,
}


def _timestamp(year, month=1, day=1) -> pd.Timestamp | pd.NaT:
    """Дата из чисел; невозможная дата (`2024-13`, 30 февраля) даёт NaT, а не исключение.

    Разбор периода идёт по данным пользователя, поэтому «13-й месяц» — это
    неразобранный период (fail в контроле качества), а не падение с трейсбеком.
    """
    try:
        return pd.Timestamp(year=int(year), month=int(month), day=int(day))
    except (TypeError, ValueError):
        return pd.NaT


def _typed(timestamp: pd.Timestamp | pd.NaT, kind: str) -> tuple[pd.Timestamp | pd.NaT, str]:
    """Пара (дата начала, тип периода); неразобранная дата — NaT и пустой тип."""
    if pd.isna(timestamp):
        return pd.NaT, ""
    return timestamp, kind


def _period_to_ym(period_tuple: tuple[pd.Timestamp | pd.NaT, str]) -> str:
    """Convert parse_period result to YYYY-MM string without pandas datetime ops."""
    start, _ = period_tuple
    if pd.isna(start):
        return ""
    # Use year/month attributes directly to avoid strftime/to_period segfault
    try:
        return f"{int(start.year):04d}-{int(start.month):02d}"
    except (AttributeError, ValueError):
        # Fallback: try to extract from string representation
        s = str(start)
        if len(s) >= 7 and s[4] == "-":
            return s[:7]
        return ""


def parse_period(value) -> tuple[pd.Timestamp | pd.NaT, str]:
    """Разобрать период в дату начала и тип (D/M/Q/Y).

    Поддерживаются: 2026-01, 2026-01-15, 15.01.2026, 01.2026, 1к02, 1кв02,
    2020 год, январь 2020, даты Excel и pandas. Невозможная дата (`2024-13`,
    `2024-02-30`) даёт `NaT` и пустой тип: дальше её ловит контроль качества.
    """
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return pd.NaT, ""

    if isinstance(value, (pd.Timestamp, np.datetime64)):
        ts = pd.Timestamp(value)
        return ts.normalize(), "D" if ts.day != 1 else "M"

    if hasattr(value, "year") and hasattr(value, "month") and not isinstance(value, str):
        day = getattr(value, "day", 1)
        kind = "D" if day != 1 else "M"
        return _typed(_timestamp(value.year, value.month, day), kind)

    text = str(value).strip().lower().replace("ё", "е")
    if not text:
        return pd.NaT, ""

    # Отсекаем штамп времени ISO/SQL (YYYY-MM-DD HH:MM:SS / YYYY-MM-DDTHH:MM:SS)
    match = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})(?:[t\s]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:z|[+-]\d{2}:?\d{2})?)?", text)
    if match:
        day = int(match[3])
        # 1-е число месяца — это месячный период, а не дневной:
        # так «2024-05-01» и Timestamp("2024-05-01") трактуются одинаково.
        return _typed(_timestamp(int(match[1]), int(match[2]), day), "D" if day != 1 else "M")

    match = re.fullmatch(r"(\d{2})[\./](\d{2})[\./](\d{4})", text)
    if match:
        day = int(match[1])
        return _typed(_timestamp(int(match[3]), int(match[2]), day), "D" if day != 1 else "M")

    match = re.fullmatch(r"(\d{4})-(\d{2})", text)
    if match:
        return _typed(_timestamp(int(match[1]), int(match[2]), 1), "M")

    match = re.fullmatch(r"(\d{1,2})[\./](\d{4})", text)
    if match:
        return _typed(_timestamp(int(match[2]), int(match[1]), 1), "M")

    match = re.fullmatch(r"(\d)\s*[кk][вb]?\s*(\d{2,4})", text)
    if match:
        quarter, year = int(match[1]), int(match[2])
        year = year + 2000 if year < 100 else year
        if 1 <= quarter <= 4:
            return _typed(_timestamp(year, 3 * (quarter - 1) + 1, 1), "Q")

    match = re.fullmatch(r"(\d{4})\s*(год|г\.?)?", text)
    if match:
        return _typed(_timestamp(int(match[1]), 1, 1), "Y")

    match = re.fullmatch(r"([а-я]+)\s*(\d{4})", text)
    if match:
        stem, year = match[1], int(match[2])
        month = next((num for key, num in MONTHS_RU.items() if stem.startswith(key)), None)
        if month:
            return _typed(_timestamp(year, month, 1), "M")

    match = re.fullmatch(r"(\d{4})\s*[-/]\s*([а-я]+)", text)
    if match:
        year, stem = int(match[1]), match[2]
        month = next((num for key, num in MONTHS_RU.items() if stem.startswith(key)), None)
        if month:
            return _typed(_timestamp(year, month, 1), "M")

    return pd.NaT, ""


def period_grain(values) -> str:
    """Зерно колонки периодов: ``D`` (дни) или ``M`` (месяцы и выше).

    Если хотя бы одно значение указывает на конкретный день (число не 1),
    колонка считается дневной. Иначе месячная колонка, заданная датами
    вида ``2024-05-01``, не была бы принята за дневную детализацию.
    """
    for value in values:
        start, kind = parse_period(value)
        if pd.isna(start):
            continue
        if kind == "D":
            return "D"
    return "M"


def period_ym(value) -> str:
    """Месячный бакет периода ``YYYY-MM`` — ключ для агрегации и графиков."""
    start, _ = parse_period(value)
    if pd.isna(start):
        return ""
    return _period_to_ym((start, "M"))


def canonical_period(value, grain: str | None = None) -> str:
    """Привести период к каноническому виду.

    При ``grain="D"`` возвращается полная дата ``YYYY-MM-DD``, иначе месяц
    ``YYYY-MM``. Если зерно не задано, оно определяется по самому значению:
    конкретный день — ``YYYY-MM-DD``, месяц, квартал и год — ``YYYY-MM``.

    Нужно, чтобы «2024-01», «01.2024» и «январь 2024» не расходились по разным
    строкам одной и той же таблицы, а дневная детализация не теряла дни.
    """
    # Быстрая проверка: если уже в формате YYYY-MM, вернуть как есть
    if grain is None and isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}", value):
        return value
    start, kind = parse_period(value)
    if pd.isna(start):
        return str(value)
    if grain == "D" or (grain is None and kind == "D"):
        try:
            return f"{int(start.year):04d}-{int(start.month):02d}-{int(start.day):02d}"
        except (AttributeError, ValueError):
            return str(value)
    return _period_to_ym((start, kind))


def normalize_periods(df: pd.DataFrame, column: str = "period") -> pd.DataFrame:
    """Добавить period_start (YYYY-MM), period_ym, period_full и period_type.

    ``period_start`` и ``period_ym`` — месячный бакет (для агрегации и графиков),
    ``period_full`` — период в зерне исходных данных: ``YYYY-MM-DD`` для дневной
    детализации и ``YYYY-MM`` для месячной. Сортировка идёт по ``period_full``,
    поэтому внутримесячный порядок не теряется.
    """
    if column not in df.columns:
        return df.copy()
    result = df.copy()
    # Avoid .map() which triggers segfault in pandas 2.2.3/numpy 2.1.3
    pairs = [parse_period(v) for v in result[column]]
    grain = period_grain(result[column])
    # Храним period_start как строку YYYY-MM, а не Timestamp,
    # чтобы избежать segfault в pandas 2.2.3 при sort_values/dropna на datetime64
    # Используем pd.NA для невозможных дат, чтобы isna() работала корректно
    result["period_start"] = [
        _period_to_ym(p) if pd.notna(p[0]) else pd.NA
        for p in pairs
    ]
    result["period_ym"] = result["period_start"]
    result["period_full"] = [
        canonical_period(value, grain) if pd.notna(pair[0]) else pd.NA
        for value, pair in zip(result[column], pairs)
    ]
    result["period_type"] = [
        ("D" if grain == "D" else pair[1]) if pd.notna(pair[0]) else ""
        for pair in pairs
    ]
    result = result.sort_values("period_full", kind="stable").reset_index(drop=True)
    return result


_PERIOD_FORMATS = {
    "%Y-%m": "{y:04d}-{m:02d}",
    "%Y-%m-%d": "{y:04d}-{m:02d}-{d:02d}",
    "%d.%m.%Y": "{d:02d}.{m:02d}.{y:04d}",
    "%Y": "{y:04d}",
    "%Y-%m-%d %H:%M": "{y:04d}-{m:02d}-{d:02d} {h:02d}:{mi:02d}",
}


def _format_period(ts: pd.Timestamp, fmt: str) -> str:
    """Отформатировать дату периода.

    Частые форматы считаются вручную: ``strftime`` в pandas 2.2.3 при numpy 2.x
    приводил к падению процесса, а не к исключению.
    """
    template = _PERIOD_FORMATS.get(fmt)
    if template is None:
        try:
            return ts.strftime(fmt)
        except (AttributeError, ValueError):
            return _period_to_ym((ts, "M"))
    try:
        return template.format(
            y=int(ts.year), m=int(ts.month), d=int(ts.day),
            h=int(ts.hour), mi=int(ts.minute),
        )
    except (AttributeError, ValueError):
        return _period_to_ym((ts, "M"))


def period_labels(values: pd.Series, fmt: str = "%Y-%m") -> pd.Series:
    """Подпись периода для отчёта.

    Формат задаётся ``fmt``: ``%Y-%m`` — месяц (по умолчанию), ``%Y-%m-%d`` —
    день. Колонка ``period_start`` может прийти датой (после ``normalize_periods``)
    или текстом (готовая длинная таблица), поэтому дата приводится через парсинг
    строк, а неразобранный текст остаётся как есть.
    """
    # Avoid pd.to_datetime which triggers segfault in pandas 2.2.3/numpy 2.1.3
    out = []
    for v in values:
        if pd.isna(v):
            out.append("")
            continue
        start, _ = parse_period(v)
        if pd.notna(start):
            out.append(_format_period(start, fmt))
            continue
        s = str(v).strip()
        if len(s) >= 7 and s[4] == "-":
            out.append(s[:7])
        elif len(s) >= 10 and s[2] == "." and s[5] == ".":
            # DD.MM.YYYY
            out.append(f"{s[6:10]}-{s[3:5]}")
        elif len(s) >= 10 and s[2] == "/" and s[5] == "/":
            # DD/MM/YYYY
            out.append(f"{s[6:10]}-{s[3:5]}")
        else:
            out.append(s)
    return pd.Series(out, index=values.index)


# Итоговые строки: по словарю данных это `product_code = 'ALL'`.
# В выгрузках встречаются и другие обозначения итогов — их тоже нельзя
# суммировать с детализацией.
TOTAL_CODES = frozenset({"ALL", "TOTAL", "ИТОГО", "ВСЕГО"})


def drop_total_rows(
    df: pd.DataFrame | None,
    column: str = "product_code",
    group: tuple[str, ...] = ("period",),
) -> tuple[pd.DataFrame | None, int]:
    """Убрать итоговые строки (``product_code = 'ALL'``), если рядом есть детализация.

    Словарь данных (§3, §7) требует не суммировать итог вместе с детализацией:
    иначе выручка и баланс удваиваются. Правило выполняется здесь, до группировки,
    чтобы одинаково работать и для ``finance``, и для ``portfolio``.

    Если в срезе (по умолчанию — внутри одного периода) детализации нет,
    итоговые строки остаются: иначе ряд из одного «ALL» стал бы пустым.
    Возвращает таблицу и число исключённых строк.
    """
    if df is None or df.empty or column not in df.columns:
        return df, 0

    is_total = df[column].astype(str).str.strip().str.upper().isin(TOTAL_CODES)
    if not is_total.any():
        return df, 0

    detailed = ~is_total
    keys = [key for key in group if key in df.columns]
    if keys:
        has_detail = detailed.groupby([df[key] for key in keys], dropna=False).transform("any")
        drop = is_total & has_detail
    else:
        drop = is_total & detailed.any()

    count = int(drop.sum())
    if not count:
        return df, 0
    return df.loc[~drop].copy(), count


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

TABLE_ALIASES = {
    "portfolio": "accounts",
    "sales": "applications",
    "funnel_events": "funnel",
}


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
            missing = int(dpd.isna().sum())
            if missing:
                report.add("portfolio_dpd_missing", False,
                           f"Пропусков dpd: {missing}; в расчёте считаются нулевой просрочкой.",
                           warn_only=True)
            report.add("portfolio_dpd_non_negative", bool((dpd.dropna() >= 0).all()),
                       f"Отрицательных dpd: {int((dpd < 0).sum())}")
        if clients is not None and "client_id" in portfolio.columns and "client_id" in clients.columns:
            coverage = portfolio["client_id"].isin(clients["client_id"]).mean()
            report.add("portfolio_fk_clients", coverage >= 0.99,
                       f"Покрытие client_id: {coverage:.1%}", warn_only=True)
        if products is not None and "product_code" in portfolio.columns and "product_code" in products.columns:
            coverage = portfolio["product_code"].isin(products["product_code"]).mean()
            report.add("portfolio_fk_products", coverage >= 0.99,
                       f"Покрытие product_code: {coverage:.1%}", warn_only=True)
        if {"dpd", "par_bucket"} <= set(portfolio.columns):
            dpd = _numeric(portfolio, "dpd")
            # Отрицательные dpd ловятся отдельной проверкой; в срезе они дают
            # ложное значение nan в pd.cut, поэтому в сравнении не участвуют.
            known = dpd.notna() & (dpd >= 0)
            bucket = portfolio.loc[known, "par_bucket"].astype(str).str.strip().str.lower()
            expected = pd.cut(dpd[known], [0, 30, 60, 90, float("inf")],
                              labels=["current", "30+", "60+", "90+"], right=False)
            mismatched = int((bucket != expected.astype(str).str.lower()).sum())
            report.add("portfolio_dpd_bucket_consistent", mismatched == 0,
                       f"Несогласованных строк dpd/par_bucket: {mismatched}", warn_only=True)
        if "currency" in portfolio.columns:
            currencies = sorted(portfolio["currency"].dropna().astype(str).unique())
            if len(currencies) > 1:
                report.warnings.append(
                    "В портфеле несколько валют (%s): суммы не сопоставимы без курса." % ", ".join(currencies)
                )
        if "product_code" in portfolio.columns:
            total_rows = int(portfolio["product_code"].astype(str).str.strip().str.upper()
                             .isin(TOTAL_CODES).sum())
            if total_rows:
                report.warnings.append(
                    f"В портфеле {total_rows} строк с product_code=ALL: это итог, "
                    "с детализацией он не суммируется."
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
            grain = period_grain(finance["period"])
            # Зерно берём из исходных данных: дневная детализация (2024-05-01,
            # 2024-05-02) — это разные строки, а не дубли одного месяца.
            grain_frame = finance[["period", "product_code"]].copy()
            grain_frame["period"] = [canonical_period(v, grain) for v in grain_frame["period"]]
            dup = int(grain_frame.duplicated().sum())
            report.add("finance_grain_unique", dup == 0,
                       f"Дублей period+product_code: {dup}")
        if "product_code" in finance.columns:
            all_rows = int(finance["product_code"].astype(str).str.strip().str.upper()
                           .isin(TOTAL_CODES).sum())
            if all_rows:
                report.warnings.append(
                    f"В finance {all_rows} строк с product_code=ALL: это итог, "
                    "с детализацией он не суммируется."
                )
        if "period" in finance.columns:
            normalized = normalize_periods(finance)
            bad = int(normalized["period_start"].isna().sum())
            report.add("finance_periods_parsed", bad == 0, f"Неразобранных периодов: {bad}")
        # Проверить, не записана ли в tax ставка вместо суммы (все значения в (0, 1] при выручке > 1000)
        if "tax" in finance.columns:
            tax_vals = pd.to_numeric(finance["tax"], errors="coerce").dropna()
            idx = finance.index
            revenue_vals = (
                pd.to_numeric(finance.get("interest_income", pd.Series(0.0, index=idx)), errors="coerce").fillna(0)
                + pd.to_numeric(finance.get("fee_income", pd.Series(0.0, index=idx)), errors="coerce").fillna(0)
                + pd.to_numeric(finance.get("premium", pd.Series(0.0, index=idx)), errors="coerce").fillna(0)
            )
            total_revenue = float(revenue_vals.sum())
            if len(tax_vals) > 0 and (tax_vals > 0).all() and (tax_vals <= 1).all() and total_revenue > 1000:
                report.warnings.append(
                    "В finance колонка tax выглядит как ставка (все значения в (0, 1]), "
                    "а не сумма налога в рублях. Проверьте данные."
                )

    survey = tables.get("macro_survey")
    if survey is not None:
        report.add("macro_survey_rows", len(survey) > 0, f"Строк: {len(survey)}")
        if "period_start" in survey.columns:
            # Count both NaN and empty strings as unparsed periods
            period_start = survey["period_start"]
            bad = int(period_start.isna().sum() + (period_start == "").sum())
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
