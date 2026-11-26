# Промт для следующей сессии: Плагинная система метрик

## Контекст
Репозиторий: `/home/anony/Documents/Личное/Проект приложения для аналитика/financial-analyst-skill/`
Ветка: `main`, чистая. Текущая версия: **2.1.8** (tag `v2.1.8` запушен).
Все тесты зелёные (238 passed), ruff, mypy, check_skill.py — чисто.
CI: зелёный (Python 3.10/3.12 + postgres интеграция).

## Что сделано в предыдущих сессиях (v2.1.8)
Реализованы 3 пункта развития среднего приоритета:
1. **Statsmodels/SARIMAX** (опциональный extra `statsmodels`)
2. **Кредитный риск PD/LGD/EAD (Basel IRB)** — модуль `credit_risk.py`, CLI `--credit-risk`
3. **Интерактивный HTML-дашборд** — `build_full_dashboard()` с 9 вкладками, фильтрами, drill-down

## Цель текущей сессии
**Пункт 54 плана развития: Плагинная система метрик**

Реализовать динамическую загрузку кастомных метрик из внешних модулей/папок, чтобы:
- Аналитики могут добавлять свои метрики без изменения ядра скилла
- Поддержка hot-reload в dev-режиме
- Изоляция плагинов (валидация входа/выхода, таймауты)
- Версионирование API плагинов

---

## Архитектура (предлагаемая)

### 1. Точка входа: `finanalyst/plugins.py`
```python
# Реестр плагинов
PLUGIN_REGISTRY: dict[str, PluginMeta] = {}

class PluginMeta(TypedDict):
    name: str
    version: str
    api_version: str  # "1.0"
    metrics: list[MetricPlugin]  # функции метрик
    hooks: dict[str, Callable]   # lifecycle hooks

class MetricPlugin(TypedDict):
    name: str                    # уникальное имя метрики
    display_name: str            # для отчётов
    description: str
    required_tables: list[str]   # какие таблицы нужны
    required_columns: dict[str, list[str]]  # таблица -> колонки
    compute: Callable[[dict[str, pd.DataFrame], PluginContext], pd.DataFrame | dict]
    output_schema: dict          # JSON Schema результата
```

### 2. Контекст плагина
```python
@dataclass
class PluginContext:
    config: PipelineConfig
    params: dict[str, Any]       # пользовательские параметры из конфига
    logger: logging.Logger
    cancel_token: threading.Event
```

### 3. Загрузчик (`finanalyst/plugin_loader.py`)
- `discover_plugins(paths: list[Path]) -> list[PluginMeta]` — сканирует директории
- `load_plugin(module_path: str) -> PluginMeta` — импортирует и валидирует
- `validate_plugin(meta: PluginMeta) -> list[ValidationError]` — проверка контракта
- Hot-reload: `watch_plugins(paths, callback)` — опционально, для dev

### 4. Интеграция в CLI (`cli.py`)
- Новый флаг: `--plugins-dir PATH` (можно многократно)
- Конфиг секция:
```yaml
plugins:
  enabled: true
  directories:
    - ./plugins
    - ~/.finanalyst/plugins
  auto_reload: false
  timeout_sec: 30
```
- В пайплайне: `_stage_plugins(p: _Pipeline)` — после загрузки данных, до расчётов

### 5. Пример плагина (`examples/plugins/custom_metrics.py`)
```python
from finanalyst.plugins import register_plugin, MetricPlugin, PluginContext

@register_plugin(
    name="my_custom_metrics",
    version="1.0.0",
    api_version="1.0",
)
def my_metrics(tables: dict[str, pd.DataFrame], ctx: PluginContext) -> pd.DataFrame:
    portfolio = tables["portfolio"]
    # ... расчёт ...
    return pd.DataFrame({"metric": ["custom_par"], "value": [par_value]})
```

---

## Задачи сессии (по порядку)

### 1. Создать `finanalyst/plugins.py` — типы и реестр
- `PluginMeta`, `MetricPlugin`, `PluginContext` (TypedDict/dataclass)
- `register_plugin()` декоратор
- `get_registry()`, `clear_registry()` для тестов
- Валидация: проверка наличия `compute`, `required_tables`, `output_schema`

### 2. Создать `finanalyst/plugin_loader.py` — загрузчик
- `discover_plugins(dirs: list[Path])` — рекурсивный поиск `*.py`, исключая `__pycache__`
- `load_plugin(file_path: Path)` — `importlib.util.spec_from_file_location`, безопасный импорт
- Валидация подписи `compute(tables, ctx) -> DataFrame|dict`
- Таймаут выполнения плагина (signal/threading)
- Логирование ошибок загрузки без падения всего пайплайна

### 3. Интеграция в конфиг (`config.py`)
- Добавить `plugins: dict[str, Any]` в `PipelineConfig`
- Парсинг секции `plugins:` из YAML
- CLI флаг `--plugins-dir` (multiple)

### 4. Этап пайплайна (`cli.py`)
- Импорт: `from .plugin_loader import discover_plugins, load_plugin`
- Функция `_stage_plugins(p: _Pipeline)`:
  - Если `plugins.enabled` и есть директории → discover + load
  - Для каждого плагина: вызвать `compute(tables, ctx)`
  - Результаты → `p.sections`, `p.metrics_payload["sections"]["plugins"]`
  - Обработка ошибок: warning в лог, продолжение работы

### 5. Документация
- `README.md`: раздел "Плагинная система", пример создания плагина
- `references/plugins.md`: спецификация API, lifecycle, best practices
- `examples/plugins/` — 2-3 примерных плагина (например: `concentration_hhi.py`, `roll_rate.py`)

### 6. Тесты (`tests/test_plugins.py`)
- Unit: реестр, декоратор, валидация
- Integration: загрузка из директории, выполнение compute, обработка ошибок
- Edge: невалидный плагин, таймаут, отсутствие таблиц, исключение в compute
- Smoke: запуск CLI с `--plugins-dir examples/plugins`

---

## Критерии готовности

1. `pytest tests/test_plugins.py -v` — все тесты проходят
2. `financial-analyst --data-dir assets/sample --out ./reports --plugins-dir examples/plugins` — работает, в отчёте появляется раздел "Кастомные метрики"
3. Невалидный плагин не ломает пайплайн (warning в лог, код возврата 0)
4. ruff, mypy, check_skill.py — чисто
5. CI зелёный

---

## Важные напоминания
- Минимальные изменения. Не добавлять лишние абстракции.
- Безопасность: плагины выполняются в том же процессе — валидировать входы, ограничивать таймауты.
- Обратная совместимость: без `--plugins-dir` поведение не меняется.
- Версию пакета (2.1.8) не менять до релиза.
- Тесты после каждого шага.

---

## Команды для старта
```bash
cd "/home/anony/Documents/Личное/Проект приложения для аналитика/financial-analyst-skill"
source .venv/bin/activate
pytest -x -q
ruff check .
mypy --ignore-missing-imports skills/financial-analyst/scripts/finanalyst
python skills/financial-analyst/scripts/check_skill.py
```