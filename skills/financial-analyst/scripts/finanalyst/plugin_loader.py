"""Загрузчик плагинов: обнаружение, импорт, валидация и выполнение."""

from __future__ import annotations

import importlib.util
import logging
import sys
import threading
from pathlib import Path
from types import ModuleType
from typing import Any, Callable

import pandas as pd

from .plugins import PLUGIN_REGISTRY, PluginContext, PluginMeta, validate_plugin

logger = logging.getLogger(__name__)


class PluginLoadError(Exception):
    """Ошибка загрузки плагина."""
    pass


class PluginTimeoutError(Exception):
    """Таймаут выполнения плагина."""
    pass


def discover_plugins(dirs: list[Path]) -> list[Path]:
    """Рекурсивно найти все *.py файлы в указанных директориях, исключая __pycache__."""
    plugin_files: list[Path] = []
    for dir_path in dirs:
        if not dir_path.exists():
            logger.debug("Plugins directory does not exist: %s", dir_path)
            continue
        if not dir_path.is_dir():
            logger.warning("Plugins path is not a directory: %s", dir_path)
            continue
        for py_file in dir_path.rglob("*.py"):
            if "__pycache__" in py_file.parts:
                continue
            if py_file.name.startswith("_"):
                continue
            plugin_files.append(py_file)
    return plugin_files


def load_plugin(file_path: Path) -> PluginMeta | None:
    """Импортировать плагин из файла и вернуть его метаданные.

    Возвращает None, если плагин невалиден или не зарегистрировался.
    Ошибки логируются, но не выбрасываются (чтобы не ломать пайплайн).
    """
    module_name = f"finanalyst_plugin_{file_path.stem}_{id(file_path)}"

    try:
        spec = importlib.util.spec_from_file_location(module_name, file_path)
        if spec is None or spec.loader is None:
            raise PluginLoadError(f"Cannot create spec for {file_path}")

        module: ModuleType = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)

        # Плагин должен был зарегистрироваться через @register_plugin
        # Ищем в реестре плагины, загруженные из этого файла
        loaded_plugins = [
            meta for name, meta in PLUGIN_REGISTRY.items()
            if getattr(meta.get("metrics", [{}])[0].get("compute"), "__module__", "") == module_name
        ]

        if not loaded_plugins:
            logger.warning("Plugin file %s did not register any plugins via @register_plugin", file_path)
            return None

        # Валидируем каждый плагин
        for meta in loaded_plugins:
            errors = validate_plugin(meta)
            if errors:
                logger.error("Plugin %s validation failed: %s", meta["name"], "; ".join(errors))
                return None

        # Возвращаем первый (обычно один плагин на файл)
        return loaded_plugins[0]

    except Exception as e:
        logger.error("Failed to load plugin from %s: %s", file_path, e)
        return None
    finally:
        # Удаляем модуль из sys.modules, чтобы не загрязнять пространство имён
        sys.modules.pop(module_name, None)


def validate_plugin_compute(meta: PluginMeta) -> list[str]:
    """Дополнительная валидация сигнатуры compute функции."""
    errors: list[str] = []
    for metric in meta.get("metrics", []):
        compute = metric.get("compute")
        if compute is None:
            errors.append(f"Metric {metric.get('name')}: missing compute function")
            continue

        import inspect
        sig = inspect.signature(compute)
        params = list(sig.parameters.keys())

        if len(params) != 2:
            errors.append(f"Metric {metric.get('name')}: compute must accept exactly 2 arguments (tables, ctx), got {len(params)}")
            continue

        if params[0] != "tables" or params[1] != "ctx":
            errors.append(f"Metric {metric.get('name')}: compute parameters must be named 'tables' and 'ctx', got {params}")

        # Проверяем аннотацию возвращаемого типа
        return_annotation = sig.return_annotation
        if return_annotation != inspect.Signature.empty:
            # Принимаем pd.DataFrame, dict или Union
            pass  # Не строго проверяем, так как typing может быть сложным

    return errors


def run_plugin_with_timeout(
    compute_fn: Callable,
    tables: dict[str, pd.DataFrame],
    ctx: PluginContext,
    timeout_sec: float,
) -> pd.DataFrame | dict:
    """Выполнить compute функцию плагина с таймаутом.

    Использует threading для ограничения времени выполнения.
    """
    result: dict[str, Any] = {"value": None, "error": None}
    done = threading.Event()

    def target():
        try:
            result["value"] = compute_fn(tables, ctx)
        except Exception as e:
            result["error"] = e
        finally:
            done.set()

    thread = threading.Thread(target=target, daemon=True)
    thread.start()

    if not done.wait(timeout=timeout_sec):
        # Таймаут — не можем убить поток в Python, но помечаем ошибку
        # и возвращаем управление. Поток продолжит работать в фоне.
        logger.error("Plugin compute timed out after %ss", timeout_sec)
        raise PluginTimeoutError(f"Plugin compute timed out after {timeout_sec}s")

    if result["error"]:
        raise result["error"]

    return result["value"]


def check_required_tables(tables: dict[str, pd.DataFrame], required: list[str]) -> list[str]:
    """Проверить наличие обязательных таблиц. Возвращает список отсутствующих."""
    missing = [t for t in required if t not in tables]
    return missing


def check_required_columns(tables: dict[str, pd.DataFrame], required: dict[str, list[str]]) -> list[str]:
    """Проверить наличие обязательных колонок. Возвращает список ошибок."""
    errors: list[str] = []
    for table_name, cols in required.items():
        df = tables.get(table_name)
        if df is None:
            errors.append(f"Table '{table_name}' not available (required for columns: {cols})")
            continue
        missing = [c for c in cols if c not in df.columns]
        if missing:
            errors.append(f"Table '{table_name}' missing columns: {missing}")
    return errors
