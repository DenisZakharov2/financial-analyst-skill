"""Адаптеры источников: приводят выгрузку к таблицам словаря данных.

Lazy imports to avoid numpy double-load in pytest.
"""

import sys
from importlib import import_module
from types import ModuleType
from typing import Any


class _LazyModule(ModuleType):
    """Lazy-load submodules on first attribute access."""

    __all__: list[str] = ["cbr_survey", "client_csv", "postgres"]

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self._loaded = False
        self._modules: dict[str, ModuleType] = {}

    def __getattr__(self, name: str) -> Any:
        if name in {"cbr_survey", "client_csv", "postgres"}:
            if name not in self._modules:
                self._modules[name] = import_module(f".{name}", __name__)
            return self._modules[name]
        raise AttributeError(f"module {self.__name__!r} has no attribute {name!r}")

    def __dir__(self) -> list[str]:
        return ["cbr_survey", "client_csv", "postgres", "__all__", "__name__", "__path__", "__file__"]


# Replace this module with lazy version
_lazy = _LazyModule(__name__)
_lazy.__file__ = __file__
_lazy.__path__ = __path__
_lazy.__all__ = ["cbr_survey", "client_csv", "postgres"]
sys.modules[__name__] = _lazy
