# Плагинная система метрик

Версия API: **1.0** (соответствует `api_version="1.0"` в плагине)

Плагинная система позволяет аналитикам добавлять собственные метрики без изменения ядра скилла. Плагины загружаются динамически из указанных директорий при запуске CLI.

---

## Быстрый старт

### 1. Создайте файл плагина

Создайте Python-файл (например, `my_metrics.py`) в любой директории:

```python
from finanalyst.plugins import PluginContext, register_plugin
import pandas as pd


@register_plugin(
    name="my_custom_metrics",
    version="1.0.0",
    api_version="1.0",
)
def my_custom_metric(tables: dict[str, pd.DataFrame], ctx: PluginContext) -> pd.DataFrame:
    portfolio = tables["portfolio"]
    total = portfolio["balance"].sum()
    return pd.DataFrame({"metric": ["total_balance"], "value": [total]})
```

### 2. Запустите с флагом `--plugins-dir`

```bash
financial-analyst \
    --data-dir ./data \
    --out ./reports \
    --plugins-dir ./my_plugins
```

Можно указать несколько директорий:

```bash
financial-analyst \
    --data-dir ./data \
    --out ./reports \
    --plugins-dir ./plugins \
    --plugins-dir ~/.finanalyst/plugins
```

### 3. Результат в отчёте

В HTML/DOCX/PDF отчёте появится раздел **"Плагины: My Custom Metric"**, а в `metrics.json` — секция `plugins` с результатами.

---

## Спецификация API плагина

### Декоратор `@register_plugin`

```python
@register_plugin(
    name: str,           # Уникальное имя плагина (для реестра)
    version: str,        # Версия плагина (semver)
    api_version: str,    # Версия API плагинов (текущая: "1.0")
)
def compute_fn(tables: dict[str, pd.DataFrame], ctx: PluginContext) -> pd.DataFrame | dict:
    ...
```

**Параметры декоратора:**
- `name` — идентификатор плагина в реестре, должен быть уникальным
- `version` — версия плагина (например, `"1.0.0"`)
- `api_version` — версия контракта API (сейчас `"1.0"`)

### Функция `compute`

Сигнатура:
```python
def compute_fn(
    tables: dict[str, pd.DataFrame],  # Все загруженные таблицы
    ctx: PluginContext                # Контекст выполнения
) -> pd.DataFrame | dict:
```

**Возвращаемое значение:**
- `pd.DataFrame` — таблица для отчёта (колонки = метрики, строки = измерения)
- `dict` — будет преобразован в DataFrame автоматически

### Атрибуты функции (метаданные метрики)

После определения функции добавьте атрибуты для валидации и документации:

```python
compute_fn._metric_name = "my_metric"              # Уникальное имя метрики
compute_fn._display_name = "My Custom Metric"      # Для отчётов
compute_fn._description = "Описание метрики"       # Для assumption в отчёте
compute_fn._required_tables = ["portfolio"]        # Обязательные таблицы
compute_fn._required_columns = {                   # Обязательные колонки
    "portfolio": ["balance", "product_type"]
}
compute_fn._output_schema = {                      # JSON Schema результата
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "metric": {"type": "string"},
            "value": {"type": "number"},
        },
        "required": ["metric", "value"],
    },
}
```

### Класс `PluginContext`

Передаётся в `compute` как второй аргумент:

```python
@dataclass
class PluginContext:
    config: PipelineConfig      # Полная конфигурация пайплайна
    params: dict[str, Any]      # Пользовательские параметры из YAML (plugins.params)
    logger: logging.Logger      # Логгер "finanalyst.plugins"
    cancel_token: threading.Event  # Токен отмены (для будущего использования)
```

---

## Конфигурация в YAML

```yaml
plugins:
  enabled: true                    # Включить/выключить плагины
  directories:                     # Список директорий для поиска плагинов
    - ./plugins
    - ~/.finanalyst/plugins
  auto_reload: false               # Hot-reload в dev-режиме (пока не реализован)
  timeout_sec: 30                  # Таймаут выполнения одной метрики (сек)
  params:                          # Пользовательские параметры для плагинов
    my_param: "value"
```

Параметры из `params` доступны в `ctx.params` внутри плагина.

---

## Жизненный цикл плагина

1. **Обнаружение** — сканируются все `*.py` файлы в указанных директориях (рекурсивно, исключая `__pycache__` и файлы, начинающиеся с `_`)
2. **Импорт** — каждый файл импортируется через `importlib.util.spec_from_file_location`
3. **Регистрация** — декоратор `@register_plugin` кладёт метаданные в глобальный `PLUGIN_REGISTRY`
4. **Валидация** — проверяется контракт: наличие `compute`, правильность сигнатуры, обязательных полей
5. **Выполнение** — для каждой метрики плагина:
   - Проверяются `required_tables` и `required_columns`
   - Вызывается `compute(tables, ctx)` с таймаутом
   - Результат добавляется в `p.sections` и `p.metrics_payload["sections"]["plugins"]`
6. **Ошибки** — невалидный плагин или исключение в `compute` логируются как `warning`, пайплайн продолжает работу

---

## Безопасность и ограничения

- Плагины выполняются **в том же процессе** — нет изоляции памяти/CPU
- Валидируйте входные данные в `compute` (проверяйте наличие таблиц/колонок)
- Используйте `timeout_sec` в конфиге для защиты от зависаний
- Не доверяйте плагинам из непроверенных источников
- Плагины имеют доступ ко всем загруженным таблицам (включая ПДн, если не отключено)

---

## Примеры плагинов

В репозитории: `examples/plugins/`

| Плагин | Описание | Таблицы |
|--------|----------|---------|
| `concentration_hhi.py` | HHI по типам продуктов, PAR-бакетам, клиентам | `portfolio` |
| `roll_rate.py` | Roll-rate матрица (требует `--compare-dirs`) | `portfolio` |
| `product_profitability.py` | NIM, Cost of Risk, ROA по продуктам | `finance`, `portfolio` |

---

## Разработка плагина: чек-лист

- [ ] Функция называется понятно, имеет docstring
- [ ] Указаны `_required_tables` и `_required_columns`
- [ ] `compute` обрабатывает отсутствующие таблицы/колонки gracefully
- [ ] Возвращается `pd.DataFrame` с понятными названиями колонок
- [ ] `_output_schema` соответствует реальному результату
- [ ] Плагин тестирован на примере данных (`assets/sample`)
- [ ] Нет побочных эффектов (запись файлов, сетевые запросы)

---

## Отладка

Включите логирование плагинов:

```bash
FINANALYST_LOG_LEVEL=DEBUG financial-analyst --data-dir ./data --plugins-dir ./plugins
```

Логи плагинов пишутся в логгер `finanalyst.plugins`.

---

## Версионирование API

| API Version | Изменения | Совместимость |
|-------------|-----------|---------------|
| 1.0 | Базовая версия: `compute(tables, ctx)`, `PluginContext`, `@register_plugin` | — |

При ломящих изменениях API версия увеличивается (1.1, 2.0). Старые плагины продолжат работать, если не используют устаревшие возможности.