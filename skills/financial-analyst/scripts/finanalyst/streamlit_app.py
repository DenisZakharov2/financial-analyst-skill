"""Streamlit UI для Financial Analyst Skill.

Запуск:
    streamlit run skills/financial-analyst/scripts/finanalyst/streamlit_app.py

Или через установленный пакет:
    financial-analyst-streamlit
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import threading
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import pandas as pd
import streamlit as st

# Добавляем путь к модулям finanalyst ДО импортов
SCRIPTS_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPTS_DIR))

from finanalyst.cli import (  # noqa: E402
    _init_metrics_payload,
    _Pipeline,
    _prepare_dirs,
    _stage_ab_report,
    _stage_ab_test,
    _stage_cohort,
    _stage_comparison,
    _stage_credit_risk,
    _stage_finance,
    _stage_funnel,
    _stage_load,
    _stage_macro,
    _stage_macro_tables,
    _stage_metrics,
    _stage_migration,
    _stage_plugins,
    _stage_portfolio,
    _stage_quality,
    _stage_reports,
    _stage_scenario,
    _stage_segmentation,
    _stage_vintage,
)
from finanalyst.visuals import build_dashboard, chart_forecast, chart_funnel, chart_portfolio  # noqa: E402

# ============================================================
# Константы и конфигурация страницы
# ============================================================

st.set_page_config(
    page_title="Financial Analyst",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)

REQUIRED_TABLES = [
    "portfolio",
    "clients",
    "finance",
    "products",
    "funnel",
    "sales",
]

OPTIONAL_TABLES = [
    "experiments",
    "macro",
    "marketing",
]

EXPECTED_COLUMNS = {
    "portfolio": [
        "account_id",
        "client_id",
        "product_code",
        "open_date",
        "close_date",
        "limit_amount",
        "balance",
        "currency",
        "status",
        "dpd",
        "par_bucket",
    ],
    "clients": [
        "client_id",
        "segment",
        "region",
        "acquisition_channel",
        "registration_date",
        "age",
        "gender",
    ],
    "finance": [
        "period",
        "product_code",
        "interest_income",
        "interest_expense",
        "fee_income",
        "funding_cost",
        "opex",
        "reserve_expense",
        "tax",
        "marketing_cost",
    ],
    "products": [
        "product_code",
        "product_type",
        "product_name",
        "interest_rate",
        "tariff",
        "cost_of_funds",
        "reserve_rate",
    ],
    "funnel": [
        "period",
        "stage",
        "count",
        "product_code",
    ],
    "sales": [
        "period",
        "product_code",
        "amount",
        "deals",
        "source",
    ],
}


# ============================================================
# Состояние сессии
# ============================================================

@dataclass
class AppState:
    """Состояние приложения в session_state."""
    data_dir: Path | None = None
    tables: dict[str, pd.DataFrame] = field(default_factory=dict)
    tables_loaded: bool = False
    config: dict = field(default_factory=dict)
    results: dict | None = None
    metrics_payload: dict | None = None
    scenario_results: list | None = None
    scenario_summary: dict | None = None
    running: bool = False
    progress: float = 0.0
    status_message: str = ""
    error: str | None = None
    charts_dir: Path | None = None
    out_dir: Path | None = None


def get_state() -> AppState:
    """Получить или создать состояние приложения."""
    if "app_state" not in st.session_state:
        st.session_state.app_state = AppState()
    return st.session_state.app_state


# ============================================================
# Вспомогательные функции
# ============================================================

def save_uploaded_files(uploaded_files: list) -> Path:
    """Сохранить загруженные файлы во временную директорию."""
    temp_dir = Path(tempfile.mkdtemp(prefix="finanalyst_"))
    for uploaded_file in uploaded_files:
        file_path = temp_dir / uploaded_file.name
        with open(file_path, "wb") as f:
            f.write(uploaded_file.getbuffer())
    return temp_dir


def load_data_preview(data_dir: Path) -> dict[str, pd.DataFrame]:
    """Загрузить превью данных (первые 5 строк каждой таблицы)."""
    tables = {}
    for table_name in REQUIRED_TABLES + OPTIONAL_TABLES:
        for ext in [".csv", ".xlsx", ".parquet"]:
            file_path = data_dir / f"{table_name}{ext}"
            if file_path.exists():
                try:
                    if ext == ".csv":
                        df = pd.read_csv(file_path, nrows=5)
                    elif ext == ".xlsx":
                        df = pd.read_excel(file_path, nrows=5)
                    else:
                        df = pd.read_parquet(file_path).head(5)
                    tables[table_name] = df
                    break
                except Exception:
                    pass
    return tables


def validate_table_columns(table_name: str, df: pd.DataFrame) -> tuple[bool, list[str]]:
    """Проверить наличие ожидаемых колонок."""
    if table_name not in EXPECTED_COLUMNS:
        return True, []
    expected = EXPECTED_COLUMNS[table_name]
    missing = [col for col in expected if col not in df.columns]
    return len(missing) == 0, missing


def build_config_from_ui() -> dict:
    """Построить конфиг из UI элементов."""
    state = get_state()
    return {
        "source": state.config.get("source", "files"),
        "data_dir": str(state.data_dir) if state.data_dir else "./assets/sample",
        "out_dir": str(state.out_dir) if state.out_dir else "./reports",
        "output_formats": state.config.get("output_formats", ["html", "xlsx"]),
        "title": state.config.get("title", "Финансовый аналитический отчёт"),
        "forecast_horizon": state.config.get("forecast_horizon", 4),
        "forecast_band": state.config.get("forecast_band", 1.0),
        "forecast_model": state.config.get("forecast_model", "auto"),
        "strict": state.config.get("strict", False),
        "pii_mode": state.config.get("pii_mode", "strict"),
        "chunksize": state.config.get("chunksize"),
        "lifetime_months": state.config.get("lifetime_months", 24.0),
        "ab_test": state.config.get("ab_test", {}),
        "segmentation": state.config.get("segmentation", {}),
        "cohort": state.config.get("cohort", {}),
        "migration": state.config.get("migration", {}),
        "vintage": state.config.get("vintage", {}),
        "credit_risk": state.config.get("credit_risk", {}),
        "drivers": state.config.get("drivers", {}),
        "comparison": state.config.get("comparison", {}),
        "unit_economics": state.config.get("unit_economics", {}),
        "scenario": state.config.get("scenario", {}),
    }


def run_pipeline_with_progress(state: AppState, config: dict) -> int:
    """Запустить пайплайн с обновлением прогресса."""
    try:
        state.running = True
        state.progress = 0.0
        state.status_message = "Подготовка директорий..."
        state.error = None

        # Создаём Namespace из конфига для совместимости с CLI
        args_dict = {
            "config": None,
            "source": config["source"],
            "data_dir": Path(config["data_dir"]),
            "out": Path(config["out_dir"]),
            "format": ",".join(config["output_formats"]),
            "adapter": "auto",
            "indicator": None,
            "lifetime_months": config["lifetime_months"],
            "forecast_h": config["forecast_horizon"],
            "band": config["forecast_band"],
            "forecast_model": config["forecast_model"],
            "ab_test": config["ab_test"].get("enabled", False),
            "ab_metric": config["ab_test"].get("metric", "value"),
            "ab_metric_type": config["ab_test"].get("metric_type", "continuous"),
            "ab_denominator": config["ab_test"].get("denominator"),
            "ab_pre_metric": config["ab_test"].get("pre_metric"),
            "ab_group_col": config["ab_test"].get("group_col", "experiment_group"),
            "ab_lower_is_better": config["ab_test"].get("lower_is_better", False),
            "segment": config["segmentation"].get("enabled", False),
            "no_cluster": config["segmentation"].get("no_cluster", False),
            "cohort": config["cohort"].get("enabled", False),
            "migration": config["migration"].get("enabled", False),
            "migration_mode": config["migration"].get("mode", "clients"),
            "vintage": config["vintage"].get("enabled", False),
            "credit_risk": config["credit_risk"].get("enabled", False),
            "drivers": config["drivers"].get("enabled", False),
            "drivers_max_lag": config["drivers"].get("max_lag", 3),
            "strict": config["strict"],
            "pii_mode": config["pii_mode"],
            "chunksize": config["chunksize"],
            "plugins_dir": None,
            "scenario": config["scenario"].get("config_file"),
            "scenario_list": config["scenario"].get("preset"),
            "scenario_param": config["scenario"].get("params"),
            "compare_dirs": None,
            "labels": ["base", "target"],
            "title": config["title"],
            "quiet": True,
            "pg_host": None,
            "pg_port": None,
            "pg_database": None,
            "pg_user": None,
            "pg_password": None,
            "pg_schema": None,
            "pg_chunksize": None,
        }
        args = argparse.Namespace(**args_dict)

        # Запускаем этапы с обновлением прогресса
        stages: list[tuple[str, Callable[..., Any]]] = [
            ("Подготовка директорий", _prepare_dirs),
            ("Загрузка данных", _stage_load),
            ("A/B тест (подготовка)", _stage_ab_test),
            ("Контроль качества", _stage_quality),
            ("Инициализация метрик", _init_metrics_payload),
            ("Плагины", _stage_plugins),
            ("Сценарийный анализ", _stage_scenario),
            ("Портфель", _stage_portfolio),
            ("Воронка", _stage_funnel),
            ("A/B отчёт", _stage_ab_report),
            ("Сегментация", _stage_segmentation),
            ("Когорты", _stage_cohort),
            ("Финансы", _stage_finance),
            ("Макро", _stage_macro),
            ("Макро таблицы", _stage_macro_tables),
            ("Сравнение", _stage_comparison),
            ("Миграция", _stage_migration),
            ("Винтажный анализ", _stage_vintage),
            ("Кредитный риск", _stage_credit_risk),
            ("Метрики", _stage_metrics),
            ("Отчёты", _stage_reports),
        ]

        # Создаём pipeline object
        data_dir, out_dir, charts_dir, formats, chunksize, pipe_config = _prepare_dirs(args)
        state.charts_dir = charts_dir
        state.out_dir = out_dir

        p = _Pipeline(
            args=args,
            data_dir=data_dir,
            out_dir=out_dir,
            charts_dir=charts_dir,
            formats=formats,
            config=pipe_config,
            chunksize=chunksize,
        )

        for i, (name, stage_fn) in enumerate(stages):
            if not state.running:
                return 1
            state.status_message = name
            state.progress = (i + 1) / len(stages)
            stage_fn(p)

        state.results = p.metrics_payload
        state.metrics_payload = p.metrics_payload
        state.tables = p.tables

        # Извлекаем результаты сценариев
        if "scenarios" in p.metrics_payload.get("sections", {}):
            scenario_data = p.metrics_payload["sections"]["scenarios"]
            state.scenario_results = scenario_data.get("details", [])
            state.scenario_summary = scenario_data.get("summary", {})

        return 0

    except Exception as e:
        state.error = f"{type(e).__name__}: {e}"
        traceback.print_exc()
        return 1
    finally:
        state.running = False
        state.progress = 1.0
        state.status_message = "Завершено" if not state.error else "Ошибка"


def display_metric_card(label: str, value: str, delta: str | None = None):
    """Отобразить метрику в виде карточки."""
    st.metric(label, value, delta=delta)


def display_dataframe_with_download(df: pd.DataFrame, name: str, key: str):
    """Отобразить DataFrame с кнопкой скачивания."""
    if df.empty:
        st.info(f"Нет данных для {name}")
        return

    col1, col2 = st.columns([4, 1])
    with col1:
        st.dataframe(df, use_container_width=True, key=f"df_{key}")
    with col2:
        csv = df.to_csv(index=False).encode("utf-8")
        st.download_button(
            "📥 CSV",
            csv,
            f"{name}.csv",
            "text/csv",
            key=f"dl_{key}",
        )


# ============================================================
# Страницы приложения
# ============================================================

def page_upload():
    """Страница загрузки данных."""
    state = get_state()

    st.header("📁 Загрузка данных")

    st.markdown("""
    Загрузите CSV/Excel файлы с данными. Обязательные таблицы:
    - **portfolio** — портфель счетов
    - **clients** — клиенты
    - **finance** — финансовые данные
    - **products** — продукты
    - **funnel** — воронка продаж
    - **sales** — продажи
    """)

    # Drag-and-drop загрузка
    uploaded_files = st.file_uploader(
        "Перетащите файлы сюда или нажмите для выбора",
        type=["csv", "xlsx", "parquet"],
        accept_multiple_files=True,
        help="Ожидаемые имена файлов: portfolio.csv, clients.csv, finance.csv, products.csv, funnel.csv, sales.csv",
    )

    if uploaded_files:
        if st.button("💾 Сохранить и загрузить", type="primary"):
            with st.spinner("Сохранение файлов..."):
                temp_dir = save_uploaded_files(uploaded_files)
                state.data_dir = temp_dir
                state.tables = load_data_preview(temp_dir)
                state.tables_loaded = True
                st.success(f"Файлы сохранены в {temp_dir}")

    # Или используем пример
    st.divider()
    st.subheader("Или используйте пример")
    if st.button("📂 Загрузить пример данных"):
        sample_dir = SCRIPTS_DIR.parent / "assets" / "sample"
        if sample_dir.exists():
            state.data_dir = sample_dir
            state.tables = load_data_preview(sample_dir)
            state.tables_loaded = True
            st.success("Пример данных загружен")
        else:
            st.error("Пример данных не найден")

    # Превью загруженных таблиц
    if state.tables_loaded and state.tables:
        st.divider()
        st.subheader("Превью таблиц")

        tabs = st.tabs(list(state.tables.keys()))
        for i, (name, df) in enumerate(state.tables.items()):
            with tabs[i]:
                valid, missing = validate_table_columns(name, df)
                if valid:
                    st.success(f"✅ {name}: структура корректна")
                else:
                    st.warning(f"⚠️ {name}: отсутствуют колонки: {', '.join(missing)}")

                st.dataframe(df, use_container_width=True)
                st.caption(f"Показано первых {len(df)} строк. Всего строк будет определено при запуске.")


def page_config():
    """Страница конфигурации."""
    state = get_state()

    st.header("⚙️ Конфигурация пайплайна")

    if not state.tables_loaded:
        st.warning("Сначала загрузите данные на вкладке 'Загрузка данных'")
        return

    with st.form("config_form"):
        col1, col2 = st.columns(2)

        with col1:
            st.subheader("Основные")
            state.config["source"] = st.selectbox(
                "Источник данных",
                ["files", "postgres"],
                index=0,
            )
            state.config["title"] = st.text_input(
                "Заголовок отчёта",
                value=state.config.get("title", "Финансовый аналитический отчёт"),
            )
            state.config["output_formats"] = st.multiselect(
                "Форматы отчётов",
                ["docx", "pdf", "xlsx", "pptx", "rtf", "html"],
                default=state.config.get("output_formats", ["html", "xlsx"]),
            )

            st.subheader("Прогноз")
            state.config["forecast_horizon"] = st.number_input(
                "Горизонт прогноза (периодов)",
                min_value=1,
                max_value=24,
                value=state.config.get("forecast_horizon", 4),
            )
            state.config["forecast_band"] = st.number_input(
                "Ширина доверительного интервала (±band × MAE)",
                min_value=0.1,
                max_value=5.0,
                value=state.config.get("forecast_band", 1.0),
                step=0.1,
            )
            state.config["forecast_model"] = st.selectbox(
                "Модель прогноза",
                ["legacy", "auto", "linear", "ets", "arx", "sarimax"],
                index=["legacy", "auto", "linear", "ets", "arx", "sarimax"].index(
                    state.config.get("forecast_model", "auto")
                ),
            )

            state.config["lifetime_months"] = st.number_input(
                "Срок жизни клиента для LTV (мес.)",
                min_value=1,
                max_value=120,
                value=state.config.get("lifetime_months", 24),
            )

        with col2:
            st.subheader("Качество данных")
            state.config["strict"] = st.checkbox(
                "Строгий режим (ошибка при провале проверок)",
                value=state.config.get("strict", False),
            )
            state.config["pii_mode"] = st.selectbox(
                "Режим обработки ПДн",
                ["strict", "redact", "off"],
                index=["strict", "redact", "off"].index(state.config.get("pii_mode", "strict")),
            )
            state.config["chunksize"] = st.number_input(
                "Размер чанка (None = полная загрузка)",
                min_value=0,
                value=state.config.get("chunksize") or 0,
                help="0 = отключено",
            )
            if state.config["chunksize"] == 0:
                state.config["chunksize"] = None

            st.subheader("Модули (включить по необходимости)")

            # A/B тест
            with st.expander("🧪 A/B тест"):
                state.config.setdefault("ab_test", {})
                state.config["ab_test"]["enabled"] = st.checkbox(
                    "Включить A/B тест",
                    value=state.config["ab_test"].get("enabled", False),
                )
                if state.config["ab_test"]["enabled"]:
                    state.config["ab_test"]["metric"] = st.text_input(
                        "Метрика", value=state.config["ab_test"].get("metric", "value")
                    )
                    state.config["ab_test"]["metric_type"] = st.selectbox(
                        "Тип метрики",
                        ["continuous", "proportion", "ratio"],
                        index=["continuous", "proportion", "ratio"].index(
                            state.config["ab_test"].get("metric_type", "continuous")
                        ),
                    )

            # Сегментация
            with st.expander("🎯 Сегментация"):
                state.config.setdefault("segmentation", {})
                state.config["segmentation"]["enabled"] = st.checkbox(
                    "Включить сегментацию",
                    value=state.config["segmentation"].get("enabled", False),
                )
                if state.config["segmentation"]["enabled"]:
                    state.config["segmentation"]["no_cluster"] = st.checkbox(
                        "Только RFM+Risk (без кластеризации)",
                        value=state.config["segmentation"].get("no_cluster", False),
                    )

            # Когорты
            with st.expander("📈 Когортный анализ"):
                state.config.setdefault("cohort", {})
                state.config["cohort"]["enabled"] = st.checkbox(
                    "Включить когортный анализ",
                    value=state.config["cohort"].get("enabled", False),
                )

            # Кредитный риск
            with st.expander("🏦 Кредитный риск"):
                state.config.setdefault("credit_risk", {})
                state.config["credit_risk"]["enabled"] = st.checkbox(
                    "Включить кредитный риск",
                    value=state.config["credit_risk"].get("enabled", False),
                )

            # Драйверы
            with st.expander("📊 Драйверы (PVM, PAR shift)"):
                state.config.setdefault("drivers", {})
                state.config["drivers"]["enabled"] = st.checkbox(
                    "Включить драйверы",
                    value=state.config["drivers"].get("enabled", False),
                )

            # Сценарийный анализ
            with st.expander("🎲 Сценарийный анализ (What-if)"):
                state.config.setdefault("scenario", {})
                state.config["scenario"]["enabled"] = st.checkbox(
                    "Включить сценарийный анализ",
                    value=state.config["scenario"].get("enabled", False),
                )
                if state.config["scenario"]["enabled"]:
                    state.config["scenario"]["preset"] = st.selectbox(
                        "Пресет сценариев",
                        [None, "standard", "all"],
                        format_func=lambda x: x or "Не выбран",
                    )

        submitted = st.form_submit_button("💾 Сохранить конфигурацию", type="primary")
        if submitted:
            st.success("Конфигурация сохранена")


def page_run():
    """Страница запуска пайплайна."""
    state = get_state()

    st.header("🚀 Запуск анализа")

    if not state.tables_loaded:
        st.warning("Сначала загрузите данные")
        return

    if not state.config:
        st.warning("Настройте конфигурацию")
        return

    # Показываем сводку конфигурации
    with st.expander("📋 Текущая конфигурация", expanded=False):
        st.json({k: v for k, v in state.config.items() if not k.startswith("_")})

    col1, col2, col3 = st.columns([1, 2, 1])

    with col1:
        if st.button("▶️ Запустить", type="primary", disabled=state.running, use_container_width=True):
            config = build_config_from_ui()
            thread = threading.Thread(target=run_pipeline_with_progress, args=(state, config))
            thread.start()

    with col2:
        if state.running:
            if st.button("⏹ Остановить", use_container_width=True):
                state.running = False
                st.warning("Остановка...")

    with col3:
        if state.results and st.button("🔄 Новый запуск", use_container_width=True):
            state.results = None
            state.metrics_payload = None
            state.scenario_results = None
            state.scenario_summary = None
            state.error = None
            st.rerun()

    # Прогресс-бар
    if state.running or state.progress > 0:
        st.progress(state.progress, text=state.status_message)

    # Ошибка
    if state.error:
        st.error(f"❌ {state.error}")

    # Результаты
    if state.results and not state.running:
        st.success("✅ Анализ завершен успешно!")

        # Ключевые метрики
        st.subheader("📊 Ключевые показатели")
        sections = state.results.get("sections", {})

        # Portfolio
        if "portfolio" in sections:
            portfolio_data = sections["portfolio"]
            if "totals" in portfolio_data:
                totals = portfolio_data["totals"]
                cols = st.columns(4)
                with cols[0]:
                    display_metric_card(
                        "Общий баланс",
                        f"{totals.get('total_balance', 0):,.0f} руб."
                    )
                with cols[1]:
                    display_metric_card(
                        "PAR 30+",
                        f"{totals.get('par_30_pct', 0):.2f}%"
                    )
                with cols[2]:
                    display_metric_card(
                        "NPL",
                        f"{totals.get('npl_ratio', 0):.2f}%"
                    )
                with cols[3]:
                    display_metric_card(
                        "Кол-во счетов",
                        f"{totals.get('account_count', 0):,}"
                    )

        # Finance
        if "finance" in sections:
            finance_data = sections["finance"]
            if "summary" in finance_data:
                summary = finance_data["summary"]
                cols = st.columns(4)
                with cols[0]:
                    display_metric_card(
                        "NIM",
                        f"{summary.get('nim_pct', 0):.2f}%"
                    )
                with cols[1]:
                    display_metric_card(
                        "Выручка",
                        f"{summary.get('total_revenue', 0):,.0f} руб."
                    )
                with cols[2]:
                    display_metric_card(
                        "Прибыль",
                        f"{summary.get('net_profit', 0):,.0f} руб."
                    )
                with cols[3]:
                    display_metric_card(
                        "C/I",
                        f"{summary.get('cost_income_ratio', 0):.2f}%"
                    )

        # Unit Economics
        if "unit_economics" in sections:
            ue = sections["unit_economics"]
            if "summary" in ue:
                summary = ue["summary"]
                cols = st.columns(4)
                with cols[0]:
                    display_metric_card(
                        "LTV/CAC",
                        f"{summary.get('ltv_cac_ratio', 0):.2f}"
                    )
                with cols[1]:
                    display_metric_card(
                        "CAC Payback",
                        f"{summary.get('cac_payback_months', 0):.1f} мес."
                    )
                with cols[2]:
                    display_metric_card(
                        "ARPU",
                        f"{summary.get('arpu', 0):,.0f} руб."
                    )
                with cols[3]:
                    display_metric_card(
                        "LTV",
                        f"{summary.get('ltv_gross', 0):,.0f} руб."
                    )


def page_results():
    """Страница результатов и метрик."""
    state = get_state()

    st.header("📈 Результаты и метрики")

    if not state.metrics_payload:
        st.info("Запустите анализ для получения результатов")
        return

    sections = state.metrics_payload.get("sections", {})

    # Табы по секциям
    section_names = list(sections.keys())
    if not section_names:
        st.warning("Нет данных в секциях")
        return

    tabs = st.tabs(section_names)

    for i, (name, tab) in enumerate(zip(section_names, tabs)):
        with tab:
            section_data = sections[name]

            # Если есть rows (DataFrame)
            if "rows" in section_data and section_data["rows"]:
                df = pd.DataFrame(section_data["rows"])
                display_dataframe_with_download(df, name, f"section_{name}")

            # Если есть summary
            if "summary" in section_data:
                st.subheader("Сводка")
                st.json(section_data["summary"])

            # Если есть totals
            if "totals" in section_data:
                st.subheader("Итоги")
                st.json(section_data["totals"])

            # Если есть profiles (сегментация)
            if "profiles" in section_data:
                st.subheader("Профили сегментов")
                df = pd.DataFrame(section_data["profiles"])
                display_dataframe_with_download(df, f"{name}_profiles", f"profiles_{name}")

            # Если есть curves (когорты)
            if "curves" in section_data:
                st.subheader("Retention кривые")
                df = pd.DataFrame(section_data["curves"])
                display_dataframe_with_download(df, f"{name}_curves", f"curves_{name}")


def page_scenarios():
    """Страница сценарийного анализа."""
    state = get_state()

    st.header("🎲 Сценарийный анализ (What-if)")

    if not state.scenario_results:
        st.info("Сценарийный анализ не запущен. Включите его в конфигурации и запустите пайплайн.")
        return

    st.subheader("Сравнение сценариев")

    # DataFrame со сценариями
    if state.scenario_results:
        df = pd.DataFrame(state.scenario_results)
        display_dataframe_with_download(df, "scenarios_comparison", "scenarios_df")

        # Визуализация ключевых метрик
        if len(df) > 1:
            st.subheader("Визуализация отклонений от базового")

            # Находим числовые колонки
            numeric_cols = df.select_dtypes(include=["number"]).columns.tolist()
            if "scenario" in df.columns:
                numeric_cols = [c for c in numeric_cols if c != "scenario"]

            if numeric_cols:
                metric = st.selectbox("Метрика для графика", numeric_cols)

                # Bar chart
                chart_data = df.set_index("scenario")[metric]
                st.bar_chart(chart_data)

    # Сводка отклонений
    if state.scenario_summary:
        st.subheader("Сводка отклонений от базового сценария")
        for scenario, diffs in state.scenario_summary.items():
            with st.expander(f"📊 {scenario}"):
                diff_df = pd.DataFrame([
                    {"Метрика": k.replace("_abs", "").replace("_pct", ""),
                     "Абс. изменение": v if "_abs" in k else None,
                     "% изменение": v if "_pct" in k else None}
                    for k, v in diffs.items()
                ])
                st.dataframe(diff_df, use_container_width=True)


def page_charts():
    """Страница графиков."""
    state = get_state()

    st.header("📊 Графики и визуализации")

    if not state.tables or not state.results:
        st.info("Запустите анализ для построения графиков")
        return

    tables = state.tables

    # Portfolio charts
    if "portfolio" in tables:
        with st.expander("📦 Портфель", expanded=True):
            col1, col2 = st.columns(2)

            with col1:
                try:
                    fig = chart_portfolio(tables["portfolio"])
                    st.pyplot(fig)
                except Exception as e:
                    st.warning(f"Не удалось построить график портфеля: {e}")

            with col2:
                try:
                    # PAR распределение
                    df = tables["portfolio"]
                    if "par_bucket" in df.columns:
                        par_counts = df["par_bucket"].value_counts().sort_index()
                        st.bar_chart(par_counts)
                except Exception as e:
                    st.warning(f"Ошибка: {e}")

    # Funnel charts
    if "funnel" in tables:
        with st.expander("🔻 Воронка"):
            try:
                fig = chart_funnel(tables["funnel"])
                st.pyplot(fig)
            except Exception as e:
                st.warning(f"Не удалось построить воронку: {e}")

    # Forecast charts
    if "finance" in tables:
        with st.expander("📈 Прогноз"):
            if "forecast" in state.results.get("sections", {}):
                forecast_data = state.results["sections"]["forecast"]
                if "rows" in forecast_data:
                    try:
                        fig = chart_forecast(forecast_data)
                        st.pyplot(fig)
                    except Exception as e:
                        st.warning(f"Ошибка прогноза: {e}")

    # Dashboard
    with st.expander("📋 Дашборд (комбинированный)"):
        if st.button("Построить дашборд"):
            try:
                with st.spinner("Построение..."):
                    fig = build_dashboard(tables, state.results)
                    st.pyplot(fig)
            except Exception as e:
                st.warning(f"Ошибка дашборда: {e}")


def page_reports():
    """Страница отчётов и скачивания."""
    state = get_state()

    st.header("📄 Отчёты")

    if not state.out_dir or not state.out_dir.exists():
        st.info("Отчёты появятся после запуска анализа")
        return

    st.subheader("Сгенерированные файлы")

    report_files = list(state.out_dir.glob("*"))
    if not report_files:
        st.info("Файлов отчётов не найдено")
        return

    for file_path in sorted(report_files):
        if file_path.is_file():
            col1, col2, col3 = st.columns([3, 1, 1])
            with col1:
                st.write(f"📄 {file_path.name}")
            with col2:
                size_kb = file_path.stat().st_size / 1024
                st.write(f"{size_kb:.1f} KB")
            with col3:
                with open(file_path, "rb") as f:
                    st.download_button(
                        "⬇️",
                        f.read(),
                        file_path.name,
                        key=f"dl_report_{file_path.name}",
                    )

    # metrics.json
    metrics_file = state.out_dir / "metrics.json"
    if metrics_file.exists():
        st.divider()
        st.subheader("metrics.json (просмотр)")
        with open(metrics_file, encoding="utf-8") as f:
            metrics = json.load(f)
        st.json(metrics, expanded=False)


# ============================================================
# Главная функция
# ============================================================

def main():
    """Точка входа Streamlit приложения."""
    # Сайдбар навигации
    st.sidebar.title("📊 Financial Analyst")
    st.sidebar.caption("Версия 2.1.9+")

    pages = {
        "📁 Загрузка данных": page_upload,
        "⚙️ Конфигурация": page_config,
        "🚀 Запуск": page_run,
        "📈 Результаты": page_results,
        "🎲 Сценарии": page_scenarios,
        "📊 Графики": page_charts,
        "📄 Отчёты": page_reports,
    }

    # Сохраняем выбранную страницу в session_state
    if "current_page" not in st.session_state:
        st.session_state.current_page = "📁 Загрузка данных"

    # Навигация в сайдбаре
    selected = st.sidebar.radio(
        "Навигация",
        list(pages.keys()),
        index=list(pages.keys()).index(st.session_state.current_page),
        key="nav_radio",
    )
    st.session_state.current_page = selected

    # Информация о состоянии
    state = get_state()
    if state.tables_loaded:
        st.sidebar.success(f"✅ Данные загружены ({len(state.tables)} таблиц)")
        if state.data_dir:
            st.sidebar.caption(f"📂 {state.data_dir}")
    else:
        st.sidebar.warning("⏳ Данные не загружены")

    if state.results:
        st.sidebar.success("✅ Анализ выполнен")
    elif state.running:
        st.sidebar.info("🔄 Анализ выполняется...")

    # Запуск выбранной страницы
    pages[selected]()


if __name__ == "__main__":
    main()
