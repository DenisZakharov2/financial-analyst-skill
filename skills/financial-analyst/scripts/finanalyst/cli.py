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
from .core import (
    DataLoadError,
    assert_no_blocking_failures,
    load_tables,
    normalize_periods,
    validate_inputs,
)
from .metrics import (
    MetricError,
    finance_summary,
    forecast_trend,
    funnel_summary,
    macro_anomalies,
    macro_dynamics,
    macro_structure,
    portfolio_summary,
    unit_economics,
)
from .reporting import ReportData, fmt_num, quality_frame, write_html, write_reports
from .visuals import (
    build_dashboard,
    chart_anomalies,
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
    parser.add_argument("--strict", action="store_true",
                        help="Завершиться с кодом 2 при провале контроля качества")
    parser.add_argument("--title", default="Финансовый аналитический отчёт")
    parser.add_argument("--quiet", action="store_true")
    return parser


def log(args: argparse.Namespace, message: str) -> None:
    if not args.quiet:
        print(message)


def short(text: str, limit: int = 70) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


# --------------------------------------------------------------------------
# Сериализация
# --------------------------------------------------------------------------

def records(frame: pd.DataFrame | None) -> list[dict]:
    if frame is None or frame.empty:
        return []
    clean = frame.copy()
    for column in clean.columns:
        clean[column] = clean[column].map(
            lambda value: None if (isinstance(value, float) and np.isnan(value)) else value
        )
    clean = clean.astype(object).where(pd.notna(clean), None)
    for column in clean.columns:
        if pd.api.types.is_datetime64_any_dtype(frame[column]):
            clean[column] = frame[column].dt.strftime("%Y-%m-%d")
    return clean.to_dict(orient="records")


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
    for stale in ("dashboard.html", "financial_analyst_report.html", "report.html"):
        (out_dir / stale).unlink(missing_ok=True)
    setup_matplotlib()

    tables = load_tables(data_dir)
    notes: list[str] = []

    survey_paths = [p for p in data_dir.iterdir()
                    if p.suffix.lower() in {".xls", ".xlsx"} and p.name.lower().startswith("stat_")]
    if args.adapter in {"auto", "cbr"} and survey_paths:
        survey = cbr_survey.parse_workbook(survey_paths[0])
        if not survey.empty:
            survey = normalize_periods(survey)
            tables["macro_survey"] = survey
            count = f"{len(survey):,}".replace(",", " ")
            notes.append(f"Выгрузка {survey_paths[0].name}: {count} строк, "
                         f"{survey['industry'].nunique()} отраслей, "
                         f"{survey['indicator'].nunique()} показателей.")
    if args.adapter == "client":
        tables.pop("macro_survey", None)

    tables, alias_notes = client_csv.normalize(tables)
    notes.extend(alias_notes)

    known = {"clients", "products", "portfolio", "accounts", "sales", "applications",
             "funnel_events", "transactions", "finance", "macro_deposits", "macro_loans",
             "macro_profit", "macro_securities", "macro_survey"}
    service = {path.stem for path in survey_paths}
    for key in list(tables):
        if key in service or key.lower().startswith("отчет"):
            tables.pop(key)
            continue
        if key not in known:
            notes.append(f"Пропущена таблица {key!r}: её нет в словаре данных.")
            tables.pop(key)

    quality = validate_inputs(tables)
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
    charts: dict[str, Path] = {}
    metrics_payload: dict = {
        "meta": {
            "version": __version__,
            "data_dir": data_dir.name,
            "adapter": args.adapter,
            "generated_at": pd.Timestamp.now().isoformat(timespec="seconds"),
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
        headline.append({"label": "Остаток по портфелю", "value": fmt_num(total_balance / 1e6, 1),
                         "unit": "млн руб.", "source": "portfolio.balance"})
        headline.append({"label": "Счетов в портфеле", "value": fmt_num(result["totals"]["accounts"], 0),
                         "unit": "шт.", "source": "portfolio.account_id"})
        if result["totals"].get("par30_pct") is not None:
            headline.append({"label": "PAR 30", "value": fmt_num(result["totals"]["par30_pct"], 2),
                             "unit": "%", "source": "portfolio.dpd"})
        chart = chart_portfolio(frame, charts_dir / "portfolio.png")
        if chart:
            charts["Портфель по типам продуктов"] = chart

    # --- Воронка ----------------------------------------------------------
    funnel_source = tables.get("funnel_events")
    funnel_column = "stage"
    if funnel_source is None and tables.get("sales") is not None:
        funnel_source = tables["sales"]
        funnel_column = "funnel_stage"
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
        headline.append({"label": "Сквозная конверсия", "value": fmt_num(last["from_start_pct"], 2),
                         "unit": "%", "source": "funnel_events"})
        headline.append({"label": "Участников воронки", "value": fmt_num(funnel["entities"], 0),
                         "unit": funnel["entity"], "source": "funnel_events"})
        chart = chart_funnel(funnel["rows"], charts_dir / "funnel.png")
        if chart:
            charts["Воронка продаж"] = chart

    # --- Финансы и юнит-экономика ----------------------------------------
    finance = tables.get("finance")
    if finance is not None and not finance.empty:
        fin = finance_summary(finance, portfolio)
        sections.append(("Финансовые показатели", fin["rows"], "Выручка = проценты + комиссии + премии; "
                                                            "прямые расходы = фондирование + резервы."))
        totals = fin["totals"]
        metrics_payload["sections"]["finance"] = {"totals": totals, "rows": records(fin["rows"])}
        headline.append({"label": "Выручка", "value": fmt_num(totals["revenue"] / 1e6, 1),
                         "unit": "млн руб.", "source": "finance"})
        if totals.get("margin_pct") is not None:
            headline.append({"label": "Валовая маржа", "value": fmt_num(totals["margin_pct"], 2),
                             "unit": "%", "source": "finance"})
        unit = unit_economics(finance, clients, portfolio, tables.get("sales"),
                              lifetime_months=args.lifetime_months)
        assumptions.extend(unit["assumptions"])
        unit_frame = pd.DataFrame([
            {"Показатель": "Выручка", "Значение": fmt_num(unit["revenue"] / 1e6, 2), "Ед.": "млн руб."},
            {"Показатель": "Активные клиенты", "Значение": fmt_num(unit["active_clients"], 0), "Ед.": "чел."},
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
            headline.append({"label": "LTV / CAC", "value": fmt_num(unit["ltv_cac"], 2),
                             "unit": "x", "source": "finance, portfolio"})

    # --- Макро ------------------------------------------------------------
    indicator = None
    if survey is not None and not survey.empty:
        indicator = args.indicator or cbr_survey.choose_indicator(survey)
        industry = _pick_industry(survey, indicator)
        series = _series(survey, indicator, industry)
        sections.append(("Макро: динамика показателя", series.tail(36).assign(
            period_start=lambda d: d["period_start"].dt.strftime("%Y-%m")),
            f"Показатель: {indicator}. Отрасль: {industry}."))
        anomalies = macro_anomalies(series, value_col="value", group_cols=())
        if not anomalies.empty:
            view = anomalies.assign(period_start=lambda d: d["period_start"].dt.strftime("%Y-%m"))
            sections.append(("Макро: аномалии", view[["period_start", "value", "_z"]].rename(
                columns={"period_start": "Период", "value": "Значение", "_z": "Z-score"}), ""))
        metrics_payload["sections"]["macro"] = {
            "indicator": indicator,
            "industry": industry,
            "points": int(len(series)),
            "anomalies": records(anomalies),
        }
        headline.append({"label": f"{short(indicator)} (последнее)", "value": fmt_num(series["value"].iloc[-1], 2),
                         "unit": "п.", "source": f"macro_survey, {industry}"})
        chart = chart_series(series, "value", "period_start", charts_dir / "macro.png",
                             label=industry)
        if chart:
            charts["Динамика по отрасли"] = chart
        if not anomalies.empty:
            chart = chart_anomalies(anomalies, charts_dir / "anomalies.png", label=industry)
            if chart:
                charts["Аномалии по отрасли"] = chart

        forecast = forecast_trend(series, horizon=args.forecast_h, band=args.band)
        metrics_payload["sections"]["forecast"] = forecast
        if forecast.get("available"):
            assumptions.append(forecast["assumption"])
            frame = pd.DataFrame({
                "Период": forecast["periods"],
                **{name: values for name, values in forecast["scenarios"].items()},
            })
            sections.append(("Прогноз: значения", frame,
                             f"MAE: {forecast['mae']}; MAPE: {forecast['mape_pct']} %"))
            chart = chart_forecast(forecast, charts_dir / "forecast.png")
            if chart:
                charts["Прогноз по сценариям"] = chart

        build_dashboard(survey, indicator, out_dir / "dashboard.html",
                        title=f"Дашборд: {indicator}")

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
            period_start=lambda d: d["period_start"].dt.strftime("%Y-%m"))
        sections.append((f"{title}: динамика", view[["period_start", value_col, "change_pct"]], ""))
        anomalies = macro_anomalies(table, value_col=value_col,
                                    group_cols=(group_col,) if group_col else ())
        if not anomalies.empty:
            view_a = anomalies.assign(
                period_start=lambda d: d["period_start"].dt.strftime("%Y-%m"))
            columns = [c for c in ("period_start", group_col, value_col, "_z") if c and c in view_a.columns]
            sections.append((f"{title}: аномалии", view_a[columns], ""))
        chart = chart_series(totals, value_col, "period_start",
                             charts_dir / f"{key}.png", title=f"{title}: динамика")
        if chart:
            charts[f"{title}: динамика"] = chart
        headline.append({"label": f"{title} (последнее)",
                         "value": fmt_num(totals[value_col].iloc[-1], 2),
                         "unit": "млн руб.", "source": key})
        metrics_payload["sections"][key] = {
            "rows": records(view), "anomalies": records(anomalies),
        }

    # --- Плоские метрики --------------------------------------------------
    metric_rows = flatten_metrics(sections)
    metrics_payload["metrics"] = metric_rows
    metrics_payload["headline"] = headline
    metrics_payload["notes"] = notes
    (out_dir / "metrics.json").write_text(
        json.dumps(metrics_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    pd.DataFrame(metric_rows).to_csv(out_dir / "metrics.csv", index=False, encoding="utf-8-sig")

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
        meta={"data_dir": data_dir.name, "version": __version__},
    )
    created = write_reports(report, out_dir, [f for f in formats if f != "html"])

    if "html" in formats:
        dashboard_path = out_dir / "dashboard.html"
        html_path = out_dir / "report.html"
        if dashboard_path.exists():
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
