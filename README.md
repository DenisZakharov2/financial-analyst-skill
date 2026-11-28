# Financial Analyst Skill

[![ci](https://github.com/DenisZakharov2/financial-analyst-skill/actions/workflows/ci.yml/badge.svg)](https://github.com/DenisZakharov2/financial-analyst-skill/actions/workflows/ci.yml)

Скилл финансового аналитика для Hermes Agent: считает метрики по портфелю,
воронке, финансам и рыночной статистике и собирает отчёт с проверяемыми числами.

A financial-analyst skill for Hermes Agent: portfolio, funnel, unit economics,
market statistics, anomaly detection and trend forecasting with reproducible reports.

**Ключевой принцип:** числа считает скрипт, выводы пишет агент. Каждое число
в отчёте трассируется до `metrics.json`, выдуманных значений нет.

---

## Что умеет

| Блок | Метрики |
|---|---|
| Портфель | объём, счета, клиенты, средние значения, PAR 30/60/90, доля топ-10 клиентов |
| Воронка | кумулятивная конверсия по этапам, потери, сквозная конверсия, **TAT (p50/p75/p90/p95) по смежным переходам, SLA, зависший бэклог, выживаемость Каплана-Мейера** |
| Финансы | выручка, прямые расходы, валовая маржа, contribution, NIM |
| Юнит-экономика | CAC, ARPU, LTV gross/net, LTV/CAC, окупаемость CAC |
| Прогноз | линейный тренд с бэктестом; **каскад Linear / Damped ETS / ARX** с выбором по MAE (RMSE, sMAPE и MASE считаются, но модель по ним не выбирается) |
| Рыночная статистика | структура, динамика, **сезонные индексы (аддитивная/мультипликативная декомпозиция)**, аномалии (робастный Z), корреляции с перестановочным p-value, **лаговые связи (тест Грейнджера + FDR)** |
| Эксперименты | **A/B-тесты: проверка SRM, тест Уэлча и Манна — Уитни, CUPED, линеаризация ratio-метрик (Дельта-метод), DiD с проверкой пре-трендов** |
| Факторный анализ | **PVM-декомпозиция (объём, микс, ставка, взаимодействие), разложение сдвига PAR по Шепли** |
| Сегментация | **матрица RFM + Risk и кластеризация K-Means++ на log1p/IQR-признаках** |
| Сравнение срезов | **схема + портфельный Bridge (New / Churn / Expansion / Contraction, retention, NRR), P&L variance** |
| ПДн | **обнаружение и псевдонимизация (strict/redact/off)** |
| Отчёты | DOCX, PDF, XLSX, PPTX, RTF, HTML-дашборд (интерактивный Plotly, офлайн) |
| Streamlit UI | веб-интерфейс: загрузка данных, конфигурация, запуск, результаты, сценарии, графики, отчёты |
| Регуляторная отчётность | КоФ 101/102 (ЦБ РФ), НБКИ 135-ФЗ, Basel III Pillar 3 |
| BI-интеграция | Parquet/CSV/Feather/XLSX/JSONL, ClickHouse/PostgreSQL/BigQuery, PowerBI/Tableau |
| Большие данные | memory-efficient streaming, партиционирование по времени/продукту, параллельные чанки |

## Флаги расширенных расчётов

| Флаг | Что делает |
|---|---|
| `--forecast-model legacy\|auto\|linear\|ets\|arx\|sarimax` | Каскад моделей прогноза с выбором на бэктесте (`legacy` — линейный тренд, по умолчанию) |
| `--ab-test` | A/B-тест: SRM, тест Уэлча и Манна — Уитни, CUPED, линеаризация ratio-метрик |
| `--ab-metric`, `--ab-metric-type`, `--ab-denominator`, `--ab-pre-metric`, `--ab-group-col`, `--ab-lower-is-better` | Настройка A/B-теста. `--ab-lower-is-better` — для метрик, где меньше значит лучше (PAR, срок, расходы) |
| `--segment` | Сегментация клиентов: RFM + Risk и кластеризация (`--no-cluster` — без кластеризации) |
| `--drivers` | PVM и разложение сдвига PAR между срезами, лаговые связи макропоказателей |
| `--credit-risk` | Кредитный риск: PD/LGD/EAD, Expected Loss, RWA (Basel IRB) |
| `--scenario-list standard|all` | Сценарный анализ: what-if моделирование с пресетами (base, stress, optimistic) |
| `--scenario` | Путь к YAML файлу с пользовательскими сценариями |
| `--scenario-param PARAM=VALUE` | Переопределение параметра сценария (например, `finance.forecast_rate_shock=1.05`) |
| `--config` | Путь к YAML-конфигу с декларативной настройкой пайплайна (перезаписывается CLI-флагами) |
| `--source {files,postgres}` | Источник данных: файлы (CSV/Excel) или PostgreSQL (требует `--pg-*` флаги) |
| `--pg-host`, `--pg-port`, `--pg-database`, `--pg-user`, `--pg-password`, `--pg-schema`, `--pg-chunksize` | Параметры подключения к PostgreSQL (также через ENV: PGHOST, PGPORT, PGDATABASE, PGUSER, PGPASSWORD, PGSCHEMA, PGCHUNKSIZE) |
| `--regulatory` | Регуляторные отчёты: КоФ 101/102, НБКИ 135-ФЗ, Basel III Pillar 3 |
| `--reg-reports`, `--org-name`, `--org-bik`, `--org-inn`, `--org-okpo`, `--reporting-date`, `--reg-formats` | Настройка регуляторных отчётов: формы, реквизиты, дата, форматы (`json,xlsx`) |
| `--bi-export` | Экспорт `metrics.json` в плоские таблицы для BI |
| `--bi-formats`, `--bi-output-dir`, `--bi-compression`, `--bi-partition-by` | Настройка BI-экспорта: форматы (`parquet,csv,feather,xlsx,jsonl`), каталог, сжатие, партиции |
| `--dwh`, `--dwh-if-exists`, `--ch-*`, `--dwh-pg-*`, `--bq-*` | Загрузка метрик в ClickHouse, PostgreSQL или BigQuery |
| `--streaming` | Memory-efficient streaming-пайплайн для больших выгрузок |
| `--chunksize`, `--mem-workers`, `--mem-processes` | Размер чанка и параллелизм streaming-режима |
| `--partition-by-time`, `--partition-by-product`, `--time-freq` | Партиционирование чанков по времени (`M`/`Q`/`W`) и продукту |
| `--skip-heavy-metrics`, `--max-rows-full` | Отключение тяжёлых метрик и порог строк для полного режима |

Расширенные расчёты требуют `scipy` (входит в `requirements.txt`). Если пакет не
установлен, базовый расчёт работает как прежде, а расширенные разделы
сообщают, что модуль недоступен.

## Быстрый старт

```bash
python -m venv .venv
source .venv/bin/activate # Windows: .venv\Scripts\activate
pip install -r requirements.txt

python skills/financial-analyst/scripts/analyze.py \
  --data-dir skills/financial-analyst/assets/sample \
  --out ./reports \
  --strict
```

Вместо скрипта можно поставить пакет: `pip install -e .` добавит команду
`financial-analyst` с теми же аргументами.

Через несколько секунд в `./reports` появятся шесть файлов отчёта, `metrics.json`,
`metrics.csv` и графики. Подробнее — [`examples/quickstart`](examples/quickstart).

Требования: Python 3.10+ (проверено на 3.12), зависимости из `requirements.txt`.
Сеть не нужна: Plotly встраивается в дашборд, шрифт для PDF берётся из matplotlib.

## YAML-конфигурация (--config)

Для повторяемых запусков удобно хранить настройки в YAML-файле:

```yaml
# config.yaml
source: files
data_dir: ./data
out_dir: ./reports
output_formats: [docx, pdf, xlsx, pptx, rtf, html]
title: "Мой отчёт"

forecast_horizon: 6
forecast_model: auto
strict: true
pii_mode: redact

postgres:
  host: localhost
  port: 5432
  database: finanalyst
  user: analyst
  password: ""  # лучше задать через PGPASSWORD
  schema: public
  chunksize: 10000
```

```bash
financial-analyst --config config.yaml
```

Приоритет настроек: **CLI > YAML > переменные окружения > дефолты**.
Пример конфига: [`config.yaml.example`](config.yaml.example).

## PostgreSQL как источник данных (--source postgres)

Можно читать таблицы напрямую из PostgreSQL вместо CSV/Excel:

```bash
financial-analyst \
  --source postgres \
  --pg-host localhost \
  --pg-port 5432 \
  --pg-database finanalyst \
  --pg-user analyst \
  --pg-password "$PGPASSWORD" \
  --pg-schema public \
  --out ./reports
```

Или через переменные окружения (без флагов в командной строке):

```bash
export PGHOST=localhost
export PGPORT=5432
export PGDATABASE=finanalyst
export PGUSER=analyst
export PGPASSWORD=secret
export PGSCHEMA=public
export PGCHUNKSIZE=10000  # для streaming-режима

financial-analyst --source postgres --out ./reports
```

Для больших таблиц используйте streaming:
```bash
financial-analyst --source postgres --chunksize 5000 --pg-chunksize 5000 --out ./reports
```

> **Важно:** `macro_survey` (опросы ЦБ РФ) в БД не хранится — для макро-раздела положите `macro_survey.csv` в `data-dir` или используйте `--adapter client`.

## Плагинная система метрик (--plugins-dir)

Аналитики могут добавлять свои метрики без изменения ядра скилла.

### Быстрый старт

1. Создайте Python-файл с декоратором `@register_plugin`:

```python
# my_metrics.py
from finanalyst.plugins import PluginContext, register_plugin
import pandas as pd


@register_plugin(
    name="my_custom_metrics",
    version="1.0.0",
    api_version="1.0",
)
def my_metric(tables: dict[str, pd.DataFrame], ctx: PluginContext) -> pd.DataFrame:
    portfolio = tables["portfolio"]
    total = portfolio["balance"].sum()
    return pd.DataFrame({"metric": ["total_balance"], "value": [total]})
```

2. Запустите с флагом `--plugins-dir`:

```bash
financial-analyst \
  --data-dir ./data \
  --out ./reports \
  --plugins-dir ./my_plugins
```

Можно указать несколько директорий (последняя переопределяет при конфликте имён):

```bash
financial-analyst --plugins-dir ./plugins --plugins-dir ~/.finanalyst/plugins ...
```

### В отчёте

- В HTML/DOCX/PDF появится раздел **"Плагины: My Metric"**
- В `metrics.json` — секция `sections.plugins` с результатами

### Конфигурация в YAML

```yaml
plugins:
  enabled: true
  directories:
    - ./plugins
    - ~/.finanalyst/plugins
  timeout_sec: 30
  params:
    my_param: "value"
```

### Примеры плагинов

В репозитории: `examples/plugins/`
- `concentration_hhi.py` — HHI по типам продуктов, PAR-бакетам, клиентам
- `roll_rate.py` — Roll-rate матрица (требует `--compare-dirs`)
- `product_profitability.py` — NIM, Cost of Risk, ROA по продуктам

Подробная спецификация API — [`references/plugins.md`](references/plugins.md).

## Сценарный анализ (--scenario-list)

What-if моделирование: запуск пайплайна с разными наборами параметров для оценки влияния изменений ставок, конверсии, качества заёмщиков и других факторов.

### Быстрый старт

```bash
financial-analyst \
  --data-dir ./data \
  --out ./reports \
  --scenario-list standard
```

Встроенные пресеты (`--scenario-list standard`):
- `base` — базовый сценарий (текущие параметры)
- `stress_rate_up` — рост ставки +3%, ухудшение PD
- `stress_rate_down` — снижение ставки -3%, улучшение PD
- `stress_conversion_drop` — падение конверсии -20%, рост PAR
- `optimistic` — ставки вниз, качество вверх, конверсия выше
- `regulatory_stress` — половые LGD, повышенные RWA

### Пользовательские сценарии

Создайте YAML файл:

```yaml
# my_scenarios.yaml
scenarios:
  - name: "custom_stress"
    description: "Собственный стресс-тест"
    params:
      finance.forecast_rate_shock: 1.05
      credit_risk.pd_calibration.a: 1.3
      funnel.conversion_multiplier: 0.7
```

Запуск:

```bash
financial-analyst --data-dir ./data --out ./reports --scenario my_scenarios.yaml
```

### Переопределение параметров

```bash
financial-analyst --scenario-list standard --scenario-param finance.forecast_rate_shock=1.02
```

### В отчёте

- Раздел **"Сценарии: сравнение"** — таблица ключевых метрик по всем сценариям
- Сводка отклонений от базового сценария (абсолютные и процентные)
- В `metrics.json` секция `scenarios` с полными результатами

## Streamlit UI

Интерактивный веб-интерфейс для запуска пайплайна без командной строки:

```bash
pip install -e ".[streamlit]"
financial-analyst-streamlit
```

Страницы: загрузка данных (drag-and-drop CSV/Excel/Parquet), конфигурация
пайплайна, запуск с прогресс-баром, результаты, сравнение сценариев, графики,
скачивание отчётов (DOCX/PDF/XLSX/PPTX/RTF/HTML).

Без установки entry point:

```bash
streamlit run skills/financial-analyst/scripts/finanalyst/streamlit_app.py
```

## Регуляторные отчёты (--regulatory)

```bash
financial-analyst --data-dir ./data --out ./reports --regulatory
```

Формы:

- **101** — отчёт о финансовом положении (КоФ ЦБ РФ, Приказ 5610-У);
- **102** — отчёт о финансовых результатах;
- **135-ФЗ / НБКИ** — выгрузка сведений о заёмщиках (Приказ 4828-У);
- **Basel III Pillar 3** — капитал (CET1/Tier1/Total), RWA, LCR, NSFR, концентрация.

Выбор форм и реквизиты:

```bash
financial-analyst --regulatory \
  --reg-reports cof_101 cof_102 basel_pillar3 \
  --org-name "АО «Банк»" --org-bik 044525225 \
  --org-inn 7707083893 --org-okpo 12345678 \
  --reporting-date 2026-09-30 \
  --reg-formats json,xlsx
```

Результаты сохраняются в `reports/regulatory/`: JSON и XLSX (по листу на секцию).

## BI-интеграция (--bi-export)

```bash
financial-analyst --data-dir ./data --out ./reports \
  --bi-export --bi-formats parquet,csv,jsonl --bi-output-dir ./bi_export
```

- Файловые форматы: Parquet, CSV, Feather, XLSX, JSONL.
- Плоские таблицы: `headline`, `quality_checks`, `section_*`, `summary_*`,
  `totals_*`, `scenario_summary_*` — готовы для загрузки в BI.
- DWH: ClickHouse, PostgreSQL, BigQuery (нужны соответствующие драйверы).
- PowerBI: JSON-модель; Tableau: `.hyper` (при установленном `tableauhyperapi`).

Для больших выгрузок доступно партиционирование: `--bi-partition-by product_code`.

## Memory-efficient режим (--streaming)

Для выгрузок, которые не помещаются в память:

```bash
financial-analyst --data-dir ./data --out ./reports \
  --streaming --chunksize 100000 --mem-workers 4 \
  --partition-by-time --partition-by-product
```

- Инкрементальные агрегаторы: портфель, финансы, воронка, клиенты.
- Партиционирование по времени (`--time-freq M|Q|W`) и продукту.
- Параллельная обработка чанков: потоки по умолчанию, `--mem-processes` — процессы.
- `--skip-heavy-metrics` отключает кредитный риск, прогноз, драйверы и сценарии.
- `--max-rows-full N` — порог строк, выше которого тяжёлые метрики пропускаются.

## Установка как скилла Hermes

```bash
git clone https://github.com/DenisZakharov2/financial-analyst-skill.git ~/financial-analyst-skill
ln -s ~/financial-analyst-skill/skills/financial-analyst ~/.hermes/skills/financial-analyst
```

Скилл появится после перезапуска сессии: загрузчик читает каталог при старте.

## Структура

```
skills/financial-analyst/
├── SKILL.md                  # процесс, ограничения, проверка
├── references/               # формулы, схемы, контракты, гайды
├── scripts/
│   ├── analyze.py            # CLI
│   ├── check_skill.py        # самопроверка скилла
│   └── finanalyst/           # ядро: метрики, графики, отчёты, Streamlit, regulatory, BI, streaming
├── templates/                # бриф, постановка задачи, каркас отчёта
└── assets/
    ├── sample/               # синтетическая выгрузка для запуска
    └── fonts/                # необязательный DejaVuSans.ttf для PDF
```

## Входные данные

Имя файла = имя таблицы. Поддерживаются CSV (UTF-8 с BOM, UTF-8, cp1251 — типичная
выгрузка 1С/АБС) и Excel. Разделитель CSV определяется автоматически, файлы с
одной колонкой читаются без искажения шапки.

- Клиентские: `clients`, `products`, `portfolio`, `sales`, `funnel_events`,
  `transactions`, `finance`.
- Рыночные: `macro_deposits`, `macro_loans`, `macro_profit`, `macro_securities`,
  `macro_survey`.
- Широкие листы Excel с опросами разбирает адаптер `cbr_survey`.

Полные схемы — [`references/data-dictionary.md`](skills/financial-analyst/references/data-dictionary.md).

## Выходные файлы

| Файл | Назначение |
|---|---|
| `financial_analyst_report.docx` | отчёт с таблицами, допущениями и графиками |
| `financial_analyst_report.pdf` | то же в PDF, кириллица через DejaVu |
| `financial_analyst_report.xlsx` | числа с числовым форматом, листы по разделам |
| `financial_analyst_report.pptx` | слайды: содержание, таблицы, графики, нумерация |
| `financial_analyst_report.rtf` | текстовая версия с RTF-экранированием |
| `report.html` | интерактивный дашборд (фильтры, зум, hover) или статический отчёт |
| `metrics.json` | числа, допущения, предупреждения, контроль качества |
| `metrics.csv` | плоская таблица для сверки: разделы, ключевые показатели, юнит-экономика |

## Контроль качества

Перед расчётом выполняются проверки: уникальность ключей, связи между таблицами,
отрицательные остатки, согласованность `dpd` и бакета, дубли периодов, строки
`ALL`, неразобранные даты, несколько валют, нехватка обязательных колонок.
В режиме `--strict` провал останавливает запуск с кодом 2.

Метрики описаны контрактами: зерно, числитель, знаменатель, краевой случай.
См. [`references/metric-contracts.md`](skills/financial-analyst/references/metric-contracts.md).

## Ограничения

- Прогноз — интервал ошибки модели, а не причинная модель: даже каскад
  Linear / ETS / ARX выбирает модель по прошлому, а не по механике бизнеса.
- Корреляции и лаговые связи не означают причинность: лаговая связь — это
  прогностическое опережение, а не изолированная интервенция.
- A/B-тест считается на уровне `client_id`: клиент в обеих группах означает
  сломанную рандомизацию, и результат помечается недействительным. При нарушении
  SRM строгий запуск останавливается с кодом 2.
- Сезонные индексы требуют не менее двух полных циклов: на коротком ряду скилл
  показывает тренд и честно сообщает, что сезонность не оценивалась.
- В TAT и SLA входят только смежные переходы между этапами; переходы с
  пропущенным этапом вынесены в отдельную таблицу и не искажают норматив.
- По умолчанию `--pii-mode=strict`: прямые ПДн останавливают запуск с кодом 2, суммы не обезличиваются. Обезличивание — `--pii-mode=redact` (без `FINANALYST_PII_KEY` псевдонимы не воспроизводятся между запусками). Отключить проверку — `--pii-mode=off`.
- Один каталог на запуск. Две выгрузки сравниваются флагом `--compare-dirs BASE_DIR TARGET_DIR`.
- Не заменяет проверку финансово-юридической службой и не даёт инвестиционных
  рекомендаций.

## Тесты

```bash
pip install pytest ruff pypdf
ruff check .
pytest -q
python skills/financial-analyst/scripts/check_skill.py
```

CI гоняет то же на Python 3.10 и 3.12.

## Лицензия

MIT. См. [LICENSE](LICENSE).

## Атрибуция

- Техника интервьюирования при постановке задачи — по мотивам
  [mattpocock/skills](https://github.com/mattpocock/skills) (MIT).
- Структура выгрузок мониторинга предприятий — публичные данные Банка России.
  Пример в `assets/sample` синтетический.