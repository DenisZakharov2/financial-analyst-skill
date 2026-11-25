"""Tests for credit_risk module."""

from __future__ import annotations

import pandas as pd
import pytest
from finanalyst.credit_risk import (
    CreditRiskConfig,
    _calculate_ead,
    _calculate_lgd,
    _calculate_pd,
    _score_to_pd,
    calculate_credit_risk,
    credit_risk_summary_frame,
)


def test_score_to_pd():
    """Test logistic score to PD conversion."""
    scores = pd.Series([300, 600, 900])
    a, b = -3.0, 0.01
    pds = _score_to_pd(scores, a, b)

    # Higher score -> lower PD
    assert pds.iloc[0] > pds.iloc[1] > pds.iloc[2]
    # PD in (0, 1)
    assert (pds > 0).all() and (pds < 1).all()
    # Rough values: score 300 ~ 0.5, score 600 ~ 0.05, score 900 ~ 0.001
    assert pds.iloc[0] > 0.1
    assert pds.iloc[1] < 0.2
    assert pds.iloc[2] < 0.01


def test_calculate_pd_with_default_flag():
    """Test PD calculation with default_flag column."""
    portfolio = pd.DataFrame({
        "account_id": ["A1", "A2", "A3"],
        "balance": [1000, 2000, 3000],
        "default_flag": [1, 0, 0],
    })
    config = CreditRiskConfig(default_pd=0.05)
    pd_series = _calculate_pd(portfolio, config)

    assert pd_series.iloc[0] == 1.0  # defaulted
    assert pd_series.iloc[1] == 0.05
    assert pd_series.iloc[2] == 0.05


def test_calculate_pd_with_dpd():
    """Test PD calculation with dpd >= 90."""
    portfolio = pd.DataFrame({
        "account_id": ["A1", "A2", "A3"],
        "balance": [1000, 2000, 3000],
        "dpd": [95, 30, 0],
    })
    config = CreditRiskConfig(default_pd=0.05)
    pd_series = _calculate_pd(portfolio, config)

    assert pd_series.iloc[0] == 1.0  # dpd >= 90
    assert pd_series.iloc[1] == 0.05
    assert pd_series.iloc[2] == 0.05


def test_calculate_pd_with_score():
    """Test PD calculation with score column."""
    portfolio = pd.DataFrame({
        "account_id": ["A1", "A2"],
        "balance": [1000, 2000],
        "score": [600, 800],
    })
    config = CreditRiskConfig(default_pd=0.05, pd_calibration_a=-3.0, pd_calibration_b=0.01)
    pd_series = _calculate_pd(portfolio, config)

    # Higher score -> lower PD
    assert pd_series.iloc[0] > pd_series.iloc[1]
    assert (pd_series < 1.0).all()


def test_calculate_lgd_with_column():
    """Test LGD calculation with lgd column."""
    portfolio = pd.DataFrame({
        "account_id": ["A1", "A2"],
        "balance": [1000, 2000],
        "lgd": [0.3, 0.6],
    })
    config = CreditRiskConfig(default_lgd=0.45)
    lgd_series = _calculate_lgd(portfolio, config)

    assert lgd_series.iloc[0] == 0.3
    assert lgd_series.iloc[1] == 0.6


def test_calculate_lgd_default():
    """Test LGD calculation with default."""
    portfolio = pd.DataFrame({
        "account_id": ["A1", "A2"],
        "balance": [1000, 2000],
    })
    config = CreditRiskConfig(default_lgd=0.45)
    lgd_series = _calculate_lgd(portfolio, config)

    assert lgd_series.iloc[0] == 0.45
    assert lgd_series.iloc[1] == 0.45


def test_calculate_ead_with_column():
    """Test EAD calculation with ead column."""
    portfolio = pd.DataFrame({
        "account_id": ["A1", "A2"],
        "balance": [1000, 2000],
        "ead": [1500, 2500],
    })
    config = CreditRiskConfig(ccf=0.5)
    ead_series = _calculate_ead(portfolio, config)

    assert ead_series.iloc[0] == 1500
    assert ead_series.iloc[1] == 2500


def test_calculate_ead_from_balance_limit():
    """Test EAD calculation from balance and limit."""
    portfolio = pd.DataFrame({
        "account_id": ["A1", "A2", "A3"],
        "balance": [1000, 2000, 3000],
        "limit_amount": [2000, 2000, 5000],  # A1: undrawn=1000, A2: undrawn=0, A3: undrawn=2000
    })
    config = CreditRiskConfig(ccf=0.5)
    ead_series = _calculate_ead(portfolio, config)

    # EAD = balance + 0.5 * max(0, limit - balance)
    assert ead_series.iloc[0] == 1000 + 0.5 * 1000  # 1500
    assert ead_series.iloc[1] == 2000  # no undrawn
    assert ead_series.iloc[2] == 3000 + 0.5 * 2000  # 4000


def test_calculate_credit_risk_empty():
    """Test credit risk with empty portfolio."""
    result = calculate_credit_risk(pd.DataFrame())
    assert result["available"] is False


def test_calculate_credit_risk_basic():
    """Test basic credit risk calculation."""
    portfolio = pd.DataFrame({
        "account_id": ["A1", "A2", "A3"],
        "client_id": ["C1", "C2", "C3"],
        "product_code": ["CC-01", "CC-01", "LN-01"],
        "balance": [100000, 200000, 300000],
        "limit_amount": [150000, 200000, 500000],
        "dpd": [0, 45, 120],
        "par_bucket": ["current", "30+", "90+"],
        "score": [720, 650, 500],
        "default_flag": [0, 0, 1],
        "lgd": [0.4, 0.45, 0.5],
    })

    config = CreditRiskConfig(default_pd=0.02, default_lgd=0.45)
    result = calculate_credit_risk(portfolio, config)

    assert result["available"] is True
    assert "portfolio" in result
    assert result["portfolio"]["total_ead"] > 0
    assert result["portfolio"]["total_expected_loss"] >= 0
    assert result["portfolio"]["total_rwa"] >= 0
    assert result["portfolio"]["capital_requirement"] >= 0


def test_calculate_credit_risk_with_product_breakdown():
    """Test credit risk with product breakdown."""
    portfolio = pd.DataFrame({
        "account_id": ["A1", "A2", "A3"],
        "client_id": ["C1", "C2", "C3"],
        "product_code": ["CC-01", "CC-01", "LN-01"],
        "balance": [100000, 200000, 300000],
        "limit_amount": [150000, 200000, 500000],
        "dpd": [0, 30, 90],
        "par_bucket": ["current", "30+", "90+"],
        "score": [720, 650, 500],
    })

    result = calculate_credit_risk(portfolio)

    assert result["available"] is True
    assert result["summary_by_product"] is not None
    assert len(result["summary_by_product"]) == 2  # CC-01 and LN-01

    # Check structure
    for prod in result["summary_by_product"]:
        assert "product_code" in prod
        assert "accounts" in prod
        assert "total_ead" in prod
        assert "avg_pd" in prod
        assert "avg_lgd" in prod
        assert "total_el" in prod
        assert "total_rwa" in prod


def test_calculate_credit_risk_with_bucket_breakdown():
    """Test credit risk with PAR bucket breakdown."""
    portfolio = pd.DataFrame({
        "account_id": ["A1", "A2", "A3"],
        "client_id": ["C1", "C2", "C3"],
        "product_code": ["CC-01", "CC-01", "LN-01"],
        "balance": [100000, 200000, 300000],
        "limit_amount": [150000, 250000, 500000],
        "dpd": [0, 45, 120],
        "par_bucket": ["current", "30+", "90+"],
    })

    result = calculate_credit_risk(portfolio)

    assert result["summary_by_bucket"] is not None
    assert len(result["summary_by_bucket"]) == 3  # current, 30+, 90+


def test_credit_risk_summary_frame():
    """Test summary frame formatting."""
    result = {
        "available": True,
        "portfolio": {
            "portfolio_pd": 0.025,
            "portfolio_lgd": 0.42,
            "total_ead": 15000000,
            "total_expected_loss": 157500,
            "el_ratio": 0.0105,
            "total_rwa": 5000000,
            "capital_requirement": 400000,
        },
    }

    frame = credit_risk_summary_frame(result)

    assert not frame.empty
    assert "Метрика" in frame.columns
    assert "Значение" in frame.columns
    assert len(frame) == 7


def test_credit_risk_summary_frame_unavailable():
    """Test summary frame when unavailable."""
    result = {"available": False}
    frame = credit_risk_summary_frame(result)
    assert frame.empty


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

