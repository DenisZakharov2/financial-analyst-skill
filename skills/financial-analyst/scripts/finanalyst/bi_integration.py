"""BI Integration: экспорт в форматы BI-систем и коннекторы к DWH.

Поддерживаемые целевые системы:
- **Файловые форматы**: CSV, Parquet, Feather (Arrow), TDE (Tableau), XLSX
- **Облачные DWH**: ClickHouse, PostgreSQL, BigQuery, Snowflake
- **BI-инструменты**: PowerBI (PBIX/CSV), Tableau (Hyper/TDE), Metabase/Superset (SQL)

Архитектура:
- `BIExporter` — базовый класс для файловых экспортов
- `DWHConnector` — базовый класс для подключений к DWH
- `BIRegistry` — реестр доступных экспортеров/коннекторов
- `export_to_bi()` — единая точка входа
"""

from __future__ import annotations

import abc
import json
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

try:
    import pyarrow as pa
    import pyarrow.feather as feather
    import pyarrow.parquet as pq
    HAS_ARROW = True
except ImportError:
    HAS_ARROW = False

try:
    import sqlalchemy as sa
    HAS_SQLALCHEMY = True
except ImportError:
    HAS_SQLALCHEMY = False

try:
    from google.cloud import bigquery
    HAS_BIGQUERY = True
except ImportError:
    HAS_BIGQUERY = False


# ============================================================
# Конфигурация
# ============================================================

@dataclass
class BIExportConfig:
    """Конфигурация экспорта в BI."""
    # Выходная директория
    output_dir: Path = Path("./bi_export")

    # Форматы для файловых экспортов
    file_formats: list[str] = field(default_factory=lambda: ["parquet", "csv"])

    # Сжатие
    compression: str = "snappy"  # snappy, gzip, zstd, none

    # Партиционирование (для больших данных)
    partition_cols: list[str] | None = None  # e.g., ["reporting_date", "product_code"]
    partition_date: date | None = None

    # Очистка перед экспортом
    clean_output_dir: bool = True

    # Префикс файлов
    file_prefix: str = "finanalyst"


@dataclass
class DWHConnectionConfig:
    """Базовая конфигурация подключения к DWH."""
    host: str
    port: int
    database: str
    user: str
    password: str
    schema: str = "public"

    # Дополнительные параметры
    extra_params: dict = field(default_factory=dict)


@dataclass
class ClickHouseConfig(DWHConnectionConfig):
    """Конфигурация ClickHouse."""
    host: str = "localhost"
    port: int = 8123
    database: str = "default"
    user: str = "default"
    password: str = ""
    schema: str = "default"

    # ClickHouse-specific
    use_http: bool = True
    compress: bool = True


@dataclass
class PostgreSQLConfig(DWHConnectionConfig):
    """Конфигурация PostgreSQL."""
    host: str = "localhost"
    port: int = 5432
    database: str = "finanalyst"
    user: str = "analyst"
    password: str = ""
    schema: str = "public"

    # PostgreSQL-specific
    sslmode: str = "prefer"
    application_name: str = "financial_analyst"


@dataclass
class BigQueryConfig:
    """Конфигурация BigQuery."""
    project_id: str
    dataset_id: str
    location: str = "US"
    credentials_path: str | None = None  # Path to service account JSON

    # Используется GOOGLE_APPLICATION_CREDENTIALS если None


# ============================================================
# Базовые классы
# ============================================================

class BIExporter(abc.ABC):
    """Базовый класс для файловых экспортеров."""

    @property
    @abc.abstractmethod
    def format_name(self) -> str:
        """Название формата (например, 'parquet')."""
        ...

    @property
    @abc.abstractmethod
    def file_extension(self) -> str:
        """Расширение файла (например, '.parquet')."""
        ...

    @abc.abstractmethod
    def export(
        self,
        df: pd.DataFrame,
        path: Path,
        config: BIExportConfig,
    ) -> Path:
        """Экспорт DataFrame в файл."""
        ...

    def _prepare_path(self, base_path: Path, table_name: str, config: BIExportConfig) -> Path:
        """Подготовка пути с учётом партиционирования."""
        if config.partition_cols and config.partition_date:
            # Создаём партиционированную структуру: table/date=YYYY-MM-DD/
            partition_path = base_path / table_name
            for col in config.partition_cols:
                if col == "reporting_date":
                    partition_path = partition_path / f"{col}={config.partition_date.isoformat()}"
                elif col == "date":
                    partition_path = partition_path / f"{col}={config.partition_date.isoformat()}"
                else:
                    partition_path = partition_path / f"{col}=unknown"
            partition_path.mkdir(parents=True, exist_ok=True)
            return partition_path / f"{table_name}{self.file_extension}"
        else:
            base_path.mkdir(parents=True, exist_ok=True)
            return base_path / f"{table_name}{self.file_extension}"


class DWHConnector(abc.ABC):
    """Базовый класс для коннекторов к DWH."""

    def __init__(self, config: Any = None) -> None:
        """Базовый конструктор. Конфиг может быть любым типом в наследниках."""
        ...

    @property
    @abc.abstractmethod
    def dialect_name(self) -> str:
        """Название диалекта (clickhouse, postgresql, bigquery)."""
        ...

    @abc.abstractmethod
    def connect(self) -> Any:
        """Установка соединения."""
        ...

    @abc.abstractmethod
    def write_table(
        self,
        df: pd.DataFrame,
        table_name: str,
        if_exists: str = "append",
        dtype: dict | None = None,
    ) -> int:
        """Запись DataFrame в таблицу. Возвращает количество строк."""
        ...

    @abc.abstractmethod
    def execute(self, query: str) -> Any:
        """Выполнение произвольного SQL."""
        ...

    @abc.abstractmethod
    def close(self) -> None:
        """Закрытие соединения."""
        ...

    def create_schema_if_not_exists(self, schema: str) -> None:
        """Создание схемы если не существует (опционально)."""
        pass


# ============================================================
# Файловые экспортеры
# ============================================================

class CSVExporter(BIExporter):
    """Экспорт в CSV."""

    @property
    def format_name(self) -> str:
        return "csv"

    @property
    def file_extension(self) -> str:
        return ".csv"

    def export(self, df: pd.DataFrame, path: Path, config: BIExportConfig) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)

        # Конвертируем numpy типы
        df = df.copy()
        for col in df.columns:
            if df[col].dtype == "object":
                df[col] = df[col].apply(
                    lambda x: x.item() if hasattr(x, "item") else x
                )

        df.to_csv(
            path,
            index=False,
            encoding="utf-8-sig",  # BOM для Excel
            na_rep="",
            float_format="%.6f",
        )
        return path


class ParquetExporter(BIExporter):
    """Экспорт в Parquet (Apache Arrow)."""

    @property
    def format_name(self) -> str:
        return "parquet"

    @property
    def file_extension(self) -> str:
        return ".parquet"

    def export(self, df: pd.DataFrame, path: Path, config: BIExportConfig) -> Path:
        if not HAS_ARROW:
            raise RuntimeError("pyarrow не установлен. pip install pyarrow")

        path.parent.mkdir(parents=True, exist_ok=True)

        # Конвертируем в Arrow Table
        table = pa.Table.from_pandas(df)

        # Настройка сжатия
        compression = config.compression if config.compression != "none" else None

        pq.write_table(
            table,
            path,
            compression=compression,
            use_dictionary=True,
            write_statistics=True,
        )
        return path


class FeatherExporter(BIExporter):
    """Экспорт в Feather (Apache Arrow IPC) — быстрый бинарный формат."""

    @property
    def format_name(self) -> str:
        return "feather"

    @property
    def file_extension(self) -> str:
        return ".feather"

    def export(self, df: pd.DataFrame, path: Path, config: BIExportConfig) -> Path:
        if not HAS_ARROW:
            raise RuntimeError("pyarrow не установлен. pip install pyarrow")

        path.parent.mkdir(parents=True, exist_ok=True)

        compression = config.compression if config.compression != "none" else "uncompressed"
        feather.write_feather(df, path, compression=compression)
        return path


class XLSXExporter(BIExporter):
    """Экспорт в XLSX (для PowerBI/Excel)."""

    @property
    def format_name(self) -> str:
        return "xlsx"

    @property
    def file_extension(self) -> str:
        return ".xlsx"

    def export(self, df: pd.DataFrame, path: Path, config: BIExportConfig) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)

        with pd.ExcelWriter(path, engine="openpyxl") as writer:
            df.to_excel(writer, sheet_name="Data", index=False)

            # Настройка форматирования
            ws = writer.sheets["Data"]
            for i, col in enumerate(df.columns, 1):
                # Ширина колонки
                max_len = max(
                    len(str(col)),
                    df[col].astype(str).str.len().max() if len(df) > 0 else 0
                )
                ws.column_dimensions[chr(64 + i) if i <= 26 else f"A{chr(64 + i - 26)}"].width = min(max_len + 4, 50)

        return path


class JSONExporter(BIExporter):
    """Экспорт в JSON Lines (для Metabase/Superset API)."""

    @property
    def format_name(self) -> str:
        return "jsonl"

    @property
    def file_extension(self) -> str:
        return ".jsonl"

    def export(self, df: pd.DataFrame, path: Path, config: BIExportConfig) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)

        def default_serializer(obj):
            if isinstance(obj, (np.integer, np.floating)):
                return float(obj)
            if isinstance(obj, np.bool_):
                return bool(obj)
            if isinstance(obj, (date, datetime)):
                return obj.isoformat()
            if isinstance(obj, np.ndarray):
                return obj.tolist()
            raise TypeError(f"Type {type(obj)} not serializable")

        with open(path, "w", encoding="utf-8") as f:
            for _, row in df.iterrows():
                record = row.to_dict()
                json.dump(record, f, ensure_ascii=False, default=default_serializer)
                f.write("\n")

        return path


# ============================================================
# DWH Коннекторы
# ============================================================

class ClickHouseConnector(DWHConnector):
    """Коннектор к ClickHouse через clickhouse-connect или SQLAlchemy."""

    def __init__(self, config: ClickHouseConfig):
        self.config = config
        self._engine: Any = None
        self._client: Any = None

    @property
    def dialect_name(self) -> str:
        return "clickhouse"

    def connect(self):
        """Подключение к ClickHouse."""
        try:
            # Пробуем clickhouse-connect (нативный, быстрый)
            import clickhouse_connect
            self._client = clickhouse_connect.get_client(
                host=self.config.host,
                port=self.config.port,
                username=self.config.user,
                password=self.config.password,
                database=self.config.database,
                compress=self.config.compress,
            )
            return self._client
        except ImportError:
            # Fallback на SQLAlchemy
            if not HAS_SQLALCHEMY:
                raise RuntimeError("Установите clickhouse-connect или sqlalchemy+clickhouse-sqlalchemy")

            url = sa.engine.URL.create(
                "clickhouse+http" if self.config.use_http else "clickhouse",
                username=self.config.user,
                password=self.config.password,
                host=self.config.host,
                port=self.config.port,
                database=self.config.database,
            )
            self._engine = sa.create_engine(url, pool_pre_ping=True)
            return self._engine

    def write_table(
        self,
        df: pd.DataFrame,
        table_name: str,
        if_exists: str = "append",
        dtype: dict | None = None,
    ) -> int:
        if self._client is not None:
            # clickhouse-connect
            self._client.insert_df(
                f"{self.config.schema}.{table_name}",
                df,
                settings={"use_numpy": True},
            )
            return len(df)
        else:
            # SQLAlchemy
            df.to_sql(
                table_name,
                self._engine,
                schema=self.config.schema,
                if_exists=if_exists,
                index=False,
                dtype=dtype,
                method="multi",
                chunksize=10000,
            )
            return len(df)

    def execute(self, query: str) -> Any:
        if self._client is not None:
            return self._client.query(query)
        else:
            with self._engine.connect() as conn:
                return conn.execute(sa.text(query))

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
        if self._engine is not None:
            self._engine.dispose()


class PostgreSQLConnector(DWHConnector):
    """Коннектор к PostgreSQL через SQLAlchemy."""

    def __init__(self, config: PostgreSQLConfig):
        self.config = config
        self._engine: Any = None

    @property
    def dialect_name(self) -> str:
        return "postgresql"

    def connect(self):
        if not HAS_SQLALCHEMY:
            raise RuntimeError("Установите sqlalchemy и psycopg: pip install sqlalchemy psycopg[binary]")

        url = sa.engine.URL.create(
            "postgresql+psycopg",
            username=self.config.user,
            password=self.config.password,
            host=self.config.host,
            port=self.config.port,
            database=self.config.database,
            query={
                "sslmode": self.config.sslmode,
                "application_name": self.config.application_name,
            },
        )
        self._engine = sa.create_engine(
            url,
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=10,
        )
        # Создаём схему
        with self._engine.connect() as conn:
            conn.execute(sa.text(f"CREATE SCHEMA IF NOT EXISTS {self.config.schema}"))
            conn.commit()
        return self._engine

    def create_schema_if_not_exists(self, schema: str) -> None:
        with self._engine.connect() as conn:
            conn.execute(sa.text(f"CREATE SCHEMA IF NOT EXISTS {schema}"))
            conn.commit()

    def write_table(
        self,
        df: pd.DataFrame,
        table_name: str,
        if_exists: str = "append",
        dtype: dict | None = None,
    ) -> int:
        if self._engine is None:
            self.connect()

        df.to_sql(
            table_name,
            self._engine,
            schema=self.config.schema,
            if_exists=if_exists,
            index=False,
            dtype=dtype,
            method="multi",
            chunksize=5000,
        )
        return len(df)

    def execute(self, query: str) -> Any:
        if self._engine is None:
            self.connect()
        with self._engine.connect() as conn:
            return conn.execute(sa.text(query))

    def close(self) -> None:
        if self._engine is not None:
            self._engine.dispose()


class BigQueryConnector(DWHConnector):
    """Коннектор к Google BigQuery."""

    def __init__(self, config: BigQueryConfig):
        self.config = config
        self._client: Any = None

    @property
    def dialect_name(self) -> str:
        return "bigquery"

    def connect(self):
        if not HAS_BIGQUERY:
            raise RuntimeError("Установите google-cloud-bigquery: pip install google-cloud-bigquery")

        if self.config.credentials_path:
            self._client = bigquery.Client.from_service_account_json(
                self.config.credentials_path,
                project=self.config.project_id,
                location=self.config.location,
            )
        else:
            self._client = bigquery.Client(
                project=self.config.project_id,
                location=self.config.location,
            )

        # Создаём датасет если не существует
        dataset_ref = self._client.dataset(self.config.dataset_id)
        try:
            self._client.get_dataset(dataset_ref)
        except Exception:
            dataset = bigquery.Dataset(dataset_ref)
            dataset.location = self.config.location
            self._client.create_dataset(dataset, exists_ok=True)

        return self._client

    def write_table(
        self,
        df: pd.DataFrame,
        table_name: str,
        if_exists: str = "append",
        dtype: dict | None = None,
    ) -> int:
        if self._client is None:
            self.connect()

        table_id = f"{self.config.project_id}.{self.config.dataset_id}.{table_name}"

        job_config = bigquery.LoadJobConfig(
            write_disposition={
                "replace": bigquery.WriteDisposition.WRITE_TRUNCATE,
                "append": bigquery.WriteDisposition.WRITE_APPEND,
                "fail": bigquery.WriteDisposition.WRITE_EMPTY,
            }.get(if_exists, bigquery.WriteDisposition.WRITE_APPEND),
            autodetect=dtype is None,
        )

        if dtype:
            job_config.schema = [
                bigquery.SchemaField(name, self._map_dtype(dt))
                for name, dt in dtype.items()
            ]

        job = self._client.load_table_from_dataframe(df, table_id, job_config=job_config)
        job.result()  # Ждём завершения

        return len(df)

    def _map_dtype(self, dtype) -> str:
        """Маппинг pandas dtype -> BigQuery type."""
        dtype_str = str(dtype)
        if "int" in dtype_str:
            return "INT64"
        elif "float" in dtype_str:
            return "FLOAT64"
        elif "bool" in dtype_str:
            return "BOOL"
        elif "datetime" in dtype_str or "date" in dtype_str:
            return "TIMESTAMP"
        else:
            return "STRING"

    def execute(self, query: str) -> Any:
        if self._client is None:
            self.connect()
        return self._client.query(query).result()

    def close(self) -> None:
        if self._client is not None:
            self._client.close()


# ============================================================
# Реестр экспортеров и коннекторов
# ============================================================

class BIRegistry:
    """Реестр доступных экспортеров и коннекторов."""

    def __init__(self):
        self._exporters: dict[str, BIExporter] = {}
        self._connectors: dict[str, type[DWHConnector]] = {}
        self._register_builtins()

    def _register_builtins(self):
        # Файловые экспортеры
        self.register_exporter(CSVExporter())
        self.register_exporter(ParquetExporter())
        self.register_exporter(FeatherExporter())
        self.register_exporter(XLSXExporter())
        self.register_exporter(JSONExporter())

        # DWH коннекторы
        self.register_connector("clickhouse", ClickHouseConnector)
        self.register_connector("postgresql", PostgreSQLConnector)
        self.register_connector("bigquery", BigQueryConnector)

    def register_exporter(self, exporter: BIExporter) -> None:
        self._exporters[exporter.format_name] = exporter

    def register_connector(self, name: str, connector_class: type[DWHConnector]) -> None:
        self._connectors[name] = connector_class

    def get_exporter(self, format_name: str) -> BIExporter:
        if format_name not in self._exporters:
            raise ValueError(f"Неизвестный формат экспорта: {format_name}. Доступны: {list(self._exporters.keys())}")
        return self._exporters[format_name]

    def get_connector(self, name: str, config: DWHConnectionConfig) -> DWHConnector:
        if name not in self._connectors:
            raise ValueError(f"Неизвестный коннектор: {name}. Доступны: {list(self._connectors.keys())}")
        connector_class: type[DWHConnector] = self._connectors[name]
        return connector_class(config)

    @property
    def available_formats(self) -> list[str]:
        return list(self._exporters.keys())

    @property
    def available_connectors(self) -> list[str]:
        return list(self._connectors.keys())


# Глобальный реестр
REGISTRY = BIRegistry()


# ============================================================
# Основные функции экспорта
# ============================================================

def export_to_files(
    tables: dict[str, pd.DataFrame],
    config: BIExportConfig | None = None,
    formats: list[str] | None = None,
) -> dict[str, Path]:
    """Экспорт таблиц в файлы указанных форматов.

    Args:
        tables: Словарь {table_name: DataFrame}
        config: Конфигурация экспорта
        formats: Список форматов (если None, берётся из config)

    Returns:
        Словарь {format_table: Path}
    """
    if config is None:
        config = BIExportConfig()

    if formats is None:
        formats = config.file_formats

    # Очистка выходной директории
    if config.clean_output_dir and config.output_dir.exists():
        import shutil
        shutil.rmtree(config.output_dir)

    exported = {}

    for table_name, df in tables.items():
        if df is None or df.empty:
            continue

        for fmt in formats:
            exporter = REGISTRY.get_exporter(fmt)
            file_path = exporter._prepare_path(
                config.output_dir,
                f"{config.file_prefix}_{table_name}",
                config,
            )
            exported_path = exporter.export(df, file_path, config)
            exported[f"{fmt}_{table_name}"] = exported_path

    return exported


def export_to_dwh(
    tables: dict[str, pd.DataFrame],
    connector_name: str,
    connector_config: DWHConnectionConfig,
    if_exists: str = "append",
    schema_overrides: dict[str, dict] | None = None,
) -> dict[str, int]:
    """Экспорт таблиц в DWH.

    Args:
        tables: Словарь {table_name: DataFrame}
        connector_name: Имя коннектора (clickhouse, postgresql, bigquery)
        connector_config: Конфигурация подключения
        if_exists: 'fail', 'replace', 'append'
        schema_overrides: Опциональные переопределения типов колонок

    Returns:
        Словарь {table_name: rows_written}
    """
    connector = REGISTRY.get_connector(connector_name, connector_config)
    connector.connect()

    try:
        results = {}
        for table_name, df in tables.items():
            if df is None or df.empty:
                results[table_name] = 0
                continue

            dtype = schema_overrides.get(table_name) if schema_overrides else None
            rows = connector.write_table(df, table_name, if_exists=if_exists, dtype=dtype)
            results[table_name] = rows

        return results
    finally:
        connector.close()


def export_metrics_to_bi(
    metrics_payload: dict,
    config: BIExportConfig | None = None,
) -> dict[str, Path]:
    """Экспорт metrics.json в плоские таблицы для BI.

    Преобразует вложенную структуру metrics_payload в набор DataFrame,
    пригодных для загрузки в BI-системы.
    """
    if config is None:
        config = BIExportConfig()

    sections = metrics_payload.get("sections", {})
    tables = {}

    # 1. Ключевые показатели (headline)
    headline = metrics_payload.get("headline", [])
    if headline:
        tables["headline"] = pd.DataFrame(headline)

    # 2. Качество данных
    quality = metrics_payload.get("quality", {})
    if quality.get("checks"):
        tables["quality_checks"] = pd.DataFrame(quality["checks"])

    # 3. Секции метрик
    for section_name, section_data in sections.items():
        if isinstance(section_data, dict):
            # rows
            if "rows" in section_data and section_data["rows"]:
                df = pd.DataFrame(section_data["rows"])
                df["_section"] = section_name
                tables[f"section_{section_name}"] = df

            # summary
            if "summary" in section_data:
                df = pd.DataFrame([section_data["summary"]])
                df["_section"] = section_name
                tables[f"summary_{section_name}"] = df

            # totals
            if "totals" in section_data:
                df = pd.DataFrame([section_data["totals"]])
                df["_section"] = section_name
                tables[f"totals_{section_name}"] = df

            # profiles (сегментация)
            if "profiles" in section_data:
                df = pd.DataFrame(section_data["profiles"])
                df["_section"] = section_name
                tables[f"profiles_{section_name}"] = df

            # curves (когорты)
            if "curves" in section_data:
                df = pd.DataFrame(section_data["curves"])
                df["_section"] = section_name
                tables[f"curves_{section_name}"] = df

            # details (сценарии)
            if "details" in section_data:
                df = pd.DataFrame(section_data["details"])
                df["_section"] = section_name
                tables[f"scenarios_{section_name}"] = df

            # summary (сводка сценариев)
            if "summary" in section_data and isinstance(section_data["summary"], dict):
                rows = []
                for scenario, diffs in section_data["summary"].items():
                    for metric, value in diffs.items():
                        rows.append({
                            "scenario": scenario,
                            "metric": metric,
                            "value": value,
                        })
                if rows:
                    df = pd.DataFrame(rows)
                    df["_section"] = section_name
                    tables[f"scenario_summary_{section_name}"] = df

    # 4. Допущения
    assumptions = metrics_payload.get("assumptions", [])
    if assumptions:
        tables["assumptions"] = pd.DataFrame(assumptions)

    # 5. Предупреждения
    warnings = metrics_payload.get("warnings", [])
    if warnings:
        tables["warnings"] = pd.DataFrame({"warning": warnings})

    # 6. Мета
    meta = metrics_payload.get("meta", {})
    if meta:
        tables["meta"] = pd.DataFrame([meta])

    # Добавляем дату экспорта ко всем таблицам
    export_date = date.today()
    for name, df in tables.items():
        df["_export_date"] = export_date
        df["_source"] = "financial_analyst"

    return export_to_files(tables, config)


def push_to_bi(
    metrics_payload: dict,
    connector_name: str,
    connector_config: DWHConnectionConfig,
    if_exists: str = "append",
) -> dict[str, int]:
    """Полный цикл: metrics.json -> плоские таблицы -> DWH.

    Удобная обёртка для ETL-пайплайнов.
    """
    tables = {}

    # Используем ту же логику что export_metrics_to_bi
    sections = metrics_payload.get("sections", {})

    # Собираем все таблицы
    for section_name, section_data in sections.items():
        if isinstance(section_data, dict) and "rows" in section_data and section_data["rows"]:
            df = pd.DataFrame(section_data["rows"])
            df["_section"] = section_name
            df["_export_date"] = date.today()
            tables[f"{connector_config.schema}_{section_name}"] = df

    # Headline
    headline = metrics_payload.get("headline", [])
    if headline:
        df = pd.DataFrame(headline)
        df["_export_date"] = date.today()
        tables[f"{connector_config.schema}_headline"] = df

    return export_to_dwh(tables, connector_name, connector_config, if_exists)


# ============================================================
# Утилиты для BI-инструментов
# ============================================================

def generate_powerbi_template(
    tables: dict[str, pd.DataFrame],
    output_path: Path,
    report_name: str = "Financial Analyst Report",
) -> Path:
    """Генерация PBIX шаблона для PowerBI (через JSON метаданные).

    PowerBI не поддерживает программное создание PBIX напрямую.
    Создаём JSON с описанием модели данных, который можно импортировать.
    """
    model: dict[str, Any] = {
        "name": report_name,
        "tables": [],
        "relationships": [],
    }

    for table_name, df in tables.items():
        if df.empty:
            continue

        columns = []
        for col in df.columns:
            dtype = str(df[col].dtype)
            if "int" in dtype:
                data_type = "Int64"
            elif "float" in dtype:
                data_type = "Double"
            elif "bool" in dtype:
                data_type = "Boolean"
            elif "datetime" in dtype:
                data_type = "DateTime"
            else:
                data_type = "String"

            columns.append({
                "name": col,
                "dataType": data_type,
                "isHidden": col.startswith("_"),
            })

        model["tables"].append({
            "name": table_name,
            "columns": columns,
            "partitions": [{"name": table_name, "source": {"type": "csv", "path": f"{table_name}.csv"}}],
        })

    # Простые связи по _section
    section_tables = [t for t in model["tables"] if any(c["name"] == "_section" for c in t["columns"])]
    if len(section_tables) > 1:
        for i, t in enumerate(section_tables[1:], 1):
            model["relationships"].append({
                "fromTable": section_tables[0]["name"],
                "fromColumn": "_section",
                "toTable": t["name"],
                "toColumn": "_section",
                "crossFilteringBehavior": "bothDirections",
            })

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(model, f, ensure_ascii=False, indent=2)

    return output_path


def generate_tableau_hyper(
    tables: dict[str, pd.DataFrame],
    output_path: Path,
) -> Path:
    """Генерация .hyper файла для Tableau (требует tableauhyperapi)."""
    try:
        from tableauhyperapi import (
            Connection,
            CreateMode,
            HyperProcess,
            Inserter,
            SqlType,
            TableDefinition,
            Telemetry,
        )
    except ImportError:
        raise RuntimeError("Установите tableauhyperapi: pip install tableauhyperapi")

    output_path.parent.mkdir(parents=True, exist_ok=True)

    with HyperProcess(Telemetry.DO_NOT_SEND_USAGE_DATA_TO_TABLEAU) as hyper:
        with Connection(hyper.endpoint, output_path, CreateMode.CREATE_AND_REPLACE) as connection:
            connection.catalog.create_schema("Extract")

            for table_name, df in tables.items():
                if df.empty:
                    continue

                # Определение колонок
                cols = []
                for col in df.columns:
                    dtype = str(df[col].dtype)
                    if "int" in dtype:
                        sql_type = SqlType.big_int()
                    elif "float" in dtype:
                        sql_type = SqlType.double()
                    elif "bool" in dtype:
                        sql_type = SqlType.bool()
                    elif "datetime" in dtype:
                        sql_type = SqlType.timestamp()
                    else:
                        sql_type = SqlType.text()

                    cols.append(TableDefinition.Column(col, sql_type))

                table_def = TableDefinition(
                    table_name=table_name,
                    columns=cols,
                )
                connection.catalog.create_table(table_def)

                with Inserter(connection, table_def) as inserter:
                    for _, row in df.iterrows():
                        inserter.add_row([row[col] for col in df.columns])
                    inserter.execute()

    return output_path


# ============================================================
# CLI удобные функции
# ============================================================

def run_bi_export(
    metrics_payload: dict,
    output_dir: Path | str = "./bi_export",
    formats: list[str] | None = None,
    dwh: dict | None = None,
) -> dict:
    """Высокоуровневая функция для запуска экспорта в BI из CLI/пайплайна.

    Args:
        metrics_payload: Результат пайплайна (metrics.json)
        output_dir: Директория для файловых экспортов
        formats: Список файловых форматов
        dwh: Конфигурация DWH подключения (опционально)
            {
                "connector": "clickhouse|postgresql|bigquery",
                "config": {...},
                "if_exists": "append|replace",
            }

    Returns:
        Словарь с результатами экспорта
    """
    config = BIExportConfig(
        output_dir=Path(output_dir),
        file_formats=formats or ["parquet", "csv"],
    )

    results: dict[str, dict] = {"files": {}, "dwh": {}}

    # Файловый экспорт
    results["files"] = export_metrics_to_bi(metrics_payload, config)

    # DWH экспорт
    if dwh:
        connector_name = dwh.get("connector")
        connector_config = dwh.get("config")
        if_exists = dwh.get("if_exists", "append")

        if connector_name and connector_config:
            results["dwh"] = push_to_bi(
                metrics_payload,
                connector_name,
                connector_config,
                if_exists,
            )

    return results


if __name__ == "__main__":
    # Тестовый запуск
    import sys
    sys.path.insert(0, str(Path(__file__).parent))


    from .core import load_tables

    sample_dir = Path(__file__).parent.parent / "assets" / "sample"
    tables = load_tables(sample_dir)

    # Создаём тестовый metrics_payload
    test_payload = {
        "headline": [
            {"label": "Total Balance", "value": "16.97M", "raw": 16969410, "unit": "руб", "source": "portfolio"},
        ],
        "sections": {
            "portfolio": {
                "rows": [
                    {"metric": "total_balance", "value": 16969410},
                    {"metric": "par_30_pct", "value": 2.5},
                ],
                "summary": {"total_accounts": 1000},
            },
            "finance": {
                "rows": [
                    {"metric": "nim_pct", "value": 4.2},
                    {"metric": "net_profit", "value": 15000000},
                ],
            },
        },
        "quality": {
            "checks": [
                {"check_id": "portfolio_pk_unique", "status": "pass", "detail": ""},
            ]
        },
    }

    config = BIExportConfig(
        output_dir=Path("./bi_test"),
        file_formats=["parquet", "csv", "xlsx"],
    )

    print("Экспорт в файлы...")
    files = export_metrics_to_bi(test_payload, config)
    for k, v in files.items():
        print(f"  {k}: {v}")

    print("\nГенерация PowerBI шаблона...")
    # Загружаем созданные таблицы обратно для теста
    test_tables = {}
    for k, v in files.items():
        if k.startswith("parquet_"):
            table_name = k.replace("parquet_", "")
            test_tables[table_name] = pd.read_parquet(v)

    generate_powerbi_template(test_tables, Path("./bi_test/model.json"))
    print("  model.json создан")
