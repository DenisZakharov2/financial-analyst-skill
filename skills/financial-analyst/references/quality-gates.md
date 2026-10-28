# Контроль качества входных данных

Проверки выполняются до расчётов. `fail` останавливает запуск в `--strict`,
`warn` пишется в отчёт и не мешает расчёту.

## Обязательные проверки

| ID | Уровень | Что проверяется | Почему |
|---|---|---|---|
| `portfolio_pk_unique` | fail | `account_id` уникален | Дубли удваивают портфель |
| `portfolio_fk_clients` | warn | ≥ 99 % `client_id` есть в `clients` | Сироты искажают сегментацию |
| `portfolio_fk_products` | warn | ≥ 99 % `product_code` есть в `products` | Без типа продукта разрез невозможен |
| `portfolio_balance_non_negative` | warn | Нет отрицательных остатков | Отрицательный остаток — признак кредитового оборота или ошибки |
| `portfolio_dpd_non_negative` | fail | `dpd ≥ 0` | Отрицательная просрочка невозможна |
| `portfolio_dpd_bucket_consistent` | warn | `par_bucket` соответствует `dpd` | Иначе PAR по бакету и по dpd расходятся |
| `clients_pk_unique` | fail | `client_id` уникален | Дубли клиентов завышают базу |
| `products_pk_unique` | fail | `product_code` уникален | Дубли типов ломают группировку |
| `sales_pk_unique` | fail | `application_id` уникален | Несколько строк на заявку ломают конверсию |
| `sales_amount_non_negative` | warn | Суммы ≥ 0 | Отрицательная сумма — сторно или ошибка знака |
| `finance_grain_unique` | fail | Уникальность `period + product_code` | Дубли периодов удваивают выручку |
| `finance_periods_parsed` | fail | Все периоды разобраны | Иначе ряд теряет точки |
| `macro_survey_rows` | fail | Таблица не пуста | Пустая выгрузка бессмысленна |
| `macro_survey_periods` | fail | Все периоды разобраны | Смешение типов ломает тренд |
| `macro_*_periods` | fail | Периоды разобраны | То же для рыночных таблиц |

## Предупреждения, которые нельзя игнорировать

- **Несколько валют в портфеле.** Суммы не сопоставимы без курса и даты курса.
- **`product_code = 'ALL'` в finance.** Итоговая строка вместе с детализацией
  удваивает выручку.
- **Метки этапов вне словаря.** `Лид` с заглавной, `issued`, `approve` —
  нормализуются, но лучше исправить источник.

## Как запускать

```bash
python scripts/analyze.py --data-dir ./data --out ./reports --strict
```

Коды возврата:

- `0` — проверки пройдены, отчёты созданы;
- `2` — ошибка данных или провал `fail`-проверки;
- `1` — непредвиденная ошибка (с трассировкой).

## Ручные проверки перед отчётом

```python
# 1. Ключи
assert portfolio.account_id.is_unique
assert clients.client_id.is_unique

# 2. Связи
assert portfolio.client_id.isin(clients.client_id).mean() > 0.99

# 3. Даты
assert portfolio.open_date.min() <= portfolio.open_date.max()
assert (portfolio.open_date <= pd.Timestamp.today()).all()

# 4. Согласованность просрочки
assert (portfolio.loc[portfolio.dpd == 0, "par_bucket"] == "current").all()

# 5. Итоги сходятся
assert abs(portfolio.balance.sum() - by_product.total_balance.sum()) < 0.01
```

## Правило fail closed

Если проверка не может быть выполнена (нет колонки, пустая таблица), это `fail`,
а не «пропустим». Молчаливый пропуск — причина большинства неверных отчётов.
