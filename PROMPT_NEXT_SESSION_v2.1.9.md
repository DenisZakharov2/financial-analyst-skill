# Промт для следующей сессии: Financial Analyst Skill v2.1.9+

## Контекст
Репозиторий: `/home/anony/Documents/Личное/Проект приложения для аналитика/financial-analyst-skill/`
Ветка: `main`, чистая. Текущая версия: **2.1.9** (tag `v2.1.9` запушен, GitHub Release создан).
Все тесты зелёные (184+ passed, включая 11 property-based), ruff, mypy, check_skill.py — чисто.
CI: зелёный (Python 3.10/3.12 + postgres интеграция).

## Что сделано в текущей сессии (v2.1.9)

### 1. Сценарный анализ (What-if / Sensitivity Analysis) — пункт плана развития
**Новые модули:**
- `finanalyst/scenario.py` — `ScenarioEngine`, 6 встроенных пресетов (`base`, `stress_rate_up`, `stress_rate_down`, `stress_conversion_drop`, `optimistic`, `regulatory_stress`), sensitivity analysis
- Интеграция в CLI: `--scenario-list standard|all`, `--scenario PATH`, `--scenario-param PARAM=VALUE`
- YAML секция `scenario:` с поддержкой `enabled`, `preset`, `config_file`, `params`
- Этап `_stage_scenario(p)` в пайплайне (после плагинов, перед портфелем)
- Результаты в отчёте: раздел "Сценарии: сравнение", секция `scenarios` в `metrics.json`

### 2. Property-based testing (Hypothesis)
**Новый файл:** `tests/test_property.py` — 11 тестов:
- `TestUnitEconomicsProperties` — LTV/CAC ratio, ARPU/churn
- `TestFinanceMetricsProperties` — margin bounds (<=1, finite)
- `TestPortfolioMetricsProperties` — PAR30 >= 0, total balance >= 0
- `TestDataValidationProperties` — validate_inputs на сгенерированных данных
- `TestCreditRiskProperties` — Expected Loss bounds, RWA Vasicek formula (прямая проверка)
- `TestForecastProperties` — линейный тренд finite values
- `TestComparisonProperties` — bridge decomposition sums
- `TestDataInvariants` — DPD/PAR consistency, client age >= 18

**Стратегии генерации:**
- `valid_portfolio_row()` — консистентные DPD/PAR bucket, datetime.date
- `valid_client_row()` — возраст >= 18, datetime.date
- `valid_finance_row()` — периоды, продуктовые коды

### 3. Версии и документация
- Версия **2.1.9** синхронизирована: `pyproject.toml`, `__init__.py`, `SKILL.md`
- `SKILL.md` description — ровно 300 символов (лимит)
- `CHANGELOG.md` — запись v2.1.9
- `README.md` — таблица флагов, раздел "Сценарный анализ" с примерами
- GitHub Release v2.1.9 создан (Latest)

---

## Что осталось сделать (план развития)

### Приоритет 1: Streamlit UI (долгосрочно)
- Интерактивный веб-интерфейс для запуска пайплайна
- Визуализация метрик и сценариев в браузере
- Загрузка файлов через drag-and-drop

### Приоритет 2: Регуляторные шаблоны
- КоФ (ЦБ РФ) отчёты: 101, 102, 135-ФЗ
- НБКИ выгрузки
- Базель III пилар 3 отчётность

### Приоритет 3: BI-интеграция
- Экспорт в PowerBI / Tableau / Metabase / Superset
- Коннекторы для ClickHouse / PostgreSQL / BigQuery

### Приоритет 4: Memory-efficient режим
- Chunked processing для больших данных (>10M строк)
- Партиционирование по времени/продуктам
- Параллельная обработка чанков

### Приоритет 5: Дополнительные тесты
- `tests/test_scenario.py` — unit/integration тесты для сценариев
- Load/stress тесты производительности

---

## Команды для старта следующей сессии

```bash
cd "/home/anony/Documents/Личное/Проект приложения для аналитика/financial-analyst-skill"
source .venv/bin/activate
pytest -x -q
ruff check .
mypy --ignore-missing-imports skills/financial-analyst/scripts/finanalyst
python skills/financial-analyst/scripts/check_skill.py
```

## Важные напоминания
- Минимальные изменения. Не добавлять слои/абстракции с одной реализацией.
- Не резать валидацию и безопасность.
- Тесты после каждого шага. Не переходить к следующему, пока предыдущий не зелёный.
- Версию пакета оставляем без изменений до релиза (бамп — отдельный шаг).