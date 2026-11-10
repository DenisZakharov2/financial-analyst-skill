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
| Портфель | объём, счета, клиенты, средние значения, PAR 30/60/90, концентрация топ-10, сдвиг PAR по Шепли |
| Воронка | кумулятивная конверсия по этапам, потери, сквозная конверсия, **TAT (p50/p75/p90/p95) по переходам, SLA, зависший бэклог, выживаемость Каплана-Мейера** |
| Финансы | выручка, прямые расходы, валовая маржа, contribution, NIM, **факторное разложение PVM** |
| Юнит-экономика | CAC, ARPU, LTV gross/net, LTV/CAC, окупаемость CAC, **RFM + Risk сегментация** |
| Рыночная статистика | структура, динамика, **сезонность (STL-аддитивная/мультипликативная)**, аномалии (робастный Z), корреляции, **Грейнджер**, прогноз |
| Эксперименты | **A/B-тесты (t-test, перестановочный p-value, FDR), DiD** |
| Сравнение срезов | **схема + портфельный Bridge (Shapley), P&L variance, макровыгрузки** |
| PII | **обнаружение/псевдонимизация (strict/redact/off)** |
| Отчёты | DOCX, PDF, XLSX, PPTX, RTF, HTML-дашборд (интерактивный Plotly) |

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

## Установка как скилла Hermes

```bash
git clone https://github.com/DenisZakharov2/financial-analyst-skill.git ~/financial-analyst-skill
ln -s ~/financial-analyst-skill/skills/financial-analyst ~/.hermes/skills/financial-analyst
```

Скилл появится после перезапуска сессии: загрузчик читает каталог при старте.

## Структура

```
skills/financial-analyst/
├── SKILL.md # процесс, ограничения, проверка
├── references/ # формулы, схемы, контракты, гайды
├── scripts/
│ ├── analyze.py # CLI
│ ├── check_skill.py # самопроверка скилла
│ └── finanalyst/ # ядро: метрики, графики, отчёты
├── templates/ # бриф, постановка задачи, каркас отчёта
└── assets/sample/ # синтетическая выгрузка для запуска
```

## Входные данные

Имя файла = имя таблицы. Поддерживаются CSV (UTF-8) и Excel.

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

- Прогноз — интервал ошибки модели (линейный тренд + бэктест MAE), а не причинная модель.
- Корреляции считаются только для опросов предприятий (попарно, с перестановочным
  p-value); матрица не заменяет причинный анализ.
- Нет автоматического обезличивания по умолчанию: используйте `--pii-mode=strict/redact`.
- Один запуск — один набор данных; сравнение нескольких выгрузок через флаг `--compare-dirs`.
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