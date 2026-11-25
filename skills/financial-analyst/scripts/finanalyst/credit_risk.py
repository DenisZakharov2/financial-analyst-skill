"""Credit Risk metrics: PD, LGD, EAD, Expected Loss, RWA.

Calculates Basel-style credit risk metrics from portfolio data.
Requires optional columns: score, default_flag, lgd, ead.
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass
class CreditRiskConfig:
    """Configuration for credit risk calculations."""

    # PD calibration: map score to PD using logistic calibration
    # PD = 1 / (1 + exp(a + b * score))
    # Typical: a = 3.0, b = -0.01 -> score 300: PD~0.5, score 600: PD~0.05, score 850: PD~0.001
    pd_calibration_a: float = 3.0  # intercept
    pd_calibration_b: float = -0.01  # slope (negative: higher score = lower PD)

    # Default PD if no score/default_flag available
    default_pd: float = 0.02

    # LGD defaults
    default_lgd: float = 0.45  # 45% loss given default

    # EAD: Credit Conversion Factor for undrawn limits
    ccf: float = 0.5  # 50% of undrawn limit

    # Correlation parameter for RWA (Basel II/III)
    asset_correlation: float = 0.24  # typical for corporate

    # Maturity adjustment
    maturity: float = 2.5  # years


def _score_to_pd(score: pd.Series, a: float, b: float) -> pd.Series:
    """Convert score to PD using logistic calibration.

    PD = 1 / (1 + exp(a + b * score))
    Higher score -> lower PD when b < 0.
    """
    return 1.0 / (1.0 + np.exp(a + b * score))


def _calculate_pd(
    portfolio: pd.DataFrame,
    config: CreditRiskConfig,
) -> pd.Series:
    """Calculate Probability of Default per account.

    Priority:
    1. If `default_flag` exists and is 1 -> PD = 1.0 (already defaulted)
    2. If `score` exists -> calibrate via logistic function
    3. If `dpd` >= 90 -> PD = 1.0 (default definition)
    4. Default PD from config
    """
    pd_series = pd.Series(config.default_pd, index=portfolio.index, dtype=float)

    # Already defaulted
    if "default_flag" in portfolio.columns:
        defaulted = portfolio["default_flag"].fillna(0).astype(int) == 1
        pd_series[defaulted] = 1.0

    # DPD >= 90 counts as default
    if "dpd" in portfolio.columns:
        dpd_default = pd.to_numeric(portfolio["dpd"], errors="coerce").fillna(0) >= 90
        pd_series[dpd_default] = 1.0

    # Score-based calibration for non-defaulted
    if "score" in portfolio.columns:
        mask = (pd_series < 1.0) & portfolio["score"].notna()
        if mask.any():
            scores = pd.to_numeric(portfolio.loc[mask, "score"], errors="coerce")
            pd_series.loc[mask] = _score_to_pd(scores, config.pd_calibration_a, config.pd_calibration_b)

    return pd_series.clip(0.0, 1.0)


def _calculate_lgd(portfolio: pd.DataFrame, config: CreditRiskConfig) -> pd.Series:
    """Calculate Loss Given Default per account.

    Priority:
    1. If `lgd` column exists -> use it
    2. Default LGD from config
    """
    if "lgd" in portfolio.columns:
        lgd = pd.to_numeric(portfolio["lgd"], errors="coerce")
        lgd = lgd.clip(0.0, 1.0).fillna(config.default_lgd)
        return lgd
    return pd.Series(config.default_lgd, index=portfolio.index, dtype=float)


def _calculate_ead(
    portfolio: pd.DataFrame,
    config: CreditRiskConfig,
) -> pd.Series:
    """Calculate Exposure at Default per account.

    EAD = balance + CCF * max(0, limit_amount - balance)
    If `ead` column exists, use it directly.
    """
    if "ead" in portfolio.columns:
        ead = pd.to_numeric(portfolio["ead"], errors="coerce")
        return ead.clip(lower=0.0).fillna(0.0)

    balance = pd.to_numeric(portfolio.get("balance", 0), errors="coerce").fillna(0.0)
    limit = pd.to_numeric(portfolio.get("limit_amount", 0), errors="coerce").fillna(0.0)

    # Undrawn = max(0, limit - balance) for credit lines
    undrawn = (limit - balance).clip(lower=0.0)
    ead = balance + config.ccf * undrawn

    return ead.clip(lower=0.0)


def calculate_credit_risk(
    portfolio: pd.DataFrame,
    config: CreditRiskConfig | None = None,
) -> dict:
    """Calculate full credit risk metrics for portfolio.

    Returns dict with:
    - pd: PD per account
    - lgd: LGD per account
    - ead: EAD per account
    - expected_loss: PD * LGD * EAD per account
    - portfolio_pd: weighted average PD
    - portfolio_lgd: weighted average LGD
    - total_expected_loss: sum of expected losses
    - rwa: Risk-Weighted Assets (Basel II/III formula)
    - capital_requirement: 8% of RWA (Basel minimum)
    - summary_by_product: breakdown by product_code
    - summary_by_bucket: breakdown by PAR bucket
    """
    if portfolio is None or portfolio.empty:
        warnings.warn("calculate_credit_risk: empty portfolio")
        return {
            "available": False,
            "assumption": "Portfolio table is empty.",
        }

    if config is None:
        config = CreditRiskConfig()

    df = portfolio.copy()

    # Ensure required columns
    if "account_id" not in df.columns:
        warnings.warn("credit_risk: account_id column missing")
        return {"available": False, "assumption": "Missing account_id column."}

    # Calculate components
    pd_series = _calculate_pd(df, config)
    lgd_series = _calculate_lgd(df, config)
    ead_series = _calculate_ead(df, config)

    # Expected Loss per account
    el_series = pd_series * lgd_series * ead_series

    # Portfolio-level metrics (weighted by EAD)
    total_ead = ead_series.sum()
    if total_ead > 0:
        portfolio_pd = float((pd_series * ead_series).sum() / total_ead)
        portfolio_lgd = float((lgd_series * ead_series).sum() / total_ead)
    else:
        portfolio_pd = config.default_pd
        portfolio_lgd = config.default_lgd

    total_expected_loss = float(el_series.sum())

    # RWA calculation (Basel II/III IRB formula simplified)
    # RWA = EAD * K * 12.5, where K = LGD * N[(1-R)^-0.5 * G(PD) + (R/(1-R))^0.5 * G(0.999)] - PD*LGD
    # Simplified: use Vasicek formula
    from scipy.stats import norm

    def vasicek_k(pd: float, lgd: float, rho: float) -> float:
        """Capital requirement K per unit of exposure (Basel II IRB)."""
        if pd <= 0 or pd >= 1:
            return 0.0
        z = norm.ppf(0.999)
        g_pd = norm.ppf(pd)
        k = lgd * norm.cdf((1 - rho) ** -0.5 * g_pd + (rho / (1 - rho)) ** 0.5 * z) - pd * lgd
        return max(k, 0.0)

    # Calculate RWA per account and sum
    rwa_per_account = ead_series * pd_series.apply(lambda x: vasicek_k(x, config.default_lgd, config.asset_correlation)) * 12.5
    total_rwa = float(rwa_per_account.sum())
    capital_requirement = total_rwa * 0.08  # 8% minimum capital

    # Summary by product
    summary_by_product = None
    if "product_code" in df.columns:
        prod_df = pd.DataFrame({
            "product_code": df["product_code"],
            "pd": pd_series,
            "lgd": lgd_series,
            "ead": ead_series,
            "el": el_series,
            "rwa": rwa_per_account,
        })
        summary_by_product = prod_df.groupby("product_code").agg(
            accounts=("ead", "size"),
            total_ead=("ead", "sum"),
            avg_pd=("pd", lambda x: np.average(x, weights=prod_df.loc[x.index, "ead"])),
            avg_lgd=("lgd", lambda x: np.average(x, weights=prod_df.loc[x.index, "ead"])),
            total_el=("el", "sum"),
            total_rwa=("rwa", "sum"),
        ).round(4).reset_index().to_dict("records")

    # Summary by PAR bucket
    summary_by_bucket = None
    if "par_bucket" in df.columns:
        bucket_df = pd.DataFrame({
            "par_bucket": df["par_bucket"],
            "pd": pd_series,
            "lgd": lgd_series,
            "ead": ead_series,
            "el": el_series,
            "rwa": rwa_per_account,
        })
        summary_by_bucket = bucket_df.groupby("par_bucket").agg(
            accounts=("ead", "size"),
            total_ead=("ead", "sum"),
            avg_pd=("pd", lambda x: np.average(x, weights=bucket_df.loc[x.index, "ead"])),
            avg_lgd=("lgd", lambda x: np.average(x, weights=bucket_df.loc[x.index, "ead"])),
            total_el=("el", "sum"),
            total_rwa=("rwa", "sum"),
        ).round(4).reset_index().to_dict("records")

    return {
        "available": True,
        "account_level": {
            "pd": pd_series.round(6).to_dict(),
            "lgd": lgd_series.round(4).to_dict(),
            "ead": ead_series.round(2).to_dict(),
            "expected_loss": el_series.round(2).to_dict(),
        },
        "portfolio": {
            "portfolio_pd": round(portfolio_pd, 6),
            "portfolio_lgd": round(portfolio_lgd, 4),
            "total_ead": round(total_ead, 2),
            "total_expected_loss": round(total_expected_loss, 2),
            "total_rwa": round(total_rwa, 2),
            "capital_requirement": round(capital_requirement, 2),
            "el_ratio": round(total_expected_loss / total_ead if total_ead > 0 else 0, 6),
        },
        "summary_by_product": summary_by_product,
        "summary_by_bucket": summary_by_bucket,
        "assumption": (
            f"PD: score calibration (a={config.pd_calibration_a}, b={config.pd_calibration_b}) "
            f"or default_flag/dpd>=90 -> 1.0, else default {config.default_pd}. "
            f"LGD: column or default {config.default_lgd}. "
            f"EAD: balance + {config.ccf}*undrawn or column. "
            f"RWA: Basel IRB (rho={config.asset_correlation}, M={config.maturity})."
        ),
    }


def credit_risk_summary_frame(result: dict) -> pd.DataFrame:
    """Format credit risk result as DataFrame for reporting."""
    if not result.get("available"):
        return pd.DataFrame()

    port = result["portfolio"]
    rows = [
        {"Метрика": "Средний PD (EAD-weighted)", "Значение": f"{port['portfolio_pd']:.4%}", "Ед.": ""},
        {"Метрика": "Средний LGD (EAD-weighted)", "Значение": f"{port['portfolio_lgd']:.2%}", "Ед.": ""},
        {"Метрика": "Общий EAD", "Значение": f"{port['total_ead']:,.0f}", "Ед.": "руб."},
        {"Метрика": "Ожидаемые потери (EL)", "Значение": f"{port['total_expected_loss']:,.0f}", "Ед.": "руб."},
        {"Метрика": "Отношение EL / EAD", "Значение": f"{port['el_ratio']:.4%}", "Ед.": ""},
        {"Метрика": "RWA (Basel IRB)", "Значение": f"{port['total_rwa']:,.0f}", "Ед.": "руб."},
        {"Метрика": "Требование к капиталу (8%)", "Значение": f"{port['capital_requirement']:,.0f}", "Ед.": "руб."},
    ]
    return pd.DataFrame(rows)

