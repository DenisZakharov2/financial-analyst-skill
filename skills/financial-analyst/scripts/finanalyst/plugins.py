"""Плагинная система метрик: типы, реестр и декоратор регистрации."""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any, Callable, TypedDict

import pandas as pd


class MetricPlugin(TypedDict):
    """Описание одной метрики плагина."""

    name: str                    # уникальное имя метрики
    display_name: str            # для отчётов
    description: str
    required_tables: list[str]   # какие таблицы нужны
    required_columns: dict[str, list[str]]  # таблица -> колонки
    compute: Callable[[dict[str, pd.DataFrame], "PluginContext"], pd.DataFrame | dict]
    output_schema: dict          # JSON Schema результата


class PluginMeta(TypedDict):
    """Метаданные плагина."""

    name: str
    version: str
    api_version: str  # "1.0"
    metrics: list[MetricPlugin]
    hooks: dict[str, Callable]  # lifecycle hooks


@dataclass
class PluginContext:
    """Контекст выполнения плагина."""

    config: Any  # PipelineConfig
    params: dict[str, Any]       # пользовательские параметры из конфига
    logger: logging.Logger
    cancel_token: threading.Event


# Глобальный реестр плагинов
PLUGIN_REGISTRY: dict[str, PluginMeta] = {}


def register_plugin(
    name: str,
    version: str,
    api_version: str = "1.0",
) -> Callable[[Callable], Callable]:
    """Декоратор для регистрации плагина.

    Использование:
        @register_plugin(name="my_metrics", version="1.0.0", api_version="1.0")
        def my_metrics(tables: dict[str, pd.DataFrame], ctx: PluginContext) -> pd.DataFrame:
            ...
    """
    def decorator(compute_fn: Callable) -> Callable:
        # Валидация сигнатуры compute_fn
        import inspect
        sig = inspect.signature(compute_fn)
        params = list(sig.parameters.keys())
        if len(params) != 2:
            raise ValueError(
                f"Plugin compute function must accept exactly 2 arguments (tables, ctx), got {len(params)}"
            )
        if params[0] != "tables" or params[1] != "ctx":
            raise ValueError(
                f"Plugin compute function parameters must be named 'tables' and 'ctx', got {params}"
            )

        # Извлекаем метаданные из атрибутов функции или используем дефолты
        metric_name = getattr(compute_fn, "_metric_name", compute_fn.__name__)
        display_name = getattr(compute_fn, "_display_name", metric_name.replace("_", " ").title())
        description = getattr(compute_fn, "_description", compute_fn.__doc__ or "")
        required_tables = getattr(compute_fn, "_required_tables", [])
        required_columns = getattr(compute_fn, "_required_columns", {})
        output_schema = getattr(compute_fn, "_output_schema", {"type": "object"})

        metric: MetricPlugin = {
            "name": metric_name,
            "display_name": display_name,
            "description": description,
            "required_tables": required_tables,
            "required_columns": required_columns,
            "compute": compute_fn,
            "output_schema": output_schema,
        }

        meta: PluginMeta = {
            "name": name,
            "version": version,
            "api_version": api_version,
            "metrics": [metric],
            "hooks": {},
        }

        PLUGIN_REGISTRY[name] = meta
        return compute_fn

    return decorator


def get_registry() -> dict[str, PluginMeta]:
    """Вернуть копию реестра плагинов."""
    return PLUGIN_REGISTRY.copy()


def clear_registry() -> None:
    """Очистить реестр (для тестов)."""
    PLUGIN_REGISTRY.clear()


def validate_plugin(meta: PluginMeta) -> list[str]:
    """Проверить контракт плагина. Возвращает список ошибок (пустой = OK)."""
    errors: list[str] = []

    # Проверка обязательных полей PluginMeta
    for field in ("name", "version", "api_version", "metrics", "hooks"):
        if field not in meta:
            errors.append(f"Missing required field in PluginMeta: {field}")

    if not errors:
        if not isinstance(meta["metrics"], list):
            errors.append("'metrics' must be a list")
        elif len(meta["metrics"]) == 0:
            errors.append("'metrics' list cannot be empty")

        for i, metric in enumerate(meta["metrics"]):
            for field in ("name", "display_name", "description", "required_tables",
                          "required_columns", "compute", "output_schema"):
                if field not in metric:
                    errors.append(f"Metric {i}: missing required field '{field}'")

            # Проверка compute — callable
            if "compute" in metric and not callable(metric["compute"]):
                errors.append(f"Metric {metric.get('name', i)}: 'compute' must be callable")

            # Проверка required_tables — list[str]
            if "required_tables" in metric:
                rt = metric["required_tables"]
                if not isinstance(rt, list) or not all(isinstance(x, str) for x in rt):
                    errors.append(f"Metric {metric.get('name', i)}: 'required_tables' must be list[str]")

            # Проверка required_columns — dict[str, list[str]]
            if "required_columns" in metric:
                rc = metric["required_columns"]
                if not isinstance(rc, dict):
                    errors.append(f"Metric {metric.get('name', i)}: 'required_columns' must be dict")
                else:
                    for table, cols in rc.items():
                        if not isinstance(table, str) or not isinstance(cols, list) or not all(isinstance(c, str) for c in cols):
                            errors.append(f"Metric {metric.get('name', i)}: 'required_columns' values must be list[str]")

            # Проверка output_schema — dict
            if "output_schema" in metric and not isinstance(metric["output_schema"], dict):
                errors.append(f"Metric {metric.get('name', i)}: 'output_schema' must be dict")

    return errors
