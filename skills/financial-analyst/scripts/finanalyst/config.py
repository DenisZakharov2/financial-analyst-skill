"""YAML-конфигурация пайплайна: декларативная настройка всех параметров."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class PipelineConfig:
    """Полная конфигурация пайплайна из YAML."""

    # Источник данных
    source: str = "files"  # files | postgres
    data_dir: str | None = None  # для файлов

    # PostgreSQL
    postgres: dict[str, Any] = field(default_factory=dict)

    # Параметры прогноза
    forecast_horizon: int = 4
    forecast_band: float = 1.0
    forecast_model: str = "legacy"  # legacy | auto | linear | ets | arx

    # Параметры качества
    strict: bool = False
    pii_mode: str = "strict"  # strict | redact | off

    # Пороги качества
    quality_thresholds: dict[str, float] = field(default_factory=dict)

    # Форматы вывода
    output_formats: list[str] = field(default_factory=lambda: ["docx", "pdf", "xlsx", "pptx", "rtf", "html"])

    # Стриминг
    chunksize: int | None = None

    # A/B тест
    ab_test: dict[str, Any] = field(default_factory=dict)

    # Сегментация
    segmentation: dict[str, Any] = field(default_factory=dict)

    # Когорты
    cohort: dict[str, Any] = field(default_factory=dict)

    # Миграция
    migration: dict[str, Any] = field(default_factory=dict)

    # Винтаж
    vintage: dict[str, Any] = field(default_factory=dict)

    # Драйверы
    drivers: dict[str, Any] = field(default_factory=dict)

    # Сравнение
    comparison: dict[str, Any] = field(default_factory=dict)

    # Метаданные отчёта
    title: str = "Финансовый аналитический отчёт"

    @classmethod
    def from_file(cls, path: Path) -> PipelineConfig:
        """Загрузить конфиг из YAML файла."""
        if not path.exists():
            raise FileNotFoundError(f"Файл конфига не найден: {path}")
        with path.open(encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PipelineConfig:
        """Создать конфиг из словаря."""
        # Извлекаем известные поля, остальное игнорируем
        known_fields = {
            "source", "data_dir", "postgres",
            "forecast_horizon", "forecast_band", "forecast_model",
            "strict", "pii_mode", "quality_thresholds", "output_formats",
            "chunksize", "ab_test", "segmentation", "cohort",
            "migration", "vintage", "drivers", "comparison", "title",
        }
        filtered = {k: v for k, v in data.items() if k in known_fields}
        return cls(**filtered)

    def to_dict(self) -> dict[str, Any]:
        """Представить конфиг как словарь."""
        return {
            "source": self.source,
            "data_dir": self.data_dir,
            "postgres": self.postgres,
            "forecast_horizon": self.forecast_horizon,
            "forecast_band": self.forecast_band,
            "forecast_model": self.forecast_model,
            "strict": self.strict,
            "pii_mode": self.pii_mode,
            "quality_thresholds": self.quality_thresholds,
            "output_formats": self.output_formats,
            "chunksize": self.chunksize,
            "ab_test": self.ab_test,
            "segmentation": self.segmentation,
            "cohort": self.cohort,
            "migration": self.migration,
            "vintage": self.vintage,
            "drivers": self.drivers,
            "comparison": self.comparison,
            "title": self.title,
        }


def merge_configs(
    yaml_config: PipelineConfig | None,
    cli_args: dict[str, Any],
) -> PipelineConfig:
    """Объединить YAML-конфиг и CLI-аргументы.

    Приоритет: CLI > YAML > defaults.
    """
    # Начинаем с дефолтов или YAML
    if yaml_config:
        result = PipelineConfig(**yaml_config.to_dict())
    else:
        result = PipelineConfig()

    # CLI перезаписывает (только переданные явно, не None)
    cli_mapping = {
        "source": "source",
        "data_dir": "data_dir",
        "forecast_horizon": "forecast_horizon",
        "forecast_band": "forecast_band",
        "forecast_model": "forecast_model",
        "strict": "strict",
        "pii_mode": "pii_mode",
        "output_formats": "format",
        "chunksize": "chunksize",
        "title": "title",
    }

    for cli_key, config_key in cli_mapping.items():
        value = cli_args.get(cli_key)
        if value is not None:
            if cli_key == "output_formats":
                # format приходит как строка "docx,pdf,html"
                result.output_formats = [f.strip().lower() for f in value.split(",") if f.strip()]
            else:
                setattr(result, config_key, value)

    # PostgreSQL параметры из CLI
    pg_keys = {
        "pg_host": "host",
        "pg_port": "port",
        "pg_database": "database",
        "pg_user": "user",
        "pg_password": "password",
        "pg_schema": "schema",
        "pg_chunksize": "chunksize",
    }
    if any(cli_args.get(k) for k in pg_keys):
        if "postgres" not in result.postgres:
            result.postgres = {}
        for cli_key, pg_key in pg_keys.items():
            value = cli_args.get(cli_key)
            if value is not None:
                result.postgres[pg_key] = value

    # A/B тест параметры
    ab_keys = {
        "ab_test": "enabled",
        "ab_metric": "metric",
        "ab_metric_type": "metric_type",
        "ab_denominator": "denominator",
        "ab_pre_metric": "pre_metric",
        "ab_group_col": "group_col",
        "ab_lower_is_better": "lower_is_better",
    }
    if any(cli_args.get(k) for k in ab_keys):
        if not result.ab_test:
            result.ab_test = {}
        for cli_key, ab_key in ab_keys.items():
            value = cli_args.get(cli_key)
            if value is not None:
                result.ab_test[ab_key] = value

    # Сегментация
    if cli_args.get("segment"):
        result.segmentation["enabled"] = True
    if cli_args.get("no_cluster"):
        result.segmentation["no_cluster"] = True

    # Когорты
    if cli_args.get("cohort"):
        result.cohort["enabled"] = True

    # Миграция
    if cli_args.get("migration"):
        result.migration["enabled"] = True
    if cli_args.get("migration_mode"):
        result.migration["mode"] = cli_args["migration_mode"]

    # Винтаж
    if cli_args.get("vintage"):
        result.vintage["enabled"] = True

    # Драйверы
    if cli_args.get("drivers"):
        result.drivers["enabled"] = True
    if cli_args.get("drivers_max_lag"):
        result.drivers["max_lag"] = cli_args["drivers_max_lag"]

    # Сравнение
    if cli_args.get("compare_dirs"):
        result.comparison["enabled"] = True
        result.comparison["base_dir"] = cli_args["compare_dirs"][0]
        result.comparison["target_dir"] = cli_args["compare_dirs"][1]
    if cli_args.get("labels"):
        result.comparison["labels"] = cli_args["labels"]

    # Индикатор для макро
    if cli_args.get("indicator"):
        if not result.postgres:  # используем как общий параметр
            result.postgres = {}
        result.postgres["indicator"] = cli_args["indicator"]

    # Lifetime months
    if cli_args.get("lifetime_months"):
        # lifetime_months используется в unit_economics
        # передаем через общий словарь
        if "unit_economics" not in result.__dict__:
            result.__dict__["unit_economics"] = {}
        result.__dict__["unit_economics"]["lifetime_months"] = cli_args["lifetime_months"]

    return result
