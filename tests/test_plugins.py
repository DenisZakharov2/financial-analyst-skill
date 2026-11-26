"""Тесты плагинной системы."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest
from finanalyst.plugin_loader import (
    PluginTimeoutError,
    check_required_columns,
    check_required_tables,
    discover_plugins,
    load_plugin,
    run_plugin_with_timeout,
)
from finanalyst.plugins import (
    PluginContext,
    PluginMeta,
    clear_registry,
    get_registry,
    register_plugin,
    validate_plugin,
)

# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def clean_registry():
    """Очистить реестр перед каждым тестом."""
    clear_registry()
    yield
    clear_registry()


@pytest.fixture
def sample_tables() -> dict[str, pd.DataFrame]:
    """Тестовые таблицы."""
    return {
        "portfolio": pd.DataFrame({
            "client_id": [1, 2, 3],
            "balance": [100.0, 200.0, 300.0],
            "product_type": ["A", "B", "A"],
            "par_bucket": ["0", "1-30", "31-60"],
        }),
        "clients": pd.DataFrame({
            "client_id": [1, 2, 3],
            "age": [25, 35, 45],
        }),
        "finance": pd.DataFrame({
            "period": ["2024-01", "2024-02"],
            "interest_income": [1000.0, 1100.0],
            "interest_expense": [400.0, 450.0],
            "provisions": [50.0, 60.0],
            "fee_income": [200.0, 220.0],
        }),
    }


@pytest.fixture
def plugin_context() -> PluginContext:
    """Тестовый контекст плагина."""
    import logging
    return PluginContext(
        config=None,
        params={},
        logger=logging.getLogger("test"),
        cancel_token=__import__("threading").Event(),
    )


# --------------------------------------------------------------------------
# Tests: Registry & Decorator
# --------------------------------------------------------------------------

def test_register_plugin_basic():
    """Базовая регистрация плагина через декоратор."""
    @register_plugin(name="test_plugin", version="1.0.0", api_version="1.0")
    def my_metric(tables, ctx):
        return pd.DataFrame({"a": [1]})

    my_metric._metric_name = "my_metric"
    my_metric._display_name = "My Metric"
    my_metric._description = "Test"
    my_metric._required_tables = ["portfolio"]
    my_metric._required_columns = {"portfolio": ["balance"]}
    my_metric._output_schema = {"type": "object"}

    registry = get_registry()
    assert "test_plugin" in registry
    meta = registry["test_plugin"]
    assert meta["name"] == "test_plugin"
    assert meta["version"] == "1.0.0"
    assert meta["api_version"] == "1.0"
    assert len(meta["metrics"]) == 1
    assert meta["metrics"][0]["name"] == "my_metric"


def test_register_plugin_invalid_signature():
    """Декоратор отклоняет неверную сигнатуру."""
    with pytest.raises(ValueError, match="exactly 2 arguments"):
        @register_plugin(name="bad", version="1.0.0")
        def bad_fn(tables):  # нет ctx
            return pd.DataFrame()

    with pytest.raises(ValueError, match="named 'tables' and 'ctx'"):
        @register_plugin(name="bad2", version="1.0.0")
        def bad_fn2(x, y):  # неверные имена
            return pd.DataFrame()


def test_clear_registry():
    """clear_registry очищает реестр."""
    @register_plugin(name="t", version="1.0.0")
    def f(tables, ctx):
        return pd.DataFrame()
    f._metric_name = "m"
    f._display_name = "M"
    f._description = "d"
    f._required_tables = []
    f._required_columns = {}
    f._output_schema = {}

    assert len(get_registry()) == 1
    clear_registry()
    assert len(get_registry()) == 0


def test_validate_plugin_valid():
    """Валидация проходит для корректного плагина."""
    meta: PluginMeta = {
        "name": "test",
        "version": "1.0.0",
        "api_version": "1.0",
        "metrics": [{
            "name": "m1",
            "display_name": "M1",
            "description": "desc",
            "required_tables": ["t1"],
            "required_columns": {"t1": ["c1"]},
            "compute": lambda tables, ctx: pd.DataFrame(),
            "output_schema": {"type": "object"},
        }],
        "hooks": {},
    }
    errors = validate_plugin(meta)
    assert errors == []


def test_validate_plugin_missing_fields():
    """Валидация находит отсутствующие поля."""
    meta: PluginMeta = {
        "name": "test",
        "version": "1.0.0",
        "api_version": "1.0",
        "metrics": [{}],  # пустая метрика
        "hooks": {},
    }
    errors = validate_plugin(meta)
    assert len(errors) > 0
    assert any("missing required field" in e for e in errors)


def test_validate_plugin_bad_compute():
    """Валидация находит не-callable compute."""
    meta: PluginMeta = {
        "name": "test",
        "version": "1.0.0",
        "api_version": "1.0",
        "metrics": [{
            "name": "m1",
            "display_name": "M1",
            "description": "desc",
            "required_tables": [],
            "required_columns": {},
            "compute": "not a function",
            "output_schema": {},
        }],
        "hooks": {},
    }
    errors = validate_plugin(meta)
    assert any("'compute' must be callable" in e for e in errors)


# --------------------------------------------------------------------------
# Tests: Plugin Loader
# --------------------------------------------------------------------------

def test_discover_plugins(tmp_path: Path):
    """Обнаружение файлов плагинов."""
    # Создаём структуру
    (tmp_path / "plugin1.py").write_text("# plugin")
    (tmp_path / "plugin2.py").write_text("# plugin")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "cached.py").write_text("# cached")
    (tmp_path / "_private.py").write_text("# private")
    subdir = tmp_path / "subdir"
    subdir.mkdir()
    (subdir / "plugin3.py").write_text("# plugin")

    files = discover_plugins([tmp_path])
    names = {f.name for f in files}
    assert "plugin1.py" in names
    assert "plugin2.py" in names
    assert "plugin3.py" in names
    assert "cached.py" not in names
    assert "_private.py" not in names


def test_discover_plugins_nonexistent_dir(tmp_path: Path):
    """Несуществующая директория не ломает discover."""
    files = discover_plugins([tmp_path / "nonexistent"])
    assert files == []


def test_load_plugin_valid(tmp_path: Path):
    """Загрузка валидного плагина."""
    plugin_code = '''
from finanalyst.plugins import PluginContext, register_plugin
import pandas as pd

@register_plugin(name="loaded_plugin", version="1.0.0", api_version="1.0")
def my_metric(tables: dict, ctx: PluginContext) -> pd.DataFrame:
    return pd.DataFrame({"result": [42]})

my_metric._metric_name = "my_metric"
my_metric._display_name = "My Metric"
my_metric._description = "Test metric"
my_metric._required_tables = ["portfolio"]
my_metric._required_columns = {"portfolio": ["balance"]}
my_metric._output_schema = {"type": "object"}
'''
    plugin_file = tmp_path / "valid_plugin.py"
    plugin_file.write_text(plugin_code)

    meta = load_plugin(plugin_file)
    assert meta is not None
    assert meta["name"] == "loaded_plugin"
    assert len(meta["metrics"]) == 1
    assert meta["metrics"][0]["name"] == "my_metric"


def test_load_plugin_invalid_no_register(tmp_path: Path):
    """Плагин без @register_plugin не загружается."""
    plugin_code = '''
import pandas as pd
def my_metric(tables, ctx):
    return pd.DataFrame()
'''
    plugin_file = tmp_path / "no_register.py"
    plugin_file.write_text(plugin_code)

    meta = load_plugin(plugin_file)
    assert meta is None


def test_load_plugin_invalid_signature(tmp_path: Path):
    """Плагин с неверной сигнатурой compute не загружается."""
    plugin_code = '''
from finanalyst.plugins import PluginContext, register_plugin
import pandas as pd

@register_plugin(name="bad_sig", version="1.0.0", api_version="1.0")
def my_metric(tables):  # нет ctx
    return pd.DataFrame()

my_metric._metric_name = "m"
my_metric._display_name = "M"
my_metric._description = "d"
my_metric._required_tables = []
my_metric._required_columns = {}
my_metric._output_schema = {}
'''
    plugin_file = tmp_path / "bad_sig.py"
    plugin_file.write_text(plugin_code)

    meta = load_plugin(plugin_file)
    assert meta is None


def test_run_plugin_with_timeout_success(plugin_context, sample_tables):
    """Успешное выполнение с таймаутом."""
    def compute(tables, ctx):
        return pd.DataFrame({"value": [1, 2, 3]})

    result = run_plugin_with_timeout(compute, sample_tables, plugin_context, timeout_sec=5)
    assert isinstance(result, pd.DataFrame)
    assert len(result) == 3


def test_run_plugin_with_timeout_exception(plugin_context, sample_tables):
    """Исключение в compute пробрасывается."""
    def compute(tables, ctx):
        raise ValueError("test error")

    with pytest.raises(ValueError, match="test error"):
        run_plugin_with_timeout(compute, sample_tables, plugin_context, timeout_sec=5)


def test_run_plugin_with_timeout_timeout(plugin_context, sample_tables):
    """Таймаут выбрасывает PluginTimeoutError."""
    import time
    def compute(tables, ctx):
        time.sleep(10)  # дольше таймаута

    with pytest.raises(PluginTimeoutError):
        run_plugin_with_timeout(compute, sample_tables, plugin_context, timeout_sec=0.1)


def test_check_required_tables(sample_tables):
    """Проверка обязательных таблиц."""
    missing = check_required_tables(sample_tables, ["portfolio", "clients"])
    assert missing == []

    missing = check_required_tables(sample_tables, ["portfolio", "missing_table"])
    assert missing == ["missing_table"]


def test_check_required_columns(sample_tables):
    """Проверка обязательных колонок."""
    errors = check_required_columns(sample_tables, {"portfolio": ["balance", "product_type"]})
    assert errors == []

    errors = check_required_columns(sample_tables, {"portfolio": ["balance", "missing_col"]})
    assert len(errors) == 1
    assert "missing_col" in errors[0]

    errors = check_required_columns(sample_tables, {"missing_table": ["col"]})
    assert len(errors) == 1
    assert "not available" in errors[0]


# --------------------------------------------------------------------------
# Tests: Integration (end-to-end)
# --------------------------------------------------------------------------

def test_plugin_integration_via_cli(tmp_path: Path, sample_tables):
    """Интеграционный тест: загрузка плагина через CLI (имитация)."""
    from argparse import Namespace
    from dataclasses import dataclass, field

    from finanalyst.cli import _stage_plugins
    from finanalyst.config import PipelineConfig

    # Создаём плагин
    plugin_code = '''
from finanalyst.plugins import PluginContext, register_plugin
import pandas as pd

def concentration(tables: dict, ctx: PluginContext) -> pd.DataFrame:
    portfolio = tables["portfolio"]
    total = portfolio["balance"].sum()
    return pd.DataFrame({"metric": ["total_balance"], "value": [total]})

concentration._metric_name = "concentration"
concentration._display_name = "Concentration"
concentration._description = "Total balance"
concentration._required_tables = ["portfolio"]
concentration._required_columns = {"portfolio": ["balance"]}
concentration._output_schema = {"type": "object"}

concentration = register_plugin(name="integ_plugin", version="1.0.0", api_version="1.0")(concentration)
'''
    plugin_dir = tmp_path / "plugins"
    plugin_dir.mkdir()
    (plugin_dir / "integ.py").write_text(plugin_code)

    # Создаём конфиг с плагинами
    config = PipelineConfig()
    config.plugins = {
        "enabled": True,
        "directories": [str(plugin_dir)],
        "timeout_sec": 30,
        "params": {},
    }

    # Создаём pipeline object
    @dataclass
    class MockPipeline:
        args: Namespace
        config: PipelineConfig
        tables: dict
        notes: list = field(default_factory=list)
        warnings: list = field(default_factory=list)
        sections: list = field(default_factory=list)
        metrics_payload: dict = field(default_factory=lambda: {"sections": {}})

    args = Namespace(quiet=True)
    p = MockPipeline(args=args, config=config, tables=sample_tables)

    # Вызываем этап плагинов
    _stage_plugins(p)

    # Проверяем результаты
    assert len(p.notes) > 0
    assert any("Плагины: обнаружено файлов" in n for n in p.notes)
    assert any("integ_plugin" in n for n in p.notes)
    assert len(p.sections) == 1
    assert p.sections[0][0] == "Плагины: Concentration"
    assert "plugins" in p.metrics_payload["sections"]
    assert "concentration" in p.metrics_payload["sections"]["plugins"]


def test_plugin_missing_table_warning(tmp_path: Path, sample_tables):
    """Предупреждение при отсутствии обязательной таблицы."""
    from argparse import Namespace
    from dataclasses import dataclass, field

    from finanalyst.cli import _stage_plugins
    from finanalyst.config import PipelineConfig

    plugin_code = '''
from finanalyst.plugins import PluginContext, register_plugin
import pandas as pd

def need_missing(tables: dict, ctx: PluginContext) -> pd.DataFrame:
    return pd.DataFrame()

need_missing._metric_name = "need_missing"
need_missing._display_name = "Need Missing"
need_missing._description = "Needs missing table"
need_missing._required_tables = ["nonexistent_table"]
need_missing._required_columns = {}
need_missing._output_schema = {"type": "object"}

need_missing = register_plugin(name="bad_plugin", version="1.0.0", api_version="1.0")(need_missing)
'''
    plugin_dir = tmp_path / "plugins"
    plugin_dir.mkdir()
    (plugin_dir / "bad.py").write_text(plugin_code)

    config = PipelineConfig()
    config.plugins = {
        "enabled": True,
        "directories": [str(plugin_dir)],
        "timeout_sec": 30,
    }

    @dataclass
    class MockPipeline:
        args: Namespace
        config: PipelineConfig
        tables: dict
        notes: list = field(default_factory=list)
        warnings: list = field(default_factory=list)
        sections: list = field(default_factory=list)
        metrics_payload: dict = field(default_factory=lambda: {"sections": {}})

    args = Namespace(quiet=True)
    p = MockPipeline(args=args, config=config, tables=sample_tables)

    _stage_plugins(p)

    assert any("отсутствуют таблицы" in w for w in p.warnings)
    assert len(p.sections) == 0  # секция не добавлена


def test_plugin_disabled_by_config(sample_tables):
    """Плагины отключены в конфиге — этап пропускается."""
    from argparse import Namespace
    from dataclasses import dataclass, field

    from finanalyst.cli import _stage_plugins
    from finanalyst.config import PipelineConfig

    config = PipelineConfig()
    config.plugins = {"enabled": False}

    @dataclass
    class MockPipeline:
        args: Namespace
        config: PipelineConfig
        tables: dict
        notes: list = field(default_factory=list)
        warnings: list = field(default_factory=list)
        sections: list = field(default_factory=list)
        metrics_payload: dict = field(default_factory=lambda: {"sections": {}})

    args = Namespace(quiet=True)
    p = MockPipeline(args=args, config=config, tables=sample_tables)

    _stage_plugins(p)

    assert len(p.notes) == 0
    assert len(p.sections) == 0


def test_plugin_no_directories(sample_tables):
    """Нет директорий — этап пропускается."""
    from argparse import Namespace
    from dataclasses import dataclass, field

    from finanalyst.cli import _stage_plugins
    from finanalyst.config import PipelineConfig

    config = PipelineConfig()
    config.plugins = {"enabled": True, "directories": []}

    @dataclass
    class MockPipeline:
        args: Namespace
        config: PipelineConfig
        tables: dict
        notes: list = field(default_factory=list)
        warnings: list = field(default_factory=list)
        sections: list = field(default_factory=list)
        metrics_payload: dict = field(default_factory=lambda: {"sections": {}})

    args = Namespace(quiet=True)
    p = MockPipeline(args=args, config=config, tables=sample_tables)

    _stage_plugins(p)

    assert len(p.notes) == 0


# --------------------------------------------------------------------------
# Tests: Example plugins (smoke)
# --------------------------------------------------------------------------

def test_example_concentration_hhi_loads(tmp_path: Path):
    """Примерный плагин concentration_hhi загружается."""
    # Копируем пример во временную директорию
    import shutil
    example_dir = Path("examples/plugins")
    if not example_dir.exists():
        pytest.skip("examples/plugins not found")

    plugin_dir = tmp_path / "examples"
    plugin_dir.mkdir()
    shutil.copy(example_dir / "concentration_hhi.py", plugin_dir / "concentration_hhi.py")

    meta = load_plugin(plugin_dir / "concentration_hhi.py")
    assert meta is not None
    assert meta["name"] == "concentration_hhi"
    assert len(meta["metrics"]) == 1


def test_example_product_profitability_loads(tmp_path: Path):
    """Примерный плагин product_profitability загружается."""
    import shutil
    example_dir = Path("examples/plugins")
    if not example_dir.exists():
        pytest.skip("examples/plugins not found")

    plugin_dir = tmp_path / "examples"
    plugin_dir.mkdir()
    shutil.copy(example_dir / "product_profitability.py", plugin_dir / "product_profitability.py")

    meta = load_plugin(plugin_dir / "product_profitability.py")
    assert meta is not None
    assert meta["name"] == "product_profitability"
    assert len(meta["metrics"]) == 1
