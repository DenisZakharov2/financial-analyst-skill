"""Сегментация клиентской базы: детерминированная матрица RFM + Risk и кластеризация.

Основной разрез — детерминированная бизнес-сегментация по матрице RFM + Risk:
квантильные грейды Recency, Frequency, Monetary в сочетании с флагом просрочки
дают понятные аналитику сегменты. Дополнительно (по запросу) строится
робастная кластеризация K-Means++ на `log1p`-признаках с масштабированием по
IQR: так крупные остатки не вытягивают отдельный кластер.

Кластеризация не влияет на бизнес-сегменты: это второй, независимый разрез.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

SEGMENTS = (
    "VIP / Активное ядро",
    "Растущие мультипродуктовые",
    "Крупные под риском оттока",
    "Массовый базовый",
    "Неактивные / Спящие",
    "Просроченные (DPD > 0)",
)


def _quantile_score(series: pd.Series, bins: int = 3, invert: bool = False) -> pd.Series:
    """Устойчивый к дубликатам квантильный скор от 1 до `bins`."""
    ranks = series.rank(method="average", pct=True)
    if invert:
        ranks = 1.0 - ranks
    scores = np.ceil(ranks * bins).clip(1, bins).astype(int)
    return pd.Series(scores, index=series.index)


def _kmeans_plus_plus(X: np.ndarray, k: int, seed: int = 42, max_iter: int = 50) -> np.ndarray:
    """Детерминированный K-Means++ на чистом NumPy без внешних зависимостей."""
    rng = np.random.default_rng(seed)
    n = X.shape[0]
    if n <= k:
        return np.arange(n, dtype=int)

    centers = np.empty((k, X.shape[1]), dtype=float)
    centers[0] = X[int(rng.integers(0, n))]

    for c_idx in range(1, k):
        dist_sq = np.min(np.sum((X[:, None, :] - centers[None, :c_idx, :]) ** 2, axis=2), axis=1)
        total_d = float(np.sum(dist_sq))
        if total_d <= 1e-12:
            next_idx = int(rng.integers(0, n))
        else:
            next_idx = int(rng.choice(n, p=dist_sq / total_d))
        centers[c_idx] = X[next_idx]

    labels = np.zeros(n, dtype=int)
    for _ in range(max_iter):
        dists = np.sum((X[:, None, :] - centers[None, :, :]) ** 2, axis=2)
        new_labels = np.argmin(dists, axis=1)
        if np.array_equal(new_labels, labels):
            break
        labels = new_labels
        for c_idx in range(k):
            mask = labels == c_idx
            if np.any(mask):
                centers[c_idx] = np.mean(X[mask], axis=0)
    return labels


def _silhouette_score_np(X: np.ndarray, labels: np.ndarray) -> float:
    """Средний коэффициент силуэта для оценки качества разбиения."""
    unique_labels = np.unique(labels)
    if len(unique_labels) < 2 or len(X) < 4:
        return -1.0

    # Подвыборка 400 точек: расчёт O(N^2) остаётся мгновенным.
    if len(X) > 400:
        idx = np.linspace(0, len(X) - 1, 400, dtype=int)
        X = X[idx]
        labels = labels[idx]

    diff = X[:, None, :] - X[None, :, :]
    dist_mat = np.sqrt(np.sum(diff**2, axis=2))
    n = len(X)
    sil = np.zeros(n, dtype=float)

    for i in range(n):
        own_cluster = labels[i]
        own_mask = (labels == own_cluster) & (np.arange(n) != i)
        a_i = float(np.mean(dist_mat[i, own_mask])) if np.any(own_mask) else 0.0

        b_i = float("inf")
        for other_c in unique_labels:
            if other_c == own_cluster:
                continue
            other_mask = labels == other_c
            if np.any(other_mask):
                b_i = min(b_i, float(np.mean(dist_mat[i, other_mask])))
        denom = max(a_i, b_i)
        sil[i] = (b_i - a_i) / denom if denom > 1e-12 else 0.0
    return float(np.mean(sil))


def segment_clients(
    clients: pd.DataFrame,
    portfolio: pd.DataFrame,
    sales: pd.DataFrame | None = None,
    reference_date: str | pd.Timestamp | None = None,
    enable_clustering: bool = True,
) -> dict:
    """Построить профиль клиентских сегментов по матрице RFM + Risk и кластеризации.

    Возвращает профили сегментов (число клиентов, остаток, доли, доля
    просроченных) и поразъектное распределение по сегментам и кластерам.
    """
    if clients is None or clients.empty or "client_id" not in clients.columns:
        return {
            "available": False,
            "profiles": pd.DataFrame(),
            "client_assignments": pd.DataFrame(),
            "assumption": "Таблица clients пуста или не содержит client_id.",
        }
    if portfolio is None or portfolio.empty or "client_id" not in portfolio.columns:
        return {
            "available": False,
            "profiles": pd.DataFrame(),
            "client_assignments": pd.DataFrame(),
            "assumption": "Таблица portfolio пуста или не содержит client_id.",
        }

    port = portfolio.copy()
    port["bal"] = pd.to_numeric(port.get("balance", 0.0), errors="coerce").fillna(0.0).clip(lower=0.0)
    port["dpd_val"] = pd.to_numeric(port.get("dpd", 0.0), errors="coerce").fillna(0.0)
    port["open_dt"] = pd.to_datetime(port.get("open_date"), errors="coerce")

    ref_dt = (
        pd.to_datetime(reference_date)
        if reference_date is not None
        else (port["open_dt"].max() if port["open_dt"].notna().any() else pd.Timestamp("2025-01-01"))
    )

    port_agg = port.groupby("client_id", as_index=False).agg(
        total_balance=("bal", "sum"),
        accounts_count=("bal", "count"),
        products_count=("product_code", "nunique") if "product_code" in port.columns else ("bal", "count"),
        max_dpd=("dpd_val", "max"),
        last_open_dt=("open_dt", "max"),
    )

    base = clients[["client_id"]].drop_duplicates().merge(port_agg, on="client_id", how="left")
    base["total_balance"] = base["total_balance"].fillna(0.0)
    base["accounts_count"] = base["accounts_count"].fillna(0).astype(int)
    base["products_count"] = base["products_count"].fillna(0).astype(int)
    base["max_dpd"] = base["max_dpd"].fillna(0.0)

    if sales is not None and not sales.empty and "client_id" in sales.columns:
        s_tmp = sales.copy()
        date_col = "stage_date" if "stage_date" in s_tmp.columns else "application_date"
        if date_col in s_tmp.columns:
            s_tmp["s_dt"] = pd.to_datetime(s_tmp[date_col], errors="coerce")
            s_agg = s_tmp.groupby("client_id", as_index=False).agg(last_sale_dt=("s_dt", "max"))
            base = base.merge(s_agg, on="client_id", how="left")
            base["last_activity_dt"] = base[["last_open_dt", "last_sale_dt"]].max(axis=1)
        else:
            base["last_activity_dt"] = base["last_open_dt"]
    else:
        base["last_activity_dt"] = base["last_open_dt"]

    recency_days = (ref_dt - base["last_activity_dt"]).dt.days.fillna(365).clip(lower=0).astype(float)
    base["recency_days"] = recency_days

    r_score = _quantile_score(base["recency_days"], bins=3, invert=True)
    f_score = _quantile_score(base["accounts_count"] + base["products_count"], bins=3, invert=False)
    m_score = _quantile_score(base["total_balance"], bins=3, invert=False)

    segments: list[str] = []
    for idx in range(len(base)):
        dpd_i = float(base["max_dpd"].iloc[idx])
        bal_i = float(base["total_balance"].iloc[idx])
        r_i = int(r_score.iloc[idx])
        f_i = int(f_score.iloc[idx])
        m_i = int(m_score.iloc[idx])

        if dpd_i > 0:
            segments.append("Просроченные (DPD > 0)")
        elif bal_i <= 0 and int(base["accounts_count"].iloc[idx]) == 0:
            segments.append("Неактивные / Спящие")
        elif m_i == 3 and r_i >= 2:
            segments.append("VIP / Активное ядро")
        elif m_i == 3 and r_i == 1:
            segments.append("Крупные под риском оттока")
        elif r_i >= 2 and f_i >= 2:
            segments.append("Растущие мультипродуктовые")
        else:
            segments.append("Массовый базовый")

    base["rfm_segment"] = segments

    best_k = 1
    best_sil = -1.0
    if enable_clustering and len(base) >= 10:
        feats = np.column_stack([
            np.log1p(base["total_balance"].to_numpy(dtype=float)),
            np.log1p(base["recency_days"].to_numpy(dtype=float)),
            base["products_count"].to_numpy(dtype=float),
            np.log1p(base["max_dpd"].to_numpy(dtype=float)),
        ])
        med = np.median(feats, axis=0)
        q75 = np.percentile(feats, 75, axis=0)
        q25 = np.percentile(feats, 25, axis=0)
        iqr = np.where((q75 - q25) > 1e-6, q75 - q25, 1.0)
        X_scaled = (feats - med) / iqr

        best_labels = np.zeros(len(base), dtype=int)
        min_cluster_size = max(2, int(0.03 * len(base)))
        for k_cand in (2, 3, 4, 5):
            if len(base) < k_cand * 3:
                continue
            cand_labels = _kmeans_plus_plus(X_scaled, k=k_cand, seed=42)
            _, counts = np.unique(cand_labels, return_counts=True)
            if int(np.min(counts)) < min_cluster_size:
                continue
            sil = _silhouette_score_np(X_scaled, cand_labels)
            if sil > best_sil:
                best_sil = sil
                best_k = k_cand
                best_labels = cand_labels
        base["cluster_id"] = [f"CL-{int(c) + 1}" for c in best_labels]
    else:
        base["cluster_id"] = "CL-1"

    total_clients = len(base)
    total_port = float(base["total_balance"].sum()) or 1.0
    profiles = (
        base.groupby("rfm_segment", as_index=False)
        .agg(
            clients_count=("client_id", "count"),
            portfolio_balance=("total_balance", "sum"),
            mean_balance=("total_balance", "mean"),
            avg_products=("products_count", "mean"),
            median_recency_days=("recency_days", "median"),
            delinquent_share_pct=("max_dpd", lambda s: float(np.mean(s > 0) * 100.0)),
        )
        .sort_values("portfolio_balance", ascending=False)
        .reset_index(drop=True)
    )
    profiles["clients_share_pct"] = (profiles["clients_count"] / total_clients * 100.0).round(2)
    profiles["portfolio_share_pct"] = (profiles["portfolio_balance"] / total_port * 100.0).round(2)
    for column in ("portfolio_balance", "mean_balance", "avg_products",
                   "median_recency_days", "delinquent_share_pct"):
        profiles[column] = profiles[column].round(2)

    return {
        "available": True,
        "profiles": profiles,
        "client_assignments": base[
            ["client_id", "rfm_segment", "cluster_id", "total_balance",
             "products_count", "recency_days", "max_dpd"]
        ],
        "optimal_k": int(best_k),
        "silhouette_score": round(float(best_sil), 4) if best_sil > -1.0 else None,
        "assumption": (
            "Сегментация построена по детерминированной матрице RFM + Risk и дополнена "
            f"робастной кластеризацией K-Means++ на log1p/IQR-признаках (выбрано k={best_k}, "
            "ограничение размера кластера >= 3%)."
        ),
    }
