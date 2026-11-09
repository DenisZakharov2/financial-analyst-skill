# Контракты метрик

Для каждой метрики: зерно, числитель, знаменатель, период, краевой случай и источник.
Если реализация расходится с таблицей, правится реализация, а не таблица.

| Метрика | Зерно | Числитель | Знаменатель | Краевой случай | Источник |
|---|---|---|---|---|---|
| `accounts` | тип продукта | число уникальных `account_id` | — | пустая таблица → ошибка данных (код 2); сумма по продуктам сходится с итогом | portfolio.accounts |
| `total_balance` | тип продукта | сумма остатков | — | все `null` → `null` | portfolio.balance |
| `par30_pct` | тип продукта | баланс с `dpd ≥ 30` | общий баланс | баланс = 0 → `null` | portfolio.dpd, balance |
| `par60_pct` | тип продукта | баланс с `dpd ≥ 60` | общий баланс | баланс = 0 → `null` | portfolio.dpd, balance |
| `par90_pct` | тип продукта | баланс с `dpd ≥ 90` | общий баланс | баланс = 0 → `null` | portfolio.dpd, balance |
| `top10_clients_share_pct` | портфель | баланс топ-10 клиентов | баланс всех клиентов | < 10 клиентов → по факту | portfolio.clients, balance |
| `reached_i` | этап воронки | заявки с максимальным этапом ≥ i | — | нет заявок → `null` | funnel_events.application_id |
| `from_start_pct` | этап воронки | `reached_i` | `reached_1` | `reached_1 = 0` → `null` | funnel_events |
| `stage_pct` | этап воронки | `reached_i` | `reached_{i-1}` | предыдущий = 0 → `null` | funnel_events |
| `tat_median_p50_days` | переход воронки | медиана `event_date_{i+1} - event_date_i` (сут.) | — | < 2 этапов у заявки → не входит в переход | funnel_events |
| `tat_p90_days` | переход воронки | 90-й перцентиль `event_date_{i+1} - event_date_i` | — | нет завершённых переходов → `null` | funnel_events |
| `sla_compliance_pct` | переход воронки | переходы с `tat_days ≤ sla_threshold` | все завершённые переходы этапа | переходов = 0 → `null` | funnel_events |
| `aging_median_days` | этап воронки | медиана `reference_date - last_event_date` | — | нет зависших заявок → 0 | funnel_events, sales |
| `km_retention_pct` | этап воронки | $\prod_{k < i} (1 - \text{hazard}_k) \times 100$ | — | `at_risk = 0` → `hazard = 0` | funnel_events |
| `revenue` | период × продукт | проценты + комиссии + премии | — | нет колонки → 0 с пометкой | finance |
| `gross_margin` | период × продукт | выручка - фондирование - резервы | — | — | finance |
| `margin_pct` | период × продукт | валовая маржа | выручка | выручка = 0 → `null` | finance |
| `nim_pct` | портфель | проценты - фондирование за месяц (поток × месяцы) | сумма остатков портфеля | баланс = 0 → `null` | finance, portfolio |
| `active_clients` | портфель | уникальные `client_id` | — | нет данных → `null` | portfolio.clients |
| `new_clients` | портфель | клиенты с `registration_date` внутри периода **и счётом в portfolio** | — | нет даты → 0 | portfolio.clients |
| `arpu` | клиент × месяц | выручка | активные клиенты × месяцы | клиентов = 0 → `null` | finance, portfolio |
| `cac` | клиент | расходы на привлечение | новые клиенты за период, иначе активные | нет `marketing_cost` → `null` | finance |
| `ltv_gross` | клиент | ARPU × маржа × срок жизни | — | нет ARPU → `null` | расчёт |
| `ltv_cac` | клиент | LTV_gross | CAC | CAC = 0 → `null` | расчёт |
| `payback_months` | клиент | CAC | ARPU × маржа | маржа = 0 → `null` | расчёт |
| `change_pct` | период | значение_t - значение_{t-1} | значение_{t-1} | предыдущее = 0 или `null` → `null` | ряд |
| `yoy_pct` | период | значение t против значения год назад по календарю | значение год назад | нет даты год назад → `null` | ряд |
| `seasonal_index` | месяц / квартал | медиана отклонения от центрированного тренда | среднее индексов по циклу | $< 2P$ точек → не строится; при $y_t \le 0$ — аддитивный | ряд |
| `strength_seasonal` | ряд | $1 - \text{Var}(R_t) / \text{Var}(S_t + R_t)$ | — | $< 2P$ точек → `0.0`; ограничивается в $[0; 1]$ | ряд |
| `z_score` | серия | робастный Z: MAD при n < 50, обычный при n ≥ 50 | — | n < 3 → `null`; MAD = 0 → Z по σ; константа → Z = 0 | отчёт |
| `mae` | ряд | среднее абсолютной ошибки | — | < 3 периодов → прогноза нет | бэктест |
| `smape_pct` | ряд | $2 \cdot |\text{факт} - \text{прогноз}|$ | $|\text{факт}| + |\text{прогноз}|$ | оба = 0 → точка равна 0 | бэктест |
| `mase` | ряд | `mae` на бэктесте | средняя абсолютная разность $|\Delta y_t|$ на обучении | константный ряд → `null` | бэктест |
| `mape_pct` | ряд | средняя относительная ошибка | — | любой факт $\le 10^{-6}$ → `null` (используйте `smape_pct`) | бэктест |
| `pvm_volume_effect` | продукт | $(Q_1 - Q_0) \cdot s_{i,0} \cdot \bar{R}_0$ | — | $Q_0 \le 0$ или $Q_1 \le 0$ → `null` | finance, sales |
| `pvm_mix_effect` | продукт | $Q_1 \cdot (s_{i,1} - s_{i,0}) \cdot (R_{i,0} - \bar{R}_0)$ | — | $Q_0 \le 0$ или $Q_1 \le 0$ → `null` | finance, sales |
| `pvm_rate_effect` | продукт | $Q_{i,0} \cdot (R_{i,1} - R_{i,0})$ | — | $Q_{i,0} = 0$ → $R_{i,0} = R_{i,1}$ | finance, sales |
| `within_risk_effect_pp` | продукт | $0.5 \cdot (w_{i,0} + w_{i,1}) \cdot (r_{i,1} - r_{i,0})$ | — | баланс = 0 → `null` | portfolio |
| `structural_mix_effect_pp` | продукт | $0.5 \cdot (r_{i,0} + r_{i,1}) \cdot (w_{i,1} - w_{i,0})$ | — | баланс = 0 → `null` | portfolio |
| `granger_p_fdr` | пара индикаторов | F-статистика Грейнджера с поправкой Бенджамини — Хохберга | — | $T < 8$ или $\sigma = 0$ → `null` | macro_survey |
| `ab_srm_p_value` | эксперимент | $\chi^2$ отклонения фактического сплита от целевого | — | $p < 0.001$ → блокировка выводов (`invalid_srm_detected`) | эксперимент |
| `ab_absolute_effect` | единица `client_id` | среднее (или ratio) в `treatment` минус `control` | — | $< 2$ клиентов в группе → `null` | эксперимент |
| `did_att_effect` | панель (регион × период) | коэффициент при `Treat × Post` (OLS с HC1 SE) | — | нет вариации до/после → `null` | панель |
| `rfm_segment` | клиент (`client_id`) | правило по квантилям $R, F, M$ и `max_dpd` | — | `max_dpd > 0` → всегда `Просроченные (DPD > 0)` | clients, portfolio, sales |
| `bridge_balance_delta` | корзина клиентов | $\sum (B_{i,1} - B_{i,0})$ внутри корзины (`New`, `Churned`, `Expansion`, `Contraction`) | — | сумма 4 корзин строго равна $\Delta \text{Total}$ | portfolio (2 среза) |
| `net_dollar_retention_pct` | портфель (2 среза) | баланс удержанных клиентов (`both`) в целевом срезе | общий баланс базового среза | $B_0 = 0$ → `null` | portfolio (2 среза) |

`nim_pct` — годовая ставка: поток за месяцы выгрузки приводится к месяцу и
умножается на 12, иначе выручка за 6 месяцев делится на остаток одного дня.

## Правила

1. **Зерно фиксировано.** Нельзя смешивать строку продукта и строку `ALL`:
   итог исключается из сумм, если в том же периоде есть детализация,
   а число исключённых строк фиксируется в `excluded_total_rows`.
2. **Знаменатель нулевой → `null` и причина.** Не 0, не `NaN`, не «-» без объяснения.
3. **Период — календарный месяц.** Квартал приводится к первому месяцу квартала
   с типом `Q`; смешивать типы в одном ряду нельзя.
4. **Валюта.** Метрики в разных валютах не агрегируются. Если в портфеле
   несколько валют, сначала пересчёт по курсу с датой, потом агрегат.
5. **Округление.** Хранить значение без округления, округлять при выводе:
   деньги — 2 знака, проценты — 2, коэффициенты — 2.
6. **Обезличивание не меняет суммы.** Псевдонимизация (`--pii-mode=redact`)
   применяется только к ключам (`client_id`, `account_id`, `application_id`)
   и удаляет текстовые колонки ПДн; числовые финансовые поля не зашумляются и не биннингуются.

## Проверка контракта

```python
assert par30 >= par60 >= par90
assert 0 <= from_start_pct <= 100
assert from_start_pct.is_monotonic_decreasing
assert ltv_cac is None or ltv_cac > 0
assert 0.0 <= strength_seasonal <= 1.0
assert abs(sum(pvm["effects"].values()) - pvm["total_delta"]) < 0.01
assert abs((par_shift["within_risk_effect_pp"] + par_shift["structural_mix_effect_pp"]) - par_shift["delta_par_pp"]) < 0.01
assert abs(bridge_df["balance_delta"].sum() - bridge_report["balance_delta"]) < 0.01
```

Если проверка падает, это дефект данных или реализации. Отчёт с такими числами
не отдаётся.
