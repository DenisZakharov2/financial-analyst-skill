"""CLI: загрузка → проверки → метрики → графики → отчёты.

Коды возврата: 0 — успех, 2 — ошибка данных или контроля качества,
1 — непредвиденная ошибка. Ошибки не проглатываются.
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path

import numpy as np
import pandas as pd

from . import __version__
from .adapters import cbr_survey, client_csv
from .comparison import compare_snapshots
from .core import (
    KNOWN_TABLES,
    WORKBOOK_SUFFIXES,
    DataLoadError,
    assert_no_blocking_failures,
    load_tables,
    normalize_periods,
    period_labels,
    period_ym,
    validate_inputs,
)
from .drivers import macro_lead_lag, par_shift_decomposition, pvm_decomposition
from .experiments import run_ab_test
from .forecast import forecast_advanced
from .metrics import (
    MetricError,
    finance_summary,
    forecast_trend,
    funnel_summary,
    macro_anomalies,
    macro_correlation,
    macro_dynamics,
    macro_structure,
    portfolio_summary,
    unit_economics,
)
from .pii import sanitize_tables
from .reporting import ReportData, column_title, fmt_num, quality_frame, write_html, write_reports
from .seasonality_tat import funnel_tat, seasonality_summary
from .segmentation import segment_clients
from .visuals import (
    build_dashboard,
    chart_anomalies,
    chart_correlation,
    chart_forecast,
    chart_funnel,
    chart_portfolio,
    chart_series,
    setup_matplotlib,
)

# --------------------------------------------------------------------------
# Аргументы
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Финансовый анализ портфеля, воронки и макро-данных с генерацией отчётов.",
    )
    parser.add_argument("--data-dir", type=Path, required=True, help="Каталог с CSV/Excel")
    parser.add_argument("--out", type=Path, default=Path("./reports"), help="Каталог результатов")
    parser.add_argument("--format", default="docx,pdf,xlsx,pptx,rtf,html",
                        help="Форматы через запятую: docx,pdf,xlsx,pptx,rtf,html")
    parser.add_argument("--adapter", choices=["auto", "client", "cbr"], default="auto",
                        help="Как читать данные (по умолчанию auto)")
    parser.add_argument("--indicator", default=None, help="Показатель для макро-разреза")
    parser.add_argument("--lifetime-months", type=float, default=24.0,
                        help="Срок жизни клиента для LTV, месяцев")
    parser.add_argument("--forecast-h", type=int, default=4, help="Горизонт прогноза, периодов")
    parser.add_argument("--band", type=float, default=1.0,
                        help="Границы прогноза: ±band × MAE бэктеста")
    parser.add_argument("--forecast-model", choices=["legacy", "auto", "linear", "ets", "arx"],
                        default="legacy",
                        help="Модель прогноза: legacy — линейный тренд (по умолчанию), auto — выбор "
                             "модели на бэктесте, linear/ets/arx — фиксированная модель")
    parser.add_argument("--ab-test", action="store_true",
                        help="Оценить A/B-тест: SRM, тест Уэлча, CUPED, линеаризация ratio-метрик")
    parser.add_argument("--ab-metric", default="value",
                        help="Колонка метрики для A/B-теста (по умолчанию value)")
    parser.add_argument("--ab-metric-type", choices=["continuous", "proportion", "ratio"],
                        default="continuous", help="Тип метрики A/B-теста")
    parser.add_argument("--ab-denominator", default=None,
                        help="Знаменатель ratio-метрики A/B-теста (например, deals)")
    parser.add_argument("--ab-pre-metric", default=None,
                        help="Доэкспериментальная ковариата для CUPED")
    parser.add_argument("--ab-group-col", default="experiment_group",
                        help="Колонка групп пилота (control / treatment)")
    parser.add_argument("--ab-lower-is-better", action="store_true",
                        help="Метрика A/B-теста, где меньшее значение лучше "
                             "(PAR, срок, расходы). По умолчанию большее — лучше")
    parser.add_argument("--segment", action="store_true",
                        help="Сегментация клиентов: матрица RFM + Risk и кластеризация")
    parser.add_argument("--no-cluster", action="store_true",
                        help="Без кластеризации в сегментации (только RFM + Risk)")
    parser.add_argument("--drivers", action="store_true",
                        help="Факторный разбор: PVM и разложение сдвига PAR между срезами, "
                             "лаговые связи макропоказателей")
    parser.add_argument("--drivers-max-lag", type=int, default=3,
                        help="Максимальный лаг для теста Грейнджера (по умолчанию 3)")
    parser.add_argument("--strict", action="store_true",
                        help="Завершиться с кодом 2 при провале контроля качества")
    parser.add_argument("--pii-mode", choices=["strict", "redact", "off"], default="strict",
                        help="Режим обработки ПДн: strict=остановить при обнаружении, redact=удалить ПДн и псевдонимизировать ключи, off=отключить проверку")
    parser.add_argument("--compare-dirs", nargs=2, metavar=("BASE_DIR", "TARGET_DIR"),
                        help="Сравнить две выгрузки: базовая и целевая директории")
    parser.add_argument("--labels", nargs=2, metavar=("BASE_LABEL", "TARGET_LABEL"), default=["base", "target"],
                        help="Подписи для сравниваемых срезов (по умолчанию base target)")
    parser.add_argument("--title", default="Финансовый аналитический отчёт")
    parser.add_argument("--quiet", action="store_true")
    return parser


def log(args: argparse.Namespace, message: str) -> None:
    if not args.quiet:
        print(message)


def short(text: str, limit: int = 70) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def headline_row(label: str, value, unit: str, source: str, digits: int = 2) -> dict:
    """Ключевой показатель: форматированная строка для отчёта и число для XLSX/JSON."""
    raw = value
    if raw is None or (isinstance(raw, float) and np.isnan(raw)):
        raw = None
    return {"label": label, "value": fmt_num(value, digits), "raw": raw,
            "digits": digits, "unit": unit, "source": source}


# --------------------------------------------------------------------------
# Сериализация
# --------------------------------------------------------------------------

def records(frame: pd.DataFrame | None) -> list[dict]:
    if frame is None or frame.empty:
        return []
    clean = frame.copy()
    for column in clean.columns:
        # Avoid .map() which triggers segfault in pandas 2.2.3/numpy 2.1.3
        clean[column] = [
            None if (isinstance(v, float) and np.isnan(v)) else v
            for v in clean[column]
        ]
    clean = clean.astype(object).where(pd.notna(clean), None)
    for column in clean.columns:
        # Check if original column was datetime64 without using .dt accessor
        col_dtype = frame[column].dtype
        if hasattr(col_dtype, 'kind') and col_dtype.kind == 'M':  # datetime64
            # Convert to YYYY-MM-DD strings using list comprehension
            clean[column] = [
                v.strftime("%Y-%m-%d") if hasattr(v, 'strftime') else str(v)[:10]
                for v in frame[column]
            ]
    return clean.to_dict(orient="records")


def json_ready(value):
    """Привести значение к строгому JSON: NaN и NaT → ``null``, numpy → Python.

    ``json.dumps`` по умолчанию пишет ``NaN``, а это невалидный JSON и нарушает
    правило «в metrics.json только null с причиной».
    """
    if isinstance(value, dict):
        return {key: json_ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_ready(item) for item in value]
    if value is None or value is pd.NaT:
        return None
    if isinstance(value, (np.bool_,)):
        return bool(value)
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        number = float(value)
        return None if np.isnan(number) or np.isinf(number) else number
    # Handle pandas Timestamp without calling strftime (segfault risk)
    if hasattr(value, 'strftime') and hasattr(value, 'year') and hasattr(value, 'month') and hasattr(value, 'day'):
        try:
            return f"{int(value.year):04d}-{int(value.month):02d}-{int(value.day):02d}"
        except (AttributeError, ValueError):
            pass
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return value


def flatten_metrics(sections: list[tuple[str, pd.DataFrame, str]]) -> list[dict]:
    rows: list[dict] = []
    for name, frame, _ in sections:
        if frame is None or frame.empty:
            continue
        numeric = frame.select_dtypes(include=[np.number]).columns
        dimension = frame.columns[0] if len(frame.columns) else None
        for _, row in frame.iterrows():
            for column in numeric:
                value = row[column]
                if pd.isna(value):
                    continue
                rows.append({
                    "section": name,
                    "metric": column,
                    "dimension": str(row[dimension]) if dimension else "",
                    "value": float(value) if isinstance(value, (float, np.floating)) else value,
                })
    return rows


def headline_rows(headline: list[dict]) -> list[dict]:
    """Ключевые показатели в плоском виде.

    Без них шаг «сверьте headline с metrics.csv» невыполним: в отчёте число есть,
    а в CSV его нет. Значение берётся из ``raw`` (число), а не из ``value``
    (форматированная строка).
    """
    rows: list[dict] = []
    for item in headline:
        raw = item.get("raw")
        if raw is None or (isinstance(raw, float) and np.isnan(raw)):
            continue
        rows.append({
            "section": "Ключевые показатели",
            "metric": str(item.get("label", "")),
            "dimension": str(item.get("unit", "")),
            "value": float(raw) if isinstance(raw, (float, np.floating)) else raw,
        })
    return rows


def flat_values(section: str, values: dict) -> list[dict]:
    """Числовые поля словаря метрик (юнит-экономика) в плоском виде."""
    rows: list[dict] = []
    for key, value in values.items():
        if isinstance(value, bool) or not isinstance(value, (int, float, np.integer, np.floating)):
            continue
        number = float(value)
        if not np.isfinite(number):
            continue
        rows.append({"section": section, "metric": str(key), "dimension": "", "value": number})
    return rows


# --------------------------------------------------------------------------
# Макро-раздел
# --------------------------------------------------------------------------

def _series(survey: pd.DataFrame, indicator: str, industry: str) -> pd.DataFrame:
    subset = survey[(survey["indicator"] == indicator) & (survey["industry"] == industry)]
    return (subset.groupby("period_start", as_index=False)["value"].mean()
              .sort_values("period_start"))


def _pick_industry(survey: pd.DataFrame, indicator: str) -> str:
    subset = survey[survey["indicator"] == indicator]
    for preferred in ("Экономика всего", "Экономика", "Всего"):
        if preferred in set(subset["industry"]):
            return preferred
    counts = subset.groupby("industry")["value"].count().sort_values(ascending=False)
    return str(counts.index[0])


def _correlations(survey: pd.DataFrame, industry: str, max_indicators: int = 8,
                  top_n: int = 10) -> dict:
    """Попарные корреляции показателей одной отрасли и матрица для графика.

    Ограничение ``max_indicators`` держит перестановочный тест (2000 итераций
    на пару) в разумном времени: восемь показателей — это 28 пар.
    """
    subset = survey[survey["industry"] == industry]
    wide = (subset.pivot_table(index="period_start", columns="indicator", values="value",
                               aggfunc="first").sort_index())
    if wide.shape[1] < 2:
        return {"pairs": pd.DataFrame(), "matrix": pd.DataFrame(), "tested": 0}
    coverage = wide.notna().sum().sort_values(ascending=False)
    names = list(coverage.index[:max_indicators])
    pairs = []
    for position, left_name in enumerate(names):
        for right_name in names[position + 1:]:
            left = wide[[left_name]].rename(columns={left_name: "_left"}).reset_index()
            right = wide[[right_name]].rename(columns={right_name: "_right"}).reset_index()
            result = macro_correlation(left, right, "_left", "_right")
            if result["pearson"] is None:
                continue
            pairs.append({"indicator_1": str(left_name), "indicator_2": str(right_name),
                          "n": result["n"], "pearson": result["pearson"],
                          "spearman": result["spearman"], "p_value": result["p_value"]})
    if not pairs:
        return {"pairs": pd.DataFrame(), "matrix": pd.DataFrame(), "tested": 0}
    frame = pd.DataFrame(pairs)
    frame = (frame.reindex(frame["pearson"].abs().sort_values(ascending=False).index)
              .head(top_n).reset_index(drop=True))
    matrix = wide[names].corr(method="pearson").round(3)
    return {"pairs": frame, "matrix": matrix, "tested": len(pairs)}


# --------------------------------------------------------------------------
# Расширенные расчёты: A/B-тест, сегментация, факторный разбор
# --------------------------------------------------------------------------

def _ab_source(tables: dict[str, pd.DataFrame], group_col: str) -> tuple[str | None, pd.DataFrame | None]:
    """Найти таблицу с колонкой групп пилота.

    Порядок предпочтения: ``sales`` (есть сумма и факт сделки), ``clients``,
    ``portfolio``.
    """
    for name in ("sales", "clients", "portfolio"):
        frame = tables.get(name)
        if frame is not None and not frame.empty and group_col in frame.columns:
            return name, frame
    return None, None


def _ab_metric_column(frame: pd.DataFrame, requested: str) -> str | None:
    """Определить колонку метрики: запрошенная либо первая подходящая из типовых."""
    if requested in frame.columns:
        return requested
    for candidate in ("value", "amount", "balance", "revenue"):
        if candidate in frame.columns:
            return candidate
    return None


def _ab_report_frame(result: dict) -> pd.DataFrame:
    """Плоская таблица результатов A/B-теста для отчёта."""
    srm = result.get("srm_check", {})
    ci_low, ci_high = (result.get("ci_95") or (None, None))
    rows = [
        {"Показатель": "Метод", "Значение": result.get("test_used", ""), "Ед.": ""},
        {"Показатель": "Клиентов в контроле", "Значение": fmt_num(result.get("n_control"), 0), "Ед.": "чел."},
        {"Показатель": "Клиентов в тесте", "Значение": fmt_num(result.get("n_treatment"), 0), "Ед.": "чел."},
        {"Показатель": "Среднее в контроле", "Значение": fmt_num(result.get("control_mean"), 2), "Ед.": ""},
        {"Показатель": "Среднее в тесте", "Значение": fmt_num(result.get("treatment_mean"), 2), "Ед.": ""},
        {"Показатель": "Абсолютный эффект", "Значение": fmt_num(result.get("absolute_effect"), 2), "Ед.": ""},
        {"Показатель": "Относительный эффект", "Значение": fmt_num(result.get("relative_uplift_pct"), 2), "Ед.": "%"},
        {"Показатель": "Доверительный интервал 95 %",
         "Значение": f"{fmt_num(ci_low, 2)} … {fmt_num(ci_high, 2)}", "Ед.": ""},
        {"Показатель": "p-value", "Значение": fmt_num(result.get("p_value"), 4), "Ед.": ""},
        {"Показатель": "p-value Манна — Уитни",
         "Значение": fmt_num(result.get("mann_whitney_p_value"), 4), "Ед.": ""},
        {"Показатель": "MDE (минимальный значимый эффект)",
         "Значение": fmt_num(result.get("mde_absolute"), 2), "Ед.": ""},
        {"Показатель": "SRM p-value", "Значение": fmt_num(srm.get("p_value"), 6), "Ед.": ""},
        {"Показатель": "SRM пройден", "Значение": "да" if srm.get("srm_ok") else "нет", "Ед.": ""},
        {"Показатель": "Вывод", "Значение": result.get("recommendation_text", ""), "Ед.": ""},
    ]
    return pd.DataFrame(rows)


def _segment_report_frame(profiles: pd.DataFrame) -> pd.DataFrame:
    """Профили сегментов в виде таблицы отчёта."""
    view = profiles.copy()
    view.columns = [column_title(str(column)) for column in view.columns]
    return view


def _pvm_input(tables: dict[str, pd.DataFrame]) -> tuple[pd.DataFrame | None, str]:
    """Собрать вход для PVM: объём (число сделок) и выручка по продукту."""
    sales = tables.get("sales")
    if sales is not None and not sales.empty and "amount" in sales.columns:
        segment_col = "product_code" if "product_code" in sales.columns else None
        frame = sales.copy()
        frame["_volume"] = 1.0
        frame["_value"] = pd.to_numeric(frame["amount"], errors="coerce").fillna(0.0)
        if segment_col is None:
            frame["_segment"] = "все продукты"
            segment_col = "_segment"
        grouped = frame.groupby(segment_col, as_index=False).agg(
            volume=("_volume", "sum"), revenue=("_value", "sum"))
        return grouped, segment_col
    return None, ""


def _run_forecast(series: pd.DataFrame, args: argparse.Namespace) -> dict:
    """Прогноз по ряду: legacy (линейный тренд) или каскад моделей с бэктестом."""
    model = getattr(args, "forecast_model", "legacy")
    if model == "legacy":
        return forecast_trend(series, horizon=args.forecast_h, band=args.band)
    return forecast_advanced(series, horizon=args.forecast_h, band=args.band, method=model)


# --------------------------------------------------------------------------
# Основной сценарий
# --------------------------------------------------------------------------

def run(args: argparse.Namespace) -> int:
    data_dir: Path = args.data_dir
    out_dir: Path = args.out
    formats = [item.strip().lower() for item in args.format.split(",") if item.strip()]
    out_dir.mkdir(parents=True, exist_ok=True)
    charts_dir = out_dir / "charts"
    charts_dir.mkdir(exist_ok=True)
    for stale_chart in charts_dir.glob("*.png"):
        stale_chart.unlink()
    for stale in ("dashboard.html", "financial_analyst_report.html", "report.html"):
        (out_dir / stale).unlink(missing_ok=True)
    setup_matplotlib()

    # В режиме auto/cbr книги stat_*.xlsx читает cbr_survey.parse_workbook (все листы),
    # поэтому пропускаем их в load_tables, чтобы не читать первый лист дважды.
    skip_patterns = ("stat_",) if args.adapter in {"auto", "cbr"} else ()
    tables = load_tables(data_dir, skip_patterns=skip_patterns)
    notes: list[str] = []

    workbook_paths = sorted(path for path in data_dir.iterdir()
                            if path.suffix.lower() in WORKBOOK_SUFFIXES)
    # В auto берём только выгрузки с префиксом stat_: в одном каталоге могут лежать
    # и клиентские книги. С --adapter cbr разбираем любую книгу, которая читается.
    survey_paths = ([path for path in workbook_paths if path.name.lower().startswith("stat_")]
                    if args.adapter == "auto" else workbook_paths)
    if args.adapter in {"auto", "cbr"}:
        for path in survey_paths:
            survey = cbr_survey.parse_workbook(path)
            if survey.empty:
                continue
            survey = normalize_periods(survey)
            tables["macro_survey"] = survey
            count = f"{len(survey):,}".replace(",", " ")
            notes.append(f"Выгрузка {path.name}: {count} строк, "
                         f"{survey['industry'].nunique()} отраслей, "
                         f"{survey['indicator'].nunique()} показателей.")
            break
        else:
            if args.adapter == "auto" and survey_paths:
                notes.append("Книга stat_*.xlsx найдена, но выгрузка опросов не распознана: "
                             "проверьте строку заголовков и листы.")
            elif args.adapter == "auto" and any(p.name.lower().startswith("stat_") for p in workbook_paths):
                # Предупреждаем только если есть файлы stat_*, но они не распознались
                notes.append("Книга с опросами не найдена: адаптер ищет файл stat_*.xlsx. "
                             "Для файла с другим именем запустите с --adapter cbr.")
    if args.adapter == "client":
        tables.pop("macro_survey", None)

    tables, alias_notes = client_csv.normalize(tables)
    notes.extend(alias_notes)

    # --- PII sanitization (before quality gates) ---
    pii_mode = getattr(args, "pii_mode", "strict")
    if pii_mode != "off":
        tables, pii_report = sanitize_tables(tables, mode=pii_mode)
        if pii_report.get("pii_found"):
            if pii_mode == "strict":
                # In strict mode, sanitize_tables raises ValueError, so we only get here in redact mode
                pass
            notes.append(
                f"PII: режим={pii_mode}, удалены колонки={pii_report['findings']}, "
                f"псевдонимизированы ключи={pii_report['pseudonymized_columns']}"
            )
            if pii_report.get("key_ephemeral"):
                notes.append(
                    "PII: ключ HMAC сгенерирован на один запуск. Задайте FINANALYST_PII_KEY, "
                    "чтобы псевдонимы совпадали между запусками."
                )
        else:
            notes.append("PII: прямых ПДн не обнаружено")

    survey_table = tables.get("macro_survey")
    if survey_table is not None and "period_start" not in survey_table.columns:
        if "period" in survey_table.columns:
            tables["macro_survey"] = normalize_periods(survey_table)
        else:
            notes.append("macro_survey: нет колонки period — раздел пропущен.")
            tables.pop("macro_survey")

    # Удаляем служебные таблицы (отчёты, загруженные по ошибке)
    for key in list(tables):
        if key.lower().startswith("отчет"):
            tables.pop(key)
            continue
        if key not in KNOWN_TABLES:
            notes.append(f"Пропущена таблица {key!r}: её нет в словаре данных.")
            tables.pop(key)

    # --- A/B-тест (--ab-test): считается до контроля качества, потому что
    # нарушение SRM делает результат теста недействительным, а не предупреждением.
    ab_result: dict | None = None
    ab_source_name: str | None = None
    if getattr(args, "ab_test", False):
        group_col = getattr(args, "ab_group_col", "experiment_group")
        ab_source_name, ab_frame = _ab_source(tables, group_col)
        if ab_frame is None:
            notes.append(
                f"A/B-тест пропущен: в таблицах нет колонки групп «{group_col}»."
            )
        else:
            metric_col = _ab_metric_column(ab_frame, getattr(args, "ab_metric", "value"))
            if metric_col is None:
                notes.append(
                    "A/B-тест пропущен: в таблице "
                    f"«{ab_source_name}» нет колонки метрики "
                    f"(искали «{getattr(args, 'ab_metric', 'value')}», amount, balance, revenue)."
                )
            else:
                ab_result = run_ab_test(
                    ab_frame,
                    group_col=group_col,
                    metric_col=metric_col,
                    denominator_col=getattr(args, "ab_denominator", None),
                    pre_metric_col=getattr(args, "ab_pre_metric", None),
                    metric_type=getattr(args, "ab_metric_type", "continuous"),
                    higher_is_better=not getattr(args, "ab_lower_is_better", False),
                )

    quality = validate_inputs(tables)
    if ab_result is not None and ab_result.get("available"):
        srm = ab_result.get("srm_check", {})
        srm_ok = bool(srm.get("srm_ok"))
        overlap = int(ab_result.get("assignment_overlap_clients", 0))
        quality.add(
            "ab_srm_check",
            srm_ok,
            (f"SRM пройден: p={srm.get('p_value')}" if srm_ok
             else f"Нарушен SRM: p={srm.get('p_value')} < 0.001 — результат теста недействителен"),
        )
        quality.add(
            "ab_assignment_overlap",
            overlap == 0,
            (f"Клиентов в обеих группах: {overlap} — такие единицы исключены, "
             "результат теста помечен недействительным"
             if overlap else "Каждый клиент принадлежит одной группе"),
        )
    log(args, f"Контроль качества: {len(quality.checks)} проверок, "
              f"{len(quality.failed)} fail, {len(quality.warned)} warn")
    for check in quality.failed + quality.warned:
        log(args, f"  [{check.status}] {check.check_id}: {check.detail}")

    if args.strict:
        assert_no_blocking_failures(quality)

    sections: list[tuple[str, pd.DataFrame, str]] = []
    assumptions: list[str] = []
    warnings: list[str] = list(quality.warnings)
    headline: list[dict] = []
    unit: dict = {}
    charts: dict[str, Path] = {}
    metrics_payload: dict = {
        "meta": {
            "version": __version__,
            "data_dir": data_dir.name,
            "adapter": args.adapter,
            "generated_at": __import__('datetime').datetime.now().isoformat(timespec="seconds"),
        },
        "quality": quality.to_dict(),
        "assumptions": assumptions,
        "warnings": warnings,
        "sections": {},
    }

    portfolio = tables.get("portfolio")
    clients = tables.get("clients")
    products = tables.get("products")
    survey = tables.get("macro_survey")

    # --- Портфель ---------------------------------------------------------
    if portfolio is not None and "balance" in portfolio.columns:
        result = portfolio_summary(portfolio, products, clients)
        frame = result["rows"]
        sections.append(("Портфель по типам продуктов", frame, ""))
        metrics_payload["sections"]["portfolio"] = {
            "totals": result["totals"], "rows": records(frame),
        }
        total_balance = result["totals"]["total_balance"]
        headline.append(headline_row("Остаток по портфелю", total_balance / 1e6, "млн руб.",
                                     "portfolio.balance", digits=1))
        headline.append(headline_row("Счетов в портфеле", result["totals"]["accounts"], "шт.",
                                     "portfolio.account_id", digits=0))
        if result["totals"].get("par30_pct") is not None:
            headline.append(headline_row("PAR 30", result["totals"]["par30_pct"], "%",
                                         "portfolio.dpd"))
        # Workaround for matplotlib tight_layout recursion bug in Python 3.14
        try:
            chart = chart_portfolio(frame, charts_dir / "portfolio.png")
            if chart:
                charts["Портфель по типам продуктов"] = chart
        except RecursionError:
            pass
        except Exception as e:
            warnings.append(f"Ошибка построения графика портфеля: {e}")

    # --- Воронка ----------------------------------------------------------
    funnel_source = tables.get("funnel_events")
    funnel_column = "stage"
    funnel_table_name = "funnel_events"
    if funnel_source is None and tables.get("sales") is not None:
        funnel_source = tables["sales"]
        funnel_column = "funnel_stage"
        funnel_table_name = "sales"
    if funnel_source is not None and funnel_column in funnel_source.columns:
        funnel = funnel_summary(funnel_source, funnel_column)
        sections.append(("Воронка продаж", funnel["rows"], funnel["assumption"]))
        assumptions.append(funnel["assumption"])
        metrics_payload["sections"]["funnel"] = {
            "rows": records(funnel["rows"]),
            "entities": funnel["entities"],
            "assumption": funnel["assumption"],
        }
        last = funnel["rows"].iloc[-1]
        headline.append(headline_row("Сквозная конверсия", last["from_start_pct"], "%",
                                     funnel_table_name))
        headline.append(headline_row("Участников воронки", funnel["entities"], funnel["entity"],
                                     funnel_table_name, digits=0))
        # Workaround for matplotlib tight_layout recursion bug in Python 3.14
        try:
            chart = chart_funnel(funnel["rows"], charts_dir / "funnel.png")
            if chart:
                charts["Воронка продаж"] = chart
        except RecursionError:
            pass
        except Exception as e:
            warnings.append(f"Ошибка построения графика воронки: {e}")

        # --- Поэтапный TAT и SLA ---
        sales_for_tat = tables.get("sales") if funnel_table_name == "funnel_events" else None
        tat_result = funnel_tat(funnel_source, sales=sales_for_tat)
        if tat_result.get("available"):
            if not tat_result["by_transition"].empty:
                sections.append(("Воронка: поэтапный TAT (дни)", tat_result["by_transition"],
                                 tat_result["assumption"]))
                metrics_payload["sections"]["funnel_tat"] = {
                    "by_transition": records(tat_result["by_transition"]),
                    "skipped_transitions": records(tat_result["skipped_transitions"]),
                    "skipped_count": tat_result.get("skipped_count", 0),
                    "backward_count": tat_result.get("backward_count", 0),
                    "by_segment": records(tat_result["by_segment"]),
                    "aging_backlog": records(tat_result["aging_backlog"]),
                    "survival_curve": records(tat_result["survival_curve"]),
                    "reference_date": tat_result["reference_date"],
                    "assumption": tat_result["assumption"],
                }
                # Headline: медианный TAT по последнему переходу
                last_trans = tat_result["by_transition"].iloc[-1]
                headline.append(headline_row(
                    f"TAT {last_trans['transition']} (медиана)", last_trans["median_p50_days"], "дней",
                    "funnel_events"
                ))
            if not tat_result.get("skipped_transitions", pd.DataFrame()).empty:
                sections.append((
                    "Воронка: переходы с пропущенным этапом",
                    tat_result["skipped_transitions"],
                    "Эти переходы охватывают несколько этапов сразу, поэтому в SLA и TAT "
                    "по этапам не входят.",
                ))
            if not tat_result["aging_backlog"].empty:
                sections.append(("Воронка: зависший бэклог", tat_result["aging_backlog"], ""))
            if not tat_result["survival_curve"].empty:
                sections.append(("Воронка: удержание Каплана-Мейера", tat_result["survival_curve"], ""))

        # --- Сезонность воронки (если есть даты и количества) ---
        date_col = next((c for c in ("event_date", "stage_date", "application_date")
                         if c in funnel_source.columns), None)
        if date_col is not None:
            funnel_ts = funnel_source[[date_col]].copy()
            # Приводим даты событий к началу месяца (YYYY-MM-01) и считаем поток событий
            funnel_periods = []
            for value in funnel_ts[date_col]:
                ym = period_ym(value) if pd.notna(value) else ""
                funnel_periods.append(f"{ym}-01" if ym else "")
            funnel_ts["period"] = funnel_periods
            funnel_ts["events_count"] = 1.0
            season_result = seasonality_summary(funnel_ts, value_col="events_count", period_col="period")
            if season_result.get("available"):
                sections.append(("Воронка: сезонные индексы", season_result["seasonal_indices"],
                                 season_result["assumption"]))
                metrics_payload["sections"]["funnel_seasonality"] = {
                    "seasonal_indices": records(season_result["seasonal_indices"]),
                    "components": records(season_result["components"]),
                    "strength_seasonal": season_result["strength_seasonal"],
                    "strength_trend": season_result["strength_trend"],
                    "mode_used": season_result["mode_used"],
                    "assumption": season_result["assumption"],
                }

    # --- A/B-тест ---------------------------------------------------------
    if ab_result is not None:
        if ab_result.get("available"):
            sections.append(("Эксперимент: A/B-тест", _ab_report_frame(ab_result),
                             ab_result["assumption"]))
            assumptions.append(ab_result["assumption"])
            metrics_payload["sections"]["ab_test"] = ab_result
            headline.append(headline_row(
                "A/B: абсолютный эффект", ab_result.get("absolute_effect"), "",
                f"{ab_source_name}.{group_col}"))
            headline.append(headline_row(
                "A/B: относительный эффект", ab_result.get("relative_uplift_pct"), "%",
                f"{ab_source_name}.{group_col}"))
        else:
            warnings.append(f"A/B-тест не рассчитан: {ab_result.get('assumption', '')}")

    # --- Сегментация клиентов (--segment) ---------------------------------
    if getattr(args, "segment", False):
        clients_table = tables.get("clients")
        portfolio_table = tables.get("portfolio")
        if clients_table is None or portfolio_table is None:
            warnings.append("Сегментация пропущена: нужны таблицы clients и portfolio.")
        else:
            segmentation_result = segment_clients(
                clients_table,
                portfolio_table,
                sales=tables.get("sales"),
                enable_clustering=not getattr(args, "no_cluster", False),
            )
            if segmentation_result.get("available"):
                sections.append(("Клиенты: сегменты RFM + Risk",
                                 _segment_report_frame(segmentation_result["profiles"]),
                                 segmentation_result["assumption"]))
                assumptions.append(segmentation_result["assumption"])
                metrics_payload["sections"]["segmentation"] = {
                    "profiles": records(segmentation_result["profiles"]),
                    "client_assignments": records(segmentation_result["client_assignments"]),
                    "optimal_k": segmentation_result["optimal_k"],
                    "silhouette_score": segmentation_result["silhouette_score"],
                    "assumption": segmentation_result["assumption"],
                }
                top = segmentation_result["profiles"].iloc[0]
                headline.append(headline_row(
                    f"Крупнейший сегмент: {top['rfm_segment']}",
                    top["portfolio_share_pct"], "%", "clients, portfolio"))
            else:
                warnings.append(f"Сегментация не рассчитана: {segmentation_result.get('assumption', '')}")

    # --- Финансы и юнит-экономика ----------------------------------------
    finance = tables.get("finance")
    if finance is not None and not finance.empty:
        fin = finance_summary(finance, portfolio, products)
        sections.append(("Финансовые показатели", fin["rows"], "Выручка = проценты + комиссии + премии; "
                                                        "прямые расходы = фондирование + резервы."))
        totals = fin["totals"]
        metrics_payload["sections"]["finance"] = {"totals": totals, "rows": records(fin["rows"])}
        headline.append(headline_row("Выручка", totals["revenue"] / 1e6, "млн руб.", "finance",
                                     digits=1))
        if totals.get("margin_pct") is not None:
            headline.append(headline_row("Валовая маржа", totals["margin_pct"], "%", "finance"))
        unit = unit_economics(finance, clients, portfolio, tables.get("sales"),
                              lifetime_months=args.lifetime_months)
        assumptions.extend(unit["assumptions"])
        unit_frame = pd.DataFrame([
            {"Показатель": "Выручка", "Значение": fmt_num(unit["revenue"] / 1e6, 2), "Ед.": "млн руб."},
            {"Показатель": "Активные клиенты", "Значение": fmt_num(unit["active_clients"], 0), "Ед.": "чел."},
            {"Показатель": "Новые клиенты за период", "Значение": fmt_num(unit["new_clients"], 0),
             "Ед.": "чел."},
            {"Показатель": "ARPU (в месяц)", "Значение": fmt_num(unit["arpu"], 2), "Ед.": "руб./клиент"},
            {"Показатель": "CAC", "Значение": fmt_num(unit["cac"], 2), "Ед.": "руб./клиент"},
            {"Показатель": "LTV (gross)", "Значение": fmt_num(unit["ltv_gross"], 2), "Ед.": "руб."},
            {"Показатель": "LTV / CAC", "Значение": fmt_num(unit["ltv_cac"], 2), "Ед.": "x"},
            {"Показатель": "Окупаемость CAC", "Значение": fmt_num(unit["payback_months"], 1), "Ед.": "мес."},
        ])
        sections.append(("Юнит-экономика", unit_frame,
                         f"Срок жизни клиента: {unit['lifetime_months']:.0f} мес."))
        metrics_payload["sections"]["unit_economics"] = unit
        if unit["ltv_cac"] is not None:
            client_src = unit.get("client_source", "нет данных")
            ltv_source = f"finance, {client_src.split('.')[0]}" if "." in client_src else "finance, portfolio"
            headline.append(headline_row("LTV / CAC", unit["ltv_cac"], "x", ltv_source))

        # --- Сезонность финансов ---
        if "period" in fin["rows"].columns:
            fin_periods = fin["rows"][["period", "revenue"]].copy()
            fin_periods = fin_periods.dropna(subset=["period"])
            if not fin_periods.empty:
                season_result = seasonality_summary(fin_periods, value_col="revenue", period_col="period")
                if season_result.get("available"):
                    sections.append(("Финансы: сезонные индексы", season_result["seasonal_indices"],
                                     season_result["assumption"]))
                    metrics_payload["sections"]["finance_seasonality"] = {
                        "seasonal_indices": records(season_result["seasonal_indices"]),
                        "components": records(season_result["components"]),
                        "strength_seasonal": season_result["strength_seasonal"],
                        "strength_trend": season_result["strength_trend"],
                        "mode_used": season_result["mode_used"],
                        "assumption": season_result["assumption"],
                    }

        # --- Прогноз выручки: только при явно выбранной модели (legacy-режим
        # оставляет отчёт без изменений) ---
        if getattr(args, "forecast_model", "legacy") != "legacy" and "period" in fin["rows"].columns:
            revenue_series = (fin["rows"].groupby("period", as_index=False)["revenue"].sum()
                              .rename(columns={"period": "period_start", "revenue": "value"})
                              .sort_values("period_start"))
            revenue_forecast = _run_forecast(revenue_series, args)
            if revenue_forecast.get("available"):
                frame = pd.DataFrame({
                    "Период": [period[:7] if period.endswith("-01") else period
                               for period in revenue_forecast["periods"]],
                    **{name: values for name, values in revenue_forecast["scenarios"].items()},
                })
                sections.append(("Финансы: прогноз выручки", frame,
                                 f"Модель: {revenue_forecast.get('method_used')}; "
                                 f"MAE: {revenue_forecast['mae']}; "
                                 f"sMAPE: {revenue_forecast.get('smape_pct')} %"))
                metrics_payload["sections"]["finance_forecast"] = revenue_forecast
                headline.append(headline_row(
                    "Прогноз выручки (базовый сценарий)",
                    revenue_forecast["scenarios"]["базовый"][0] / 1e6, "млн руб.", "finance", digits=1))

    # --- Макро ------------------------------------------------------------
    indicator = None
    if survey is not None and not survey.empty:
        missing = sorted({"period_start", "industry", "indicator", "value"} - set(survey.columns))
        if missing:
            warnings.append(f"macro_survey: нет колонок {', '.join(missing)} — рыночный раздел пропущен.")
            survey = None
    if survey is not None and not survey.empty:
        indicator = args.indicator or cbr_survey.choose_indicator(survey)
        available = sorted(str(value) for value in survey["indicator"].dropna().unique())
        if indicator not in available:
            raise MetricError(
                "Показатель %r не найден в macro_survey. Доступны: %s"
                % (indicator, ", ".join(available[:10]) or "нет")
            )
        industry = _pick_industry(survey, indicator)
        series = _series(survey, indicator, industry)
        # period_start может быть датой или текстом: .dt на тексте падает.
        # Применяем macro_dynamics для расчёта change_pct и yoy_pct
        series = macro_dynamics(series, value_col="value")
        dynamics_view = series.tail(36).assign(
            period_start=lambda d: period_labels(d["period_start"]))
        cols = [c for c in ("period_start", "value", "change_pct", "yoy_pct") if c in dynamics_view.columns]
        sections.append(("Макро: динамика показателя", dynamics_view[cols],
                         f"Показатель: {indicator}. Отрасль: {industry}."))
        anomalies = macro_anomalies(series, value_col="value", group_cols=())
        if not anomalies.empty:
            view = anomalies.assign(period_start=lambda d: period_labels(d["period_start"]))
            sections.append(("Макро: аномалии", view[["period_start", "value", "_z"]].rename(
                columns={"period_start": "Период", "value": "Значение", "_z": "Z-score"}), ""))
        metrics_payload["sections"]["macro"] = {
            "indicator": indicator,
            "industry": industry,
            "points": int(len(series)),
            "anomalies": records(anomalies),
        }
        headline.append(headline_row(f"{short(indicator)} (последнее)", series["value"].iloc[-1],
                                     "п.", f"macro_survey, {industry}"))
        # Workaround for matplotlib tight_layout recursion bug in Python 3.14
        try:
            chart = chart_series(series, "value", "period_start", charts_dir / "macro.png",
                             label=industry)
            if chart:
                charts["Динамика по отрасли"] = chart
        except RecursionError:
            pass
        except Exception as e:
            warnings.append(f"Ошибка построения графика макро-динамики: {e}")
        if not anomalies.empty:
            try:
                chart = chart_anomalies(anomalies, charts_dir / "anomalies.png", label=industry)
                if chart:
                    charts["Аномалии по отрасли"] = chart
            except RecursionError:
                pass
            except Exception as e:
                warnings.append(f"Ошибка построения графика аномалий: {e}")

        forecast = _run_forecast(series, args)
        metrics_payload["sections"]["forecast"] = forecast
        if forecast.get("available"):
            assumptions.append(forecast["assumption"])
            frame = pd.DataFrame({
                "Период": [period[:7] if period.endswith("-01") else period
                           for period in forecast["periods"]],
                **{name: values for name, values in forecast["scenarios"].items()},
            })
            model_note = ""
            if forecast.get("method_used"):
                model_note = (f"Модель: {forecast['method_used']}; "
                              f"MAE: {forecast['mae']}; "
                              f"sMAPE: {forecast.get('smape_pct')} %; "
                              f"MASE: {forecast.get('mase')}")
            else:
                model_note = (f"MAE: {forecast['mae']}; "
                              f"MAPE: {forecast['mape_pct'] if forecast['mape_pct'] is not None else '— (все факты в бэктесте = 0)'} %")
            sections.append(("Прогноз: значения", frame, model_note))
            # Workaround for matplotlib tight_layout recursion bug in Python 3.14
            try:
                chart = chart_forecast(forecast, charts_dir / "forecast.png")
                if chart:
                    charts["Прогноз по сценариям"] = chart
            except RecursionError:
                pass
            except Exception as e:
                warnings.append(f"Ошибка построения графика прогноза: {e}")

        # Корреляции показателей внутри отрасли: перестановочный p-value не даёт
        # принять случайную связь за вывод, число проверенных пар фиксируется.
        correlation = _correlations(survey, industry)
        if not correlation["pairs"].empty:
            note = "Корреляция не означает причинность: это связь, а не механизм."
            sections.append(("Макро: корреляции показателей", correlation["pairs"], note))
            assumptions.append(
                f"{note} Проверено пар: {correlation['tested']}; "
                f"показаны {len(correlation['pairs'])} с наибольшим |r|; "
                "в матрицу вошло не более 8 показателей по покрытию."
            )
            metrics_payload["sections"]["correlations"] = {
                "industry": industry,
                "tested": int(correlation["tested"]),
                "pairs": records(correlation["pairs"]),
            }
            # Workaround for matplotlib tight_layout recursion bug in Python 3.14
            try:
                chart = chart_correlation(correlation["matrix"], charts_dir / "correlation.png")
                if chart:
                    charts["Корреляционная матрица"] = chart
            except RecursionError:
                pass
            except Exception as e:
                warnings.append(f"Ошибка построения графика корреляций: {e}")

        if "html" in formats:
            build_dashboard(survey, indicator, out_dir / "dashboard.html",
                            title=f"Дашборд: {indicator}")

        # --- Лаговые связи макропоказателей (--drivers) ---
        if getattr(args, "drivers", False):
            industry_frame = survey[survey["industry"] == industry]
            lead_lag = macro_lead_lag(
                industry_frame,
                period_col="period" if "period" in industry_frame.columns else "period_start",
                max_lag=getattr(args, "drivers_max_lag", 3),
            )
            if lead_lag.get("available"):
                sections.append(("Макро: опережающие связи (Грейнджер)", lead_lag["links"],
                                 lead_lag["assumption"]))
                assumptions.append(lead_lag["assumption"])
                metrics_payload["sections"]["macro_lead_lag"] = {
                    "links": records(lead_lag["links"]),
                    "max_lag_used": lead_lag["max_lag_used"],
                    "periods_analyzed": lead_lag["periods_analyzed"],
                    "significant_count": lead_lag["significant_count"],
                    "assumption": lead_lag["assumption"],
                }
                headline.append(headline_row(
                    "Значимых лаговых связей (FDR < 0.05)", lead_lag["significant_count"], "шт.",
                    "macro_survey"))
            else:
                warnings.append(f"Лаговая причинность не рассчитана: {lead_lag.get('assumption', '')}")

    # --- Рыночные таблицы (macro_*) --------------------------------------
    macro_specs = {
        "macro_deposits": ("value_mln_rub", "category", "Вклады и депозиты"),
        "macro_loans": ("value_mln_rub", "category", "Кредитный портфель"),
        "macro_profit": ("profit_total_mln_rub", None, "Прибыль организаций"),
        "macro_securities": ("value_mln_rub", "instrument_type", "Ценные бумаги"),
    }
    for key, (value_col, group_col, title) in macro_specs.items():
        table = tables.get(key)
        if table is None or value_col not in table.columns:
            continue
        table = normalize_periods(table).dropna(subset=["period_start"])
        if table.empty:
            continue
        if group_col and group_col in table.columns:
            structure = macro_structure(table, value_col, group_col)
            sections.append((f"{title}: структура", structure, f"Источник: {key}"))
        totals = table.groupby("period_start", as_index=False)[value_col].sum()
        dynamics = macro_dynamics(totals, value_col=value_col)
        view = dynamics.tail(36).assign(
            period_start=lambda d: period_labels(d["period_start"]))
        cols = [c for c in ("period_start", value_col, "change_pct", "yoy_pct") if c in view.columns]
        sections.append((f"{title}: динамика", view[cols], ""))
        anomalies = macro_anomalies(table, value_col=value_col,
                                    group_cols=(group_col,) if group_col else ())
        if not anomalies.empty:
            view_a = anomalies.assign(
                period_start=lambda d: period_labels(d["period_start"]))
            columns = [c for c in ("period_start", group_col, value_col, "_z") if c and c in view_a.columns]
            sections.append((f"{title}: аномалии", view_a[columns], ""))
        # Workaround for matplotlib tight_layout recursion bug in Python 3.14
        try:
            chart = chart_series(totals, value_col, "period_start",
                                 charts_dir / f"{key}.png", title=f"{title}: динамика")
            if chart:
                charts[f"{title}: динамика"] = chart
        except RecursionError:
            pass
        except Exception as e:
            warnings.append(f"Ошибка построения графика {title}: {e}")
        headline.append(headline_row(f"{title} (последнее)", totals[value_col].iloc[-1],
                                     "млн руб.", key))
        metrics_payload["sections"][key] = {
            "rows": records(view), "anomalies": records(anomalies),
        }

    # --- Сравнение двух выгрузок (--compare-dirs) ---
    if getattr(args, "compare_dirs", None):
        import secrets
        base_dir = Path(args.compare_dirs[0])
        target_dir = Path(args.compare_dirs[1])
        label_base, label_target = args.labels
        tables_base = load_tables(base_dir)
        tables_target = load_tables(target_dir)
        tables_base, _ = client_csv.normalize(tables_base)
        tables_target, _ = client_csv.normalize(tables_target)
        if pii_mode != "off":
            shared_secret = secrets.token_hex(32)
            tables_base, _ = sanitize_tables(tables_base, mode=pii_mode, secret_key=shared_secret)
            tables_target, _ = sanitize_tables(tables_target, mode=pii_mode, secret_key=shared_secret)
        comparison = compare_snapshots(tables_base, tables_target, label_base, label_target)
        if comparison.get("available"):
            sections.append((f"Сравнение срезов: {label_base} → {label_target}: схема",
                             comparison["schema"]["table_summary"],
                             comparison["assumption"]))
            bridge = comparison["portfolio_bridge"]
            if bridge.get("available"):
                def _pct(value: object) -> str:
                    return "—" if value is None else f"{float(value):.2f}%"

                sections.append(("Сравнение: портфельный Bridge", bridge["bridge_table"],
                                 f"Δ портфель: {bridge['balance_delta']:,.2f} ({_pct(bridge['balance_growth_pct'])}), "
                                 f"Retention: {_pct(bridge['client_retention_pct'])}, "
                                 f"NRR: {_pct(bridge['net_dollar_retention_pct'])}"))
                headline.append(headline_row(
                    f"Δ Портфель ({label_base}→{label_target})", bridge["balance_delta"], "руб.",
                    "portfolio", digits=0
                ))
                headline.append(headline_row(
                    "Client Retention", bridge["client_retention_pct"], "%",
                    "portfolio"
                ))
                headline.append(headline_row(
                    "Net Dollar Retention", bridge["net_dollar_retention_pct"], "%",
                    "portfolio"
                ))
            if not comparison["finance_comparison"].empty:
                sections.append(("Сравнение: P&L", comparison["finance_comparison"], ""))

            # --- Факторный разбор отклонений (--drivers) ---
            if getattr(args, "drivers", False):
                pvm_base, pvm_segment = _pvm_input(tables_base)
                pvm_target, _ = _pvm_input(tables_target)
                if pvm_base is not None and pvm_target is not None:
                    pvm = pvm_decomposition(
                        pvm_base, pvm_target,
                        segment_col=pvm_segment, volume_col="volume", value_col="revenue",
                    )
                    if pvm.get("available"):
                        effects = pvm["effects"]
                        frame = pd.DataFrame([
                            {"Эффект": "Объём", "Вклад": effects["volume_effect"]},
                            {"Эффект": "Продуктовый микс", "Вклад": effects["mix_effect"]},
                            {"Эффект": "Ставка (цена)", "Вклад": effects["rate_effect"]},
                            {"Эффект": "Взаимодействие", "Вклад": effects["interaction_effect"]},
                            {"Эффект": "Итого изменение", "Вклад": pvm["total_delta"]},
                        ])
                        sections.append((f"Факторный разбор PVM ({label_base} → {label_target})",
                                         frame, pvm["assumption"]))
                        metrics_payload["sections"]["pvm"] = {
                            "effects": pvm["effects"],
                            "total_delta": pvm["total_delta"],
                            "by_segment": records(pvm["by_segment"]),
                            "assumption": pvm["assumption"],
                        }
                        headline.append(headline_row("PVM: вклад объёма", effects["volume_effect"],
                                                    "руб.", "sales"))
                        headline.append(headline_row("PVM: вклад ставки", effects["rate_effect"],
                                                    "руб.", "sales"))
                else:
                    warnings.append("PVM-разбор пропущен: в выгрузках нет таблицы sales с суммой сделки.")

                port_base = tables_base.get("portfolio")
                if port_base is None:
                    port_base = tables_base.get("accounts")
                port_target = tables_target.get("portfolio")
                if port_target is None:
                    port_target = tables_target.get("accounts")
                if port_base is not None and port_target is not None:
                    par_shift = par_shift_decomposition(port_base, port_target)
                    if par_shift.get("available"):
                        frame = pd.DataFrame([
                            {"Эффект": "Риск внутри продуктов", "Вклад, п.п.": par_shift["within_risk_effect_pp"]},
                            {"Эффект": "Сдвиг структуры портфеля", "Вклад, п.п.": par_shift["structural_mix_effect_pp"]},
                            {"Эффект": "Итого изменение PAR", "Вклад, п.п.": par_shift["delta_par_pp"]},
                        ])
                        sections.append((f"Разложение сдвига PAR ({label_base} → {label_target})",
                                         frame, par_shift["assumption"]))
                        metrics_payload["sections"]["par_shift"] = {
                            "par_base_pct": par_shift["par_base_pct"],
                            "par_target_pct": par_shift["par_target_pct"],
                            "delta_par_pp": par_shift["delta_par_pp"],
                            "within_risk_effect_pp": par_shift["within_risk_effect_pp"],
                            "structural_mix_effect_pp": par_shift["structural_mix_effect_pp"],
                            "by_segment": records(par_shift["by_segment"]),
                            "assumption": par_shift["assumption"],
                        }
                        headline.append(headline_row("PAR: вклад риска внутри продуктов",
                                                    par_shift["within_risk_effect_pp"], "п.п.",
                                                    "portfolio.dpd"))

            metrics_payload["sections"]["comparison"] = {
                "labels": comparison["labels"],
                "schema": {k: records(v) if hasattr(v, "to_dict") else v
                           for k, v in comparison["schema"].items()},
                "portfolio_bridge": {k: records(v) if hasattr(v, "to_dict") else v
                                      for k, v in bridge.items()},
                "finance_comparison": records(comparison["finance_comparison"]),
                "assumption": comparison["assumption"],
            }

    # --- Плоские метрики --------------------------------------------------
    # metrics.csv — плоский список чисел для сверки: разделы, ключевые
    # показатели (raw, а не форматированная строка) и юнит-экономика.
    metric_rows = flatten_metrics(sections)
    metric_rows += headline_rows(headline)
    metric_rows += flat_values("Юнит-экономика", unit)
    metrics_payload["metrics"] = metric_rows
    metrics_payload["headline"] = headline
    metrics_payload["notes"] = notes
    (out_dir / "metrics.json").write_text(
        json.dumps(json_ready(metrics_payload), ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8")
    pd.DataFrame(metric_rows, columns=["section", "metric", "dimension", "value"]).to_csv(
        out_dir / "metrics.csv", index=False, encoding="utf-8-sig")

    # --- Отчёты -----------------------------------------------------------
    report = ReportData(
        title=args.title,
        subtitle=f"Источник: {data_dir.name}",
        headline=headline,
        assumptions=assumptions,
        notes=notes,
        warnings=warnings,
        sections=sections,
        quality=quality_frame(quality),
        charts=charts,
        meta=dict(metrics_payload["meta"]),
    )
    created = write_reports(report, out_dir, [f for f in formats if f != "html"])

    if "html" in formats:
        dashboard_path = out_dir / "dashboard.html"
        html_path = out_dir / "report.html"
        has_client_sections = any(
            k in metrics_payload["sections"] for k in ("portfolio", "funnel", "finance", "comparison")
        )
        if dashboard_path.exists() and not has_client_sections:
            dashboard_path.replace(html_path)
        else:
            write_html(report, html_path)
        created["html"] = html_path

    log(args, "")
    log(args, "Готово:")
    for name, path in created.items():
        log(args, f"  {name:5s} {path}")
    log(args, f"  json  {out_dir / 'metrics.json'}")
    log(args, f"  csv   {out_dir / 'metrics.csv'}")
    if warnings:
        log(args, f"Предупреждений: {len(warnings)} (см. metrics.json → warnings)")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return run(args)
    except (DataLoadError, MetricError, ValueError) as exc:
        print(f"Ошибка данных: {exc}", file=sys.stderr)
        return 2
    except FileNotFoundError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 2
    except Exception:  # noqa: BLE001 - показываем трассировку, но с понятным кодом
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
