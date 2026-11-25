"""Migration matrices and vintage analysis for portfolio risk management.

Migration matrix: transitions of PAR buckets between two portfolio snapshots.
Vintage analysis: balance and delinquency share by origination month (open_date).
"""

from __future__ import annotations

import warnings
from typing import Literal

import numpy as np
import pandas as pd

# Default PAR buckets as defined in metric-contracts.md
DEFAULT_BUCKETS = ["current", "30+", "60+", "90+"]

BucketMode = Literal["clients", "balance"]


def _assign_bucket(series: pd.Series) -> pd.Series:
    """Map dpd values to PAR bucket labels."""
    conditions = [
        series < 30,
        (series >= 30) & (series < 60),
        (series >= 60) & (series < 90),
        series >= 90,
    ]
    choices = DEFAULT_BUCKETS
    return pd.Series(np.select(conditions, choices, default="current"), index=series.index)


def _prepare_snapshot(
    portfolio: pd.DataFrame,
    snapshot_date: str | pd.Timestamp | None = None,
) -> pd.DataFrame:
    """Prepare a portfolio snapshot as of a given date.

    Filters accounts active at snapshot_date (open_date <= snapshot_date < close_date or no close_date).
    """
    df = portfolio.copy()

    # Parse dates
    for col in ("open_date", "close_date"):
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce")

    if snapshot_date is not None:
        snap = pd.Timestamp(snapshot_date)
        mask_open = df["open_date"] <= snap if "open_date" in df.columns else True
        if "close_date" in df.columns:
            mask_close = (df["close_date"] > snap) | df["close_date"].isna()
        else:
            mask_close = True
        df = df[mask_open & mask_close].copy()

    # Ensure required columns
    if "dpd" not in df.columns:
        warnings.warn("migration: column 'dpd' not found, assuming 0")
        df["dpd"] = 0
    df["dpd"] = pd.to_numeric(df["dpd"], errors="coerce").fillna(0)

    if "balance" not in df.columns:
        warnings.warn("migration: column 'balance' not found, assuming 0")
        df["balance"] = 0
    df["balance"] = pd.to_numeric(df["balance"], errors="coerce").fillna(0)

    if "client_id" not in df.columns:
        warnings.warn("migration: column 'client_id' not found")
        df["client_id"] = np.nan

    # Assign PAR bucket
    df["par_bucket"] = _assign_bucket(df["dpd"])
    return df


def migration_matrix(
    base: pd.DataFrame,
    target: pd.DataFrame,
    buckets: list[str] | None = None,
    mode: BucketMode = "clients",
) -> pd.DataFrame:
    """Compute migration matrix between two portfolio snapshots.

    Args:
        base: Base portfolio snapshot (DataFrame with account_id, client_id, dpd, balance).
        target: Target portfolio snapshot (same columns).
        buckets: Ordered list of bucket labels. Default: current, 30+, 60+, 90+.
        mode: "clients" — share of unique clients, "balance" — share of balance.

    Returns:
        DataFrame with rows = base buckets, columns = target buckets, values = %.
        Index and columns match `buckets`. Missing buckets get 0.0.
    """
    if buckets is None:
        buckets = DEFAULT_BUCKETS

    if base.empty or target.empty:
        warnings.warn("migration_matrix: empty base or target portfolio")
        return pd.DataFrame(0.0, index=buckets, columns=buckets, dtype=float)

    # Prepare snapshots
    base_prep = _prepare_snapshot(base)
    target_prep = _prepare_snapshot(target)

    # Join on client_id (track client migration)
    # Use client_id as key; if multiple accounts per client, take worst bucket
    def worst_bucket(grp: pd.DataFrame) -> str:
        order = {b: i for i, b in enumerate(buckets)}
        return max(grp["par_bucket"], key=lambda b: order.get(b, -1))

    base_client = base_prep.groupby("client_id").apply(worst_bucket).rename("base_bucket")
    target_client = target_prep.groupby("client_id").apply(worst_bucket).rename("target_bucket")

    joined = pd.DataFrame({"base_bucket": base_client, "target_bucket": target_client}).dropna()

    if mode == "balance":
        # For balance mode, aggregate balance per client per snapshot then assign bucket
        base_prep.groupby("client_id")["balance"].sum()
        target_prep.groupby("client_id")["balance"].sum()

        base_bal_bucket = base_prep.assign(balance=base_prep["balance"]).groupby("client_id").apply(
            lambda g: max(g["par_bucket"], key=lambda b: {b: i for i, b in enumerate(buckets)}.get(b, -1))
        )
        target_bal_bucket = target_prep.assign(balance=target_prep["balance"]).groupby("client_id").apply(
            lambda g: max(g["par_bucket"], key=lambda b: {b: i for i, b in enumerate(buckets)}.get(b, -1))
        )

        base_weights = base_prep.groupby("client_id")["balance"].sum()
        target_weights = target_prep.groupby("client_id")["balance"].sum()

        joined = pd.DataFrame({
            "base_bucket": base_bal_bucket,
            "target_bucket": target_bal_bucket,
            "base_weight": base_weights,
            "target_weight": target_weights,
        }).dropna()

        # Use base weight for row normalization
        weights = joined["base_weight"]
    else:
        # Client count mode
        weights = pd.Series(1.0, index=joined.index)

    # Build crosstab
    crosstab = pd.crosstab(
        joined["base_bucket"],
        joined["target_bucket"],
        values=weights,
        aggfunc="sum",
        normalize="index",
    ) * 100

    # Ensure all buckets present
    crosstab = crosstab.reindex(index=buckets, columns=buckets, fill_value=0.0)
    crosstab = crosstab.round(2)

    # Row sums should be 100 (or 0 for empty rows)
    row_sums = crosstab.sum(axis=1)
    for bucket in buckets:
        if row_sums.get(bucket, 0) == 0:
            crosstab.loc[bucket] = 0.0

    return crosstab


def vintage_analysis(
    portfolio: pd.DataFrame,
    clients: pd.DataFrame | None = None,
    as_of: str | pd.Timestamp | None = None,
) -> pd.DataFrame:
    """Vintage analysis: balance and delinquency by origination month.

    Args:
        portfolio: Portfolio DataFrame with open_date, balance, dpd, client_id, status.
        clients: Optional clients DataFrame with registration_date.
        as_of: Analysis date. Defaults to max open_date in portfolio.

    Returns:
        DataFrame indexed by vintage (YYYY-MM) with columns:
        - accounts: number of accounts opened in that month
        - clients: number of unique clients
        - balance: total balance as of as_of
        - par30_pct: % of balance with dpd >= 30
        - par60_pct: % of balance with dpd >= 60
        - par90_pct: % of balance with dpd >= 90
        - active_rate: % of accounts with balance > 0 (not closed)
    """
    if portfolio is None or portfolio.empty:
        warnings.warn("vintage_analysis: empty portfolio")
        return pd.DataFrame(
            columns=["accounts", "clients", "balance", "par30_pct", "par60_pct", "par90_pct", "active_rate"],
            dtype=float,
        )

    df = portfolio.copy()

    # Parse dates
    for col in ("open_date", "close_date"):
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce")

    # Determine as_of date
    if as_of is None:
        as_of = df["open_date"].max() if "open_date" in df.columns and df["open_date"].notna().any() else pd.Timestamp.now()
    as_of = pd.Timestamp(as_of)

    # Filter to accounts opened on or before as_of
    if "open_date" in df.columns:
        df = df[df["open_date"] <= as_of].copy()
    else:
        warnings.warn("vintage_analysis: no open_date column")
        return pd.DataFrame(
            columns=["accounts", "clients", "balance", "par30_pct", "par60_pct", "par90_pct", "active_rate"],
            dtype=float,
        )

    if df.empty:
        return pd.DataFrame(
            columns=["accounts", "clients", "balance", "par30_pct", "par60_pct", "par90_pct", "active_rate"],
            dtype=float,
        )

    # Vintage = month of open_date
    df["vintage"] = df["open_date"].dt.to_period("M").astype(str)

    # Active accounts: balance > 0 and not closed before as_of
    if "close_date" in df.columns:
        df["is_active"] = (df["close_date"] > as_of) | df["close_date"].isna()
    else:
        df["is_active"] = True

    if "balance" not in df.columns:
        warnings.warn("vintage_analysis: no balance column")
        df["balance"] = 0
    df["balance"] = pd.to_numeric(df["balance"], errors="coerce").fillna(0)

    if "dpd" not in df.columns:
        warnings.warn("vintage_analysis: no dpd column")
        df["dpd"] = 0
    df["dpd"] = pd.to_numeric(df["dpd"], errors="coerce").fillna(0)

    if "client_id" not in df.columns:
        warnings.warn("vintage_analysis: no client_id column")
        df["client_id"] = np.nan

    # Aggregate by vintage
    grouped = df.groupby("vintage", dropna=False)

    result = pd.DataFrame({
        "accounts": grouped.size(),
        "clients": grouped["client_id"].nunique(),
        "balance": grouped["balance"].sum(min_count=1),
    })

    # Delinquency shares
    total_bal = result["balance"].replace(0, np.nan)
    for days, col in [(30, "par30_pct"), (60, "par60_pct"), (90, "par90_pct")]:
        overdue = df[df["dpd"] >= days].groupby("vintage")["balance"].sum()
        result[col] = (overdue.reindex(result.index).fillna(0) / total_bal * 100).round(2)

    # Active rate: accounts with balance > 0 and not closed
    active = df[(df["balance"] > 0) & (df["is_active"])].groupby("vintage").size()
    result["active_rate"] = (active.reindex(result.index).fillna(0) / result["accounts"] * 100).round(2)

    # Sort by vintage
    result = result.sort_index()

    # Fill NaN with 0 for numeric columns
    for col in ["accounts", "clients", "balance", "par30_pct", "par60_pct", "par90_pct", "active_rate"]:
        result[col] = result[col].fillna(0)

    result.index.name = "vintage"
    return result
