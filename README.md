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

## Флаги расширенных расчётов

| Флаг | Что делает |
|---|---|
| `--forecast-model legacy\|auto\|linear\|ets\|arx` | Каскад моделей прогноза с выбором на бэктесте (`legacy` — линейный тренд, по умолчанию) |
| `--ab-test` | A/B-тест: SRM, тест Уэлча и Манна — Уитни, CUPED, линеаризация ratio-метрик |
| `--ab-metric`, `--ab-metric-type`, `--ab-denominator`, `--ab-pre-metric`, `--ab-group-col`, `--ab-lower-is-better` | Настройка A/B-теста. `--ab-lower-is-better` — для метрик, где меньше значит лучше (PAR, срок, расходы) |
| `--segment` | Сегментация клиентов: RFM + Risk и кластеризация (`--no-cluster` — без кластеризации) |
| `--drivers` | PVM и разложение сдвига PAR между срезами, лаговые связи макропоказателей |

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
│   └── finanalyst/           # ядро: метрики, графики, отчёты
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