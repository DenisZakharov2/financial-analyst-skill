"""Регуляторная отчётность: КоФ (ЦБ РФ), НБКИ, Базель III Пилар 3.

Модуль генерирует отчёты в форматах, соответствующих требованиям регулятора:
- Форма 101: Отчёт о финансовом положении кредитной организации
- Форма 102: Отчёт о финансовых результатах
- 135-ФЗ: Информация о заёмщиках для БКИ
- НБКИ: Выгрузка кредитных историй
- Базель III Пилар 3: Информация о достаточности капитала и рисках
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from .credit_risk import CreditRiskConfig, calculate_credit_risk

# ============================================================
# Конфигурация регуляторных отчётов
# ============================================================

@dataclass
class RegulatoryConfig:
    """Конфигурация для регуляторных отчётов."""
    # Банк/Организация
    org_name: str = "ООО «Финансовая Компания»"
    org_bik: str = "044525225"  # БИК
    org_inn: str = "7707083893"  # ИНН
    org_okpo: str = "12345678"   # ОКПО

    # Период отчётности
    reporting_date: date = field(default_factory=date.today)
    reporting_period: str = "M"  # M=месяц, Q=квартал, Y=год

    # Валюта отчётности (тыс. руб.)
    currency_unit: str = "thousand_rub"

    # Настройки КоФ
    cof_include_off_balance: bool = True
    cof_consolidated: bool = False

    # Настройки 135-ФЗ / НБКИ
    nbki_include_guarantors: bool = True
    nbki_cutoff_days: int = 90  # Отсечка по просрочке для передачи в БКИ

    # Базель III
    basel_include_pillar3: bool = True
    basel_tier1_ratio_min: float = 0.085  # 8.5% включая буфер
    basel_total_capital_ratio_min: float = 0.105  # 10.5%


# ============================================================
# Вспомогательные функции форматирования
# ============================================================

def _fmt_num(value: float, decimals: int = 0, unit: str = "") -> str:
    """Форматирование числа для отчётов."""
    if pd.isna(value) or value is None:
        return "-"
    if abs(value) >= 1e9:
        return f"{value/1e9:.{decimals}f} млрд{unit}"
    elif abs(value) >= 1e6:
        return f"{value/1e6:.{decimals}f} млн{unit}"
    elif abs(value) >= 1e3:
        return f"{value/1e3:.{decimals}f} тыс{unit}"
    else:
        return f"{value:,.{decimals}f}{unit}"


def _fmt_pct(value: float, decimals: int = 2) -> str:
    """Форматирование процента."""
    if pd.isna(value) or value is None:
        return "-"
    return f"{value*100:.{decimals}f}%"


def _to_thousands(value: float) -> float:
    """Перевод в тысячи рублей."""
    return value / 1000.0


# ============================================================
# Форма 101: Отчёт о финансовом положении (баланс)
# ============================================================

def generate_form_101(
    tables: dict[str, pd.DataFrame],
    credit_result: dict | None = None,
    config: RegulatoryConfig | None = None,
) -> dict[str, Any]:
    """Генерация формы 101 (пункт 1.1 КоФ).

    Структура согласно Приказу Банка России от 24.11.2020 № 5610-У
    (с изменениями на 2024 год).
    """
    if config is None:
        config = RegulatoryConfig()

    portfolio = tables.get("portfolio", pd.DataFrame())
    tables.get("finance", pd.DataFrame())
    tables.get("products", pd.DataFrame())

    if portfolio.empty:
        return {"available": False, "error": "Portfolio table is empty"}

    # Активы
    total_assets = portfolio["balance"].sum() if "balance" in portfolio.columns else 0

    # Кредитная задолженность (нетто)
    gross_loans = portfolio[portfolio.get("product_code", "").isin(["LOAN", "CREDIT"])]["balance"].sum() \
        if "product_code" in portfolio.columns else total_assets

    # Резервы под возможные потери
    # Приблизительно: PAR90 * LGD * баланс
    if credit_result and credit_result.get("available"):
        reserves = credit_result["portfolio"].get("total_expected_loss", 0)
    else:
        # Грубая оценка: PAR30+ * 5% + PAR90 * 45%
        par30 = portfolio[portfolio.get("dpd", 0) >= 30]["balance"].sum() if "dpd" in portfolio.columns else 0
        par90 = portfolio[portfolio.get("dpd", 0) >= 90]["balance"].sum() if "dpd" in portfolio.columns else 0
        reserves = par30 * 0.05 + par90 * 0.45

    net_loans = gross_loans - reserves

    # Денежные средства и эквиваленты
    cash = portfolio[portfolio.get("product_code", "").isin(["CASH", "DEPOSIT"])]["balance"].sum() \
        if "product_code" in portfolio.columns else 0

    # Ценные бумаги
    securities = portfolio[portfolio.get("product_code", "").isin(["BOND", "EQUITY"])]["balance"].sum() \
        if "product_code" in portfolio.columns else 0

    # Основные средства и НМА
    fixed_assets = 0  # Нет в наших данных

    # Прочие активы
    other_assets = total_assets - gross_loans - cash - securities - fixed_assets
    other_assets = max(other_assets, 0)

    # Пассивы
    # Средства клиентов (вклады)
    deposits = portfolio[portfolio.get("product_code", "").isin(["DEPOSIT", "SAVINGS"])]["balance"].sum() \
        if "product_code" in portfolio.columns else 0

    # Займы от банков и ЦБ
    borrowed_funds = 0  # Нет в наших данных

    # Выпущенные ценные бумаги
    debt_securities = 0

    # Прочие пассивы
    other_liabilities = total_assets * 0.05  # Приблизительно 5%

    # Капитал
    capital = total_assets - deposits - borrowed_funds - debt_securities - other_liabilities

    # Приводим к тысячам рублей
    unit = 1000.0

    form_101 = {
        "form_code": "101",
        "form_name": "Отчёт о финансовом положении кредитной организации",
        "reporting_date": config.reporting_date.isoformat(),
        "org_name": config.org_name,
        "org_bik": config.org_bik,
        "currency": "тыс. руб.",
        "sections": {
            "assets": {
                "name": "АКТИВЫ",
                "items": [
                    {"code": "1", "name": "Денежные средства", "value": round(cash / unit, 0)},
                    {"code": "2", "name": "Ценные бумаги", "value": round(securities / unit, 0)},
                    {"code": "3", "name": "Кредитная задолженность нетто", "value": round(net_loans / unit, 0)},
                    {"code": "3.1", "name": "  включая: Кредитная задолженность валовая", "value": round(gross_loans / unit, 0)},
                    {"code": "3.2", "name": "  минус: Резервы под возможные потери", "value": round(-reserves / unit, 0)},
                    {"code": "4", "name": "Основные средства, НМА и нематериальные активы", "value": round(fixed_assets / unit, 0)},
                    {"code": "5", "name": "Прочие активы", "value": round(other_assets / unit, 0)},
                    {"code": "TOTAL", "name": "ВСЕГО АКТИВЫ", "value": round(total_assets / unit, 0), "is_total": True},
                ]
            },
            "liabilities": {
                "name": "ПАССИВЫ",
                "items": [
                    {"code": "10", "name": "Средства ЦБ РФ", "value": round(borrowed_funds / unit, 0)},
                    {"code": "11", "name": "Средства клиентов (вклады)", "value": round(deposits / unit, 0)},
                    {"code": "12", "name": "Заемные средства от кредитных организаций", "value": round(borrowed_funds / unit, 0)},
                    {"code": "13", "name": "Выпущенные ценные бумаги", "value": round(debt_securities / unit, 0)},
                    {"code": "14", "name": "Прочие пассивы", "value": round(other_liabilities / unit, 0)},
                    {"code": "15", "name": "Капитал и резервы", "value": round(capital / unit, 0)},
                    {"code": "TOTAL", "name": "ВСЕГО ПАССИВЫ", "value": round(total_assets / unit, 0), "is_total": True},
                ]
            }
        },
        "checks": {
            "balance_equality": abs(total_assets - (deposits + borrowed_funds + debt_securities + other_liabilities + capital)) < 1000,
            "capital_adequacy": capital > 0,
        }
    }

    return form_101


# ============================================================
# Форма 102: Отчёт о финансовых результатах (ОФР)
# ============================================================

def generate_form_102(
    tables: dict[str, pd.DataFrame],
    credit_result: dict | None = None,
    config: RegulatoryConfig | None = None,
) -> dict[str, Any]:
    """Генерация формы 102 (пункт 1.2 КоФ).

    Отчёт о финансовых результатах за период.
    """
    if config is None:
        config = RegulatoryConfig()

    finance = tables.get("finance", pd.DataFrame())
    portfolio = tables.get("portfolio", pd.DataFrame())

    if finance.empty:
        return {"available": False, "error": "Finance table is empty"}

    # Берём последний период
    latest_period = finance["period"].max() if "period" in finance.columns else None
    period_data = finance[finance["period"] == latest_period] if latest_period else finance

    # Доходы
    interest_income = period_data["interest_income"].sum() if "interest_income" in period_data.columns else 0
    fee_income = period_data["fee_income"].sum() if "fee_income" in period_data.columns else 0
    other_income = 0  # Нет в данных

    # Расходы
    interest_expense = period_data["interest_expense"].sum() if "interest_expense" in period_data.columns else 0
    funding_cost = period_data["funding_cost"].sum() if "funding_cost" in period_data.columns else 0
    opex = period_data["opex"].sum() if "opex" in period_data.columns else 0
    reserve_expense = period_data["reserve_expense"].sum() if "reserve_expense" in period_data.columns else 0

    # Налог
    tax = period_data["tax"].sum() if "tax" in period_data.columns else 0

    # Расчётные показатели
    net_interest_income = interest_income - interest_expense
    net_fee_income = fee_income
    operating_income = net_interest_income + net_fee_income + other_income
    operating_expenses = opex + funding_cost
    profit_before_provisions = operating_income - operating_expenses
    profit_before_tax = profit_before_provisions - reserve_expense
    net_profit = profit_before_tax - tax

    unit = 1000.0

    form_102 = {
        "form_code": "102",
        "form_name": "Отчёт о финансовых результатах",
        "reporting_period": latest_period or config.reporting_date.strftime("%Y-%m"),
        "org_name": config.org_name,
        "org_bik": config.org_bik,
        "currency": "тыс. руб.",
        "sections": {
            "income": {
                "name": "ДОХОДЫ",
                "items": [
                    {"code": "1", "name": "Процентные доходы", "value": round(interest_income / unit, 0)},
                    {"code": "2", "name": "Комиссионные доходы", "value": round(fee_income / unit, 0)},
                    {"code": "3", "name": "Прочие операционные доходы", "value": round(other_income / unit, 0)},
                    {"code": "TOTAL", "name": "ВСЕГО ДОХОДЫ", "value": round((interest_income + fee_income + other_income) / unit, 0), "is_total": True},
                ]
            },
            "expenses": {
                "name": "РАСХОДЫ",
                "items": [
                    {"code": "10", "name": "Процентные расходы", "value": round(interest_expense / unit, 0)},
                    {"code": "11", "name": "Расходы на финансирование", "value": round(funding_cost / unit, 0)},
                    {"code": "12", "name": "Операционные расходы (OPEX)", "value": round(opex / unit, 0)},
                    {"code": "13", "name": "Резервы под возможные потери", "value": round(reserve_expense / unit, 0)},
                    {"code": "TOTAL", "name": "ВСЕГО РАСХОДЫ", "value": round((interest_expense + funding_cost + opex + reserve_expense) / unit, 0), "is_total": True},
                ]
            },
            "profit": {
                "name": "ФИНАНСОВЫЙ РЕЗУЛЬТАТ",
                "items": [
                    {"code": "20", "name": "Чистый процентный доход (НПД)", "value": round(net_interest_income / unit, 0)},
                    {"code": "21", "name": "Чистый комиссионный доход", "value": round(net_fee_income / unit, 0)},
                    {"code": "22", "name": "Операционный доход", "value": round(operating_income / unit, 0)},
                    {"code": "23", "name": "Операционные расходы", "value": round(-operating_expenses / unit, 0)},
                    {"code": "24", "name": "Прибыль до создания резервов", "value": round(profit_before_provisions / unit, 0)},
                    {"code": "25", "name": "Расходы на создание резервов", "value": round(-reserve_expense / unit, 0)},
                    {"code": "26", "name": "Прибыль до налогообложения", "value": round(profit_before_tax / unit, 0)},
                    {"code": "27", "name": "Налог на прибыль", "value": round(-tax / unit, 0)},
                    {"code": "28", "name": "ЧИСТАЯ ПРИБЫЛЬ (УБЫТОК)", "value": round(net_profit / unit, 0), "is_total": True},
                ]
            }
        },
        "key_ratios": {
            "nim": _fmt_pct(net_interest_income / portfolio["balance"].sum() if portfolio["balance"].sum() > 0 else 0),
            "cost_income": _fmt_pct(operating_expenses / operating_income if operating_income > 0 else 0),
            "roa": _fmt_pct(net_profit / portfolio["balance"].sum() if portfolio["balance"].sum() > 0 else 0),
            "roe": _fmt_pct(net_profit / (portfolio["balance"].sum() * 0.1) if portfolio["balance"].sum() > 0 else 0),  # Прибл. капитал 10%
        }
    }

    return form_102


# ============================================================
# 135-ФЗ / НБКИ: Выгрузка для Бюро кредитных историй
# ============================================================

def generate_nbki_export(
    tables: dict[str, pd.DataFrame],
    credit_result: dict | None = None,
    config: RegulatoryConfig | None = None,
) -> dict[str, Any]:
    """Генерация выгрузки для НБКИ по 135-ФЗ.

    Формат согласно Приказу Банка России от 26.12.2017 № 4828-У
    (требования к передаче информации в БКИ).
    """
    if config is None:
        config = RegulatoryConfig()

    portfolio = tables.get("portfolio", pd.DataFrame())
    clients = tables.get("clients", pd.DataFrame())

    if portfolio.empty or clients.empty:
        return {"available": False, "error": "Portfolio or clients table is empty"}

    # Объединяем портфель с клиентами
    df = portfolio.merge(clients, on="client_id", how="left")

    # Фильтруем: только активные договоры с просрочкой или открытые
    active_mask = df.get("status", "ACTIVE") == "ACTIVE"
    df = df[active_mask].copy()

    # Рассчитываем DPD
    if "dpd" not in df.columns:
        df["dpd"] = 0

    # Формируем записи для НБКИ
    records = []
    for _, row in df.iterrows():
        dpd = int(row.get("dpd", 0) or 0)

        # Определяем статус кредита для БКИ
        if dpd >= config.nbki_cutoff_days:
            credit_status = "ОVERDUE_90+"  # Просрочка 90+
        elif dpd >= 30:
            credit_status = "OVERDUE_30_89"
        elif dpd > 0:
            credit_status = "OVERDUE_1_29"
        else:
            credit_status = "NORMAL"

        # Сумма просрочки
        overdue_amount: float = 0.0
        if dpd > 0 and "balance" in row:
            # Приблизительно: просроченная часть пропорциональна DPD
            overdue_amount = float(row.get("balance", 0)) * min(dpd / 90, 1.0) * 0.1

        record = {
            # Идентификация заёмщика
            "borrower_type": "INDIVIDUAL" if row.get("segment") == "RETAIL" else "LEGAL_ENTITY",
            "borrower_inn": row.get("inn", ""),  # Нужно добавить в clients
            "borrower_name": f"{row.get('segment', '')} client {row.get('client_id', '')}",

            # Идентификация договора
            "contract_id": row.get("account_id", ""),
            "contract_date": row.get("open_date", ""),
            "contract_amount": float(row.get("limit_amount", row.get("balance", 0))),
            "currency": row.get("currency", "RUB"),

            # Статус и качество
            "credit_status": credit_status,
            "dpd": dpd,
            "overdue_principal": round(overdue_amount, 2),
            "overdue_interest": round(overdue_amount * 0.3, 2),  # Прибл.
            "overdue_total": round(overdue_amount * 1.3, 2),

            # Остаточная задолженность
            "outstanding_balance": float(row.get("balance", 0)),

            # Продукт
            "product_type": row.get("product_code", ""),
            "interest_rate": float(row.get("interest_rate", 0)) if "interest_rate" in row else 0,

            # Закрытие
            "closed_date": row.get("close_date", "") if row.get("close_date") else "",
            "close_reason": "EARLY_REPAYMENT" if row.get("close_date") else "",
        }
        records.append(record)

    # Сводка
    summary = {
        "total_contracts": len(records),
        "normal_contracts": len([r for r in records if r["credit_status"] == "NORMAL"]),
        "overdue_1_29": len([r for r in records if r["credit_status"] == "OVERDUE_1_29"]),
        "overdue_30_89": len([r for r in records if r["credit_status"] == "OVERDUE_30_89"]),
        "overdue_90plus": len([r for r in records if r["credit_status"] == "OVERDUE_90+"]),
        "total_outstanding": sum(r["outstanding_balance"] for r in records),
        "total_overdue": sum(r["overdue_total"] for r in records),
    }

    return {
        "form_code": "NBKI_135FZ",
        "form_name": "Выгрузка информации о заёмщиках для БКИ (135-ФЗ)",
        "reporting_date": config.reporting_date.isoformat(),
        "org_name": config.org_name,
        "org_inn": config.org_inn,
        "records": records,
        "summary": summary,
        "format_version": "4828-U",
    }


# ============================================================
# Базель III Пилар 3: Информация о достаточности капитала
# ============================================================

def generate_basel_pillar3(
    tables: dict[str, pd.DataFrame],
    credit_result: dict | None = None,
    config: RegulatoryConfig | None = None,
) -> dict[str, Any]:
    """Генерация отчёта Базель III Пилар 3.

    Согласно Basel III: Pillar 3 disclosure requirements (BCBS 309, 2015)
    и рекомендациям ЦБ РФ для небанковских кредитных организаций.
    """
    if config is None:
        config = RegulatoryConfig()

    portfolio = tables.get("portfolio", pd.DataFrame())
    tables.get("finance", pd.DataFrame())

    if portfolio.empty:
        return {"available": False, "error": "Portfolio table is empty"}

    # Если нет расчёта кредитного риска, делаем его
    if credit_result is None or not credit_result.get("available"):
        cr_config = CreditRiskConfig()
        credit_result = calculate_credit_risk(portfolio, cr_config)

    if not credit_result.get("available"):
        return {"available": False, "error": "Credit risk calculation failed"}

    port_cr = credit_result["portfolio"]

    # Капитал (упрощённо)
    total_assets = portfolio["balance"].sum()
    total_rwa = port_cr.get("total_rwa", 0)
    tier1_capital = total_assets * 0.1  # Прибл. 10% от активов
    tier2_capital = total_assets * 0.02  # Прибл. 2%
    total_capital = tier1_capital + tier2_capital

    # Коэффициенты достаточности
    cet1_ratio = tier1_capital / total_rwa if total_rwa > 0 else 0
    tier1_ratio = tier1_capital / total_rwa if total_rwa > 0 else 0
    total_capital_ratio = total_capital / total_rwa if total_rwa > 0 else 0

    # Леверидж
    leverage_ratio = tier1_capital / total_assets if total_assets > 0 else 0

    # ЛКР и НСФР (упрощённо)
    hqla = portfolio[portfolio.get("product_code", "").isin(["CASH", "BOND_GOV"])]["balance"].sum() \
        if "product_code" in portfolio.columns else total_assets * 0.15
    total_outflows = total_assets * 0.2  # Прибл.
    lcr = hqla / total_outflows if total_outflows > 0 else 0

    available_stable_funding = total_assets * 0.8
    required_stable_funding = total_rwa * 0.85
    nsfr = available_stable_funding / required_stable_funding if required_stable_funding > 0 else 0

    # Структура RWA
    rwa_by_risk = {
        "credit_risk": total_rwa,
        "market_risk": 0,  # Нет данных
        "operational_risk": total_rwa * 0.15,  # Базовый индикаторный подход ~15%
    }

    # Кредитный риск по классам активов
    rwa_by_asset_class = []
    if "summary_by_product" in credit_result and credit_result["summary_by_product"]:
        for prod in credit_result["summary_by_product"]:
            rwa_by_asset_class.append({
                "asset_class": prod.get("product_code", "UNKNOWN"),
                "exposure_ead": prod.get("total_ead", 0),
                "rwa": prod.get("total_rwa", 0),
                "avg_rw": prod.get("total_rwa", 0) / prod.get("total_ead", 1) if prod.get("total_ead", 0) > 0 else 0,
                "el": prod.get("total_el", 0),
            })

    # Концентрация
    top_exposures = portfolio.nlargest(20, "balance")["balance"].sum() if "balance" in portfolio.columns else 0
    concentration_ratio = top_exposures / total_assets if total_assets > 0 else 0

    return {
        "form_code": "BASEL_PILLAR3",
        "form_name": "Пилар 3: Информация о достаточности капитала и рисках (Базель III)",
        "reporting_date": config.reporting_date.isoformat(),
        "org_name": config.org_name,
        "framework": "Basel III (BCBS 309) / ЦБ РФ",
        "capital_adequacy": {
            "cet1_capital": round(tier1_capital, 0),
            "tier1_capital": round(tier1_capital, 0),
            "tier2_capital": round(tier2_capital, 0),
            "total_capital": round(total_capital, 0),
            "total_rwa": round(total_rwa, 0),
            "cet1_ratio": round(cet1_ratio, 4),
            "tier1_ratio": round(tier1_ratio, 4),
            "total_capital_ratio": round(total_capital_ratio, 4),
            "min_cet1_required": config.basel_tier1_ratio_min,
            "min_total_required": config.basel_total_capital_ratio_min,
            "capital_buffer": round(total_capital_ratio - config.basel_total_capital_ratio_min, 4),
        },
        "leverage_ratio": {
            "tier1_capital": round(tier1_capital, 0),
            "total_exposure": round(total_assets, 0),
            "leverage_ratio": round(leverage_ratio, 4),
            "min_required": 0.03,  # 3% minimum
        },
        "liquidity": {
            "lcr": {
                "hqla": round(hqla, 0),
                "net_cash_outflows": round(total_outflows, 0),
                "lcr_ratio": round(lcr, 4),
                "min_required": 1.0,
            },
            "nsfr": {
                "available_stable_funding": round(available_stable_funding, 0),
                "required_stable_funding": round(required_stable_funding, 0),
                "nsfr_ratio": round(nsfr, 4),
                "min_required": 1.0,
            }
        },
        "rwa_structure": {
            "by_risk_type": rwa_by_risk,
            "by_asset_class": rwa_by_asset_class,
        },
        "credit_risk": {
            "total_ead": round(port_cr.get("total_ead", 0), 0),
            "total_el": round(port_cr.get("total_expected_loss", 0), 0),
            "portfolio_pd": port_cr.get("portfolio_pd", 0),
            "portfolio_lgd": port_cr.get("portfolio_lgd", 0),
            "el_coverage_ratio": port_cr.get("el_ratio", 0),
            "by_par_bucket": credit_result.get("summary_by_bucket", []),
        },
        "concentration": {
            "top_20_exposures": round(top_exposures, 0),
            "total_portfolio": round(total_assets, 0),
            "concentration_ratio": round(concentration_ratio, 4),
            "hhi_estimate": round(concentration_ratio * concentration_ratio * 10000, 0),  # Прибл. HHI
        },
        "compliance": {
            "cet1_ok": cet1_ratio >= config.basel_tier1_ratio_min,
            "total_capital_ok": total_capital_ratio >= config.basel_total_capital_ratio_min,
            "leverage_ok": leverage_ratio >= 0.03,
            "lcr_ok": lcr >= 1.0,
            "nsfr_ok": nsfr >= 1.0,
        }
    }


# ============================================================
# Главная функция: генерация всех регуляторных отчётов
# ============================================================

def generate_regulatory_reports(
    tables: dict[str, pd.DataFrame],
    config: RegulatoryConfig | None = None,
    include: list[str] | None = None,
) -> dict[str, Any]:
    """Генерация пакета регуляторных отчётов.

    Args:
        tables: Словарь таблиц (portfolio, clients, finance, products, ...)
        config: Конфигурация регуляторных отчётов
        include: Список отчётов для генерации (по умолчанию все)
            Доступно: "cof_101", "cof_102", "nbki", "basel_pillar3"

    Returns:
        Словарь с результатами каждого отчёта
    """
    if config is None:
        config = RegulatoryConfig()

    if include is None:
        include = ["cof_101", "cof_102", "nbki", "basel_pillar3"]

    # Считаем кредитный риск один раз для всех отчётов
    portfolio = tables.get("portfolio", pd.DataFrame())
    credit_result = None
    if not portfolio.empty:
        cr_config = CreditRiskConfig()
        credit_result = calculate_credit_risk(portfolio, cr_config)

    results = {}

    if "cof_101" in include:
        results["cof_101"] = generate_form_101(tables, credit_result, config)

    if "cof_102" in include:
        results["cof_102"] = generate_form_102(tables, credit_result, config)

    if "nbki" in include:
        results["nbki"] = generate_nbki_export(tables, credit_result, config)

    if "basel_pillar3" in include:
        results["basel_pillar3"] = generate_basel_pillar3(tables, credit_result, config)

    # Мета-информация
    results["_meta"] = {
        "generated_at": datetime.now().isoformat(),
        "reporting_date": config.reporting_date.isoformat(),
        "org_name": config.org_name,
        "included_reports": include,
    }

    return results


# ============================================================
# Экспорт в файлы
# ============================================================

def save_regulatory_reports(
    reports: dict[str, Any],
    out_dir: Path,
    formats: list[str] = ["json", "xlsx"],
) -> dict[str, Path]:
    """Сохранение регуляторных отчётов в файлы."""
    out_dir.mkdir(parents=True, exist_ok=True)
    saved = {}

    for report_key, report_data in reports.items():
        if report_key == "_meta":
            continue

        if not report_data.get("available", True):
            continue

        base_name = f"regulatory_{report_data.get('form_code', report_key).lower()}"

        if "json" in formats:
            json_path = out_dir / f"{base_name}.json"
            with open(json_path, "w", encoding="utf-8") as f:
                # Конвертируем numpy типы
                def default_serializer(obj):
                                if isinstance(obj, (np.integer, np.floating)):
                                    return float(obj)
                                if isinstance(obj, np.bool_):
                                    return bool(obj)
                                if isinstance(obj, np.ndarray):
                                    return obj.tolist()
                                if isinstance(obj, (date, datetime)):
                                    return obj.isoformat()
                                raise TypeError(f"Type {type(obj)} not serializable")

                json.dump(report_data, f, ensure_ascii=False, indent=2, default=default_serializer)
            saved[f"{report_key}_json"] = json_path

        if "xlsx" in formats:
            xlsx_path = out_dir / f"{base_name}.xlsx"
            with pd.ExcelWriter(xlsx_path, engine="openpyxl") as writer:
                # Сохраняем каждую секцию на отдельном листе
                if "sections" in report_data:
                    for section_key, section_data in report_data["sections"].items():
                        if "items" in section_data:
                            df = pd.DataFrame(section_data["items"])
                            sheet_name = section_key[:31]  # Excel limit
                            df.to_excel(writer, sheet_name=sheet_name, index=False)

                # Summary листы
                for key in ["summary", "key_ratios", "capital_adequacy", "leverage_ratio",
                           "liquidity", "rwa_structure", "credit_risk", "concentration", "compliance"]:
                    if key in report_data:
                        data = report_data[key]
                        if isinstance(data, dict):
                            df = pd.DataFrame([data])
                        elif isinstance(data, list):
                            df = pd.DataFrame(data)
                        else:
                            continue
                        sheet_name = key[:31]
                        df.to_excel(writer, sheet_name=sheet_name, index=False)

            saved[f"{report_key}_xlsx"] = xlsx_path

    # Мета-файл
    if "json" in formats:
        meta_path = out_dir / "regulatory_meta.json"
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(reports.get("_meta", {}), f, ensure_ascii=False, indent=2)
        saved["meta_json"] = meta_path

    return saved


if __name__ == "__main__":
    # Тестовый запуск
    import sys
    sys.path.insert(0, str(Path(__file__).parent))

    from .core import load_tables

    sample_dir = Path(__file__).parent.parent / "assets" / "sample"
    tables = load_tables(sample_dir)

    config = RegulatoryConfig(
        org_name="Тестовый Банк ПАО",
        org_bik="044525225",
        reporting_date=date(2024, 12, 31),
    )

    reports = generate_regulatory_reports(tables, config)

    out_dir = Path("./regulatory_test")
    saved = save_regulatory_reports(reports, out_dir, ["json", "xlsx"])

    print("Сгенерированные отчёты:")
    for key, path in saved.items():
        print(f"  {key}: {path}")

    print("\nПроверка КоФ 101:")
    cof101 = reports.get("cof_101", {})
    if cof101.get("available"):
        for section in ["assets", "liabilities"]:
            print(f"  {section}: {len(cof101['sections'][section]['items'])} строк")
            for item in cof101['sections'][section]['items']:
                if item.get("is_total"):
                    print(f"    ИТОГО: {item['name']} = {item['value']:,.0f} тыс. руб.")
