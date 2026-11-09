"""Расчёт поэтапного времени прохождения воронки (TAT) и декомпозиции сезонности.

Модуль предназначен для пакета `finanalyst` (skills/financial-analyst/scripts/finanalyst/seasonality_tat.py).
Согласован со справочником `references/data-dictionary.md`, `STAGE_ORDER` из `metrics.py`
и реальной структурой `assets/sample/funnel_events.csv`, `sales.csv` и `finance.csv`.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

STAGE_ORDER: list[str] = ["лид", "обращение", "заявка", "одобрение", "договор", "активация"]
STAGE_RANK: dict[str, int] = {stage: idx + 1 for idx, stage in enumerate(STAGE_ORDER)}
I_NAT = np.iinfo(np.int64).min


def _normalize_stage(value: object) -> str:
    if pd.isna(value):
        return ""
    return str(value).strip().lower()


def _parse_dates_to_ns(date_strings: np.ndarray) -> np.ndarray:
    """Parse date strings directly to int64 nanoseconds using numpy datetime64.
    
    Avoids pandas datetime64 segfault bug in pandas 2.2.3 + numpy 2.1.3.
    Invalid dates become I_NAT.
    """
    result = np.full(len(date_strings), I_NAT, dtype="int64")
    for i, d in enumerate(date_strings):
        if pd.isna(d) or d == "":
            continue
        try:
            # Parse as numpy datetime64[ns] then convert to int64
            dt = np.datetime64(str(d), "ns")
            result[i] = dt.astype("int64")
        except (ValueError, TypeError):
            pass
    return result


def _ns_to_datetime_str(ns_array: np.ndarray) -> np.ndarray:
    """Convert int64 nanoseconds to YYYY-MM-DD string array."""
    result = np.full(len(ns_array), "", dtype=object)
    for i, ns in enumerate(ns_array):
        if ns != I_NAT:
            dt = np.datetime64(int(ns), "ns")
            result[i] = str(dt)[:10]  # YYYY-MM-DD
    return result


def _ns_to_month(ns_array: np.ndarray) -> np.ndarray:
    """Extract month (1-12) from int64 nanoseconds."""
    result = np.zeros(len(ns_array), dtype=int)
    for i, ns in enumerate(ns_array):
        if ns != I_NAT:
            dt = np.datetime64(int(ns), "ns")
            result[i] = int(str(dt)[5:7])
    return result


def _ns_to_quarter(ns_array: np.ndarray) -> np.ndarray:
    """Extract quarter (1-4) from int64 nanoseconds."""
    result = np.zeros(len(ns_array), dtype=int)
    for i, ns in enumerate(ns_array):
        if ns != I_NAT:
            dt = np.datetime64(int(ns), "ns")
            month = int(str(dt)[5:7])
            result[i] = (month - 1) // 3 + 1
    return result


def funnel_tat(
    events: pd.DataFrame,
    sales: pd.DataFrame | None = None,
    sla_days: dict[str, float] | float = 3.0,
    reference_date: str | None = None,
) -> dict:
    """Рассчитать время прохождения этапов воронки (TAT), выполнение SLA и зависший бэклог.

    Поддерживает таблицу `funnel_events` (`application_id`, `stage`, `event_date`, `product_code`, `channel`)
    и опциональное обогащение из `sales` (`application_id`, `manager_id`, `result`) без ошибки выжившего.
    """
    if events is None or events.empty:
        return {
            "available": False,
            "by_transition": pd.DataFrame(),
            "by_segment": pd.DataFrame(),
            "aging_backlog": pd.DataFrame(),
            "survival_curve": pd.DataFrame(),
            "assumption": "Таблица событий воронки пуста или не передана.",
        }

    df = events.copy()
    entity_col = "application_id" if "application_id" in df.columns else "client_id"
    if entity_col not in df.columns or "stage" not in df.columns or "event_date" not in df.columns:
        return {
            "available": False,
            "by_transition": pd.DataFrame(),
            "by_segment": pd.DataFrame(),
            "aging_backlog": pd.DataFrame(),
            "survival_curve": pd.DataFrame(),
            "assumption": "В таблице funnel_events отсутствуют обязательные колонки (application_id/client_id, stage, event_date).",
        }

    # Convert to numpy arrays early - avoid pandas datetime operations
    # Avoid .map() which triggers segfault in pandas 2.2.3/numpy 2.1.3
    stage_norm = np.array([_normalize_stage(s) for s in df["stage"].to_numpy()])
    stage_rank = np.array([STAGE_RANK.get(s, 0) for s in stage_norm], dtype=int)
    entity_ids = df[entity_col].to_numpy()
    event_dt_ns = _parse_dates_to_ns(df["event_date"].to_numpy())
    
    has_product = "product_code" in df.columns
    has_channel = "channel" in df.columns
    product_codes = df["product_code"].to_numpy() if has_product else np.array([""] * len(entity_ids))
    channels = df["channel"].to_numpy() if has_channel else np.array([""] * len(entity_ids))

    # Filter valid rows
    mask = (
        pd.notna(entity_ids)
        & (stage_rank > 0)
        & (event_dt_ns != I_NAT)
    )
    entity_ids = entity_ids[mask]
    stage_norm = stage_norm[mask]
    stage_rank = stage_rank[mask]
    event_dt_ns = event_dt_ns[mask]
    product_codes = product_codes[mask]
    channels = channels[mask]

    if len(entity_ids) == 0:
        return {
            "available": False,
            "by_transition": pd.DataFrame(),
            "by_segment": pd.DataFrame(),
            "aging_backlog": pd.DataFrame(),
            "survival_curve": pd.DataFrame(),
            "assumption": "После очистки дат и этапов не осталось валидных событий воронки.",
        }

    # Sort by entity, stage_rank, event_dt
    sort_idx = np.lexsort((event_dt_ns, stage_rank, entity_ids))
    entity_ids = entity_ids[sort_idx]
    stage_norm = stage_norm[sort_idx]
    stage_rank = stage_rank[sort_idx]
    event_dt_ns = event_dt_ns[sort_idx]
    product_codes = product_codes[sort_idx]
    channels = channels[sort_idx]

    # Group by entity, stage_rank, stage_norm (take first event_dt per group)
    grouped_data = []
    i = 0
    n = len(entity_ids)
    while i < n:
        j = i + 1
        while j < n and entity_ids[j] == entity_ids[i] and stage_rank[j] == stage_rank[i] and stage_norm[j] == stage_norm[i]:
            j += 1
        grouped_data.append({
            "entity": entity_ids[i],
            "stage_rank": stage_rank[i],
            "stage_norm": stage_norm[i],
            "event_dt_ns": event_dt_ns[i],
            "product_code": product_codes[i],
            "channel": channels[i],
        })
        i = j

    grouped_n = len(grouped_data)
    if grouped_n == 0:
        return {
            "available": False,
            "by_transition": pd.DataFrame(),
            "by_segment": pd.DataFrame(),
            "aging_backlog": pd.DataFrame(),
            "survival_curve": pd.DataFrame(),
            "assumption": "После группировки не осталось валидных событий.",
        }

    # Extract arrays
    g_entity = np.array([d["entity"] for d in grouped_data], dtype=object)
    g_stage_rank = np.array([d["stage_rank"] for d in grouped_data], dtype=int)
    g_stage_norm = np.array([d["stage_norm"] for d in grouped_data], dtype=object)
    g_event_dt_ns = np.array([d["event_dt_ns"] for d in grouped_data], dtype="int64")
    g_product = np.array([d["product_code"] for d in grouped_data], dtype=object)
    g_channel = np.array([d["channel"] for d in grouped_data], dtype=object)

    # Compute next_* using manual iteration
    next_stage = np.full(grouped_n, "", dtype=object)
    next_rank = np.full(grouped_n, -1, dtype=int)
    next_dt_ns = np.full(grouped_n, I_NAT, dtype="int64")

    for i in range(grouped_n - 1):
        if g_entity[i] == g_entity[i + 1]:
            next_stage[i] = g_stage_norm[i + 1]
            next_rank[i] = g_stage_rank[i + 1]
            next_dt_ns[i] = g_event_dt_ns[i + 1]

    # Compute TAT
    tat_ns = next_dt_ns - g_event_dt_ns
    tat_days = np.where(
        (next_dt_ns != I_NAT) & (g_event_dt_ns != I_NAT) & (tat_ns >= 0),
        tat_ns / 86400_000_000_000.0,
        np.nan,
    )

    # Build valid transitions
    valid_mask = ~np.isnan(tat_days) & (tat_days >= 0)
    if not np.any(valid_mask):
        return {
            "available": False,
            "by_transition": pd.DataFrame(),
            "by_segment": pd.DataFrame(),
            "aging_backlog": pd.DataFrame(),
            "survival_curve": pd.DataFrame(),
            "assumption": "Нет валидных переходов между этапами.",
        }

    v_transition = g_stage_norm[valid_mask] + " -> " + next_stage[valid_mask]
    v_from_stage = g_stage_norm[valid_mask]
    v_to_stage = next_stage[valid_mask]
    v_tat = tat_days[valid_mask]
    v_product = g_product[valid_mask]
    v_channel = g_channel[valid_mask]

    def _threshold_for(trans_name: str, from_stage: str) -> float:
        if isinstance(sla_days, dict):
            return float(sla_days.get(trans_name, sla_days.get(from_stage, 3.0)))
        return float(sla_days)

    # By transition
    transition_rows: list[dict] = []
    for trans_name in np.unique(v_transition):
        mask = v_transition == trans_name
        vals = v_tat[mask]
        from_stage = v_from_stage[mask][0]
        thresh = _threshold_for(str(trans_name), str(from_stage))
        p25, p50, p75, p90, p95 = np.percentile(vals, [25, 50, 75, 90, 95])
        transition_rows.append(
            {
                "transition": str(trans_name),
                "count": int(len(vals)),
                "mean_days": round(float(np.mean(vals)), 2),
                "median_p50_days": round(float(p50), 2),
                "iqr_days": round(float(p75 - p25), 2),
                "p75_days": round(float(p75), 2),
                "p90_days": round(float(p90), 2),
                "p95_days": round(float(p95), 2),
                "sla_threshold_days": round(thresh, 2),
                "sla_compliance_pct": round(float(np.mean(vals <= thresh) * 100.0), 2),
            }
        )
    by_transition_df = pd.DataFrame(transition_rows)

    # App span
    unique_entities = np.unique(g_entity)
    app_span_rows = []
    for ent in unique_entities:
        ent_mask = g_entity == ent
        ent_event_dt_ns = g_event_dt_ns[ent_mask]
        ent_stage_rank = g_stage_rank[ent_mask]
        ent_stage_norm = g_stage_norm[ent_mask]
        ent_product = g_product[ent_mask]
        ent_channel = g_channel[ent_mask]

        first_idx = np.argmin(ent_event_dt_ns)
        last_idx = np.argmax(ent_event_dt_ns)

        app_span_rows.append({
            "entity": ent,
            "first_dt_ns": ent_event_dt_ns[first_idx],
            "last_dt_ns": ent_event_dt_ns[last_idx],
            "min_rank": int(np.min(ent_stage_rank)),
            "max_rank": int(np.max(ent_stage_rank)),
            "last_stage": str(ent_stage_norm[last_idx]),
            "product_code": str(ent_product[first_idx]) if has_product else "",
            "channel": str(ent_channel[first_idx]) if has_channel else "",
        })

    app_span_n = len(app_span_rows)
    app_first_dt_ns = np.array([r["first_dt_ns"] for r in app_span_rows], dtype="int64")
    app_last_dt_ns = np.array([r["last_dt_ns"] for r in app_span_rows], dtype="int64")
    app_min_rank = np.array([r["min_rank"] for r in app_span_rows], dtype=int)
    app_max_rank = np.array([r["max_rank"] for r in app_span_rows], dtype=int)
    app_last_stage = np.array([r["last_stage"] for r in app_span_rows], dtype=object)
    app_product = np.array([r["product_code"] for r in app_span_rows], dtype=object)
    app_channel = np.array([r["channel"] for r in app_span_rows], dtype=object)
    app_entity = np.array([r["entity"] for r in app_span_rows], dtype=object)

    # Total TAT
    total_tat_ns = app_last_dt_ns - app_first_dt_ns
    total_tat_days = np.where(
        (app_first_dt_ns != I_NAT) & (app_last_dt_ns != I_NAT) & (total_tat_ns >= 0),
        total_tat_ns / 86400_000_000_000.0,
        np.nan,
    )

    # Sales merge
    has_sales = sales is not None and not sales.empty and entity_col in sales.columns
    if has_sales:
        sales_cols = [c for c in ("application_id", "manager_id", "result") if c in sales.columns]
        if entity_col in sales_cols and len(sales_cols) > 1:
            sales_meta = sales[sales_cols].drop_duplicates(subset=[entity_col])
            sales_dict = {}
            for _, row in sales_meta.iterrows():
                sales_dict[str(row[entity_col])] = row

            app_manager = np.full(app_span_n, "", dtype=object)
            app_result = np.full(app_span_n, "", dtype=object)
            for i, ent in enumerate(app_entity):
                if ent in sales_dict:
                    sr = sales_dict[ent]
                    if "manager_id" in sr:
                        app_manager[i] = str(sr["manager_id"])
                    if "result" in sr:
                        app_result[i] = str(sr["result"])
        else:
            app_manager = np.full(app_span_n, "", dtype=object)
            app_result = np.full(app_span_n, "", dtype=object)
    else:
        app_manager = np.full(app_span_n, "", dtype=object)
        app_result = np.full(app_span_n, "", dtype=object)

    # By segment
    segment_rows: list[dict] = []
    for seg_col_name, seg_col_data in [("product_code", app_product), ("channel", app_channel), ("manager_id", app_manager)]:
        unique_segments = np.unique(seg_col_data)
        for seg_val in unique_segments:
            mask = seg_col_data == seg_val
            completed_mask = mask & (app_max_rank > app_min_rank)
            if not np.any(completed_mask):
                continue
            vals = total_tat_days[completed_mask]
            vals = vals[~np.isnan(vals)]
            if len(vals) == 0:
                continue
            p50, p90 = np.percentile(vals, [50, 90])
            segment_rows.append(
                {
                    "dimension": seg_col_name,
                    "segment": str(seg_val) if pd.notna(seg_val) else "не указано",
                    "applications": int(len(vals)),
                    "mean_total_days": round(float(np.mean(vals)), 2),
                    "p50_total_days": round(float(p50), 2),
                    "p90_total_days": round(float(p90), 2),
                }
            )
    by_segment_df = pd.DataFrame(segment_rows)

    # Aging backlog
    if reference_date is not None:
        # Parse reference_date to nanoseconds
        try:
            ref_dt_ns = int(np.datetime64(str(reference_date), 'ns').astype('int64'))
        except (ValueError, TypeError):
            ref_dt_ns = I_NAT
    else:
        ref_dt_ns = I_NAT
    terminal_rank = max(STAGE_RANK.values())

    in_progress_mask = app_max_rank < terminal_rank
    if has_sales:
        result_lower = np.array([r.lower() for r in app_result])
        completed_mask = np.isin(result_lower, ["отказ", "выдано", "закрыто"])
        in_progress_mask = in_progress_mask & ~completed_mask

    if np.any(in_progress_mask):
        ip_last_dt_ns = app_last_dt_ns[in_progress_mask]
        ip_last_stage = app_last_stage[in_progress_mask]
        aging_ns = ref_dt_ns - ip_last_dt_ns
        aging_days = np.where(
            (ip_last_dt_ns != I_NAT) & (aging_ns >= 0),
            aging_ns / 86400_000_000_000.0,
            np.nan,
        )

        aging_rows: list[dict] = []
        for stg in np.unique(ip_last_stage):
            mask = ip_last_stage == stg
            vals = aging_days[mask]
            vals = vals[~np.isnan(vals)]
            if len(vals) == 0:
                continue
            p50, p90 = np.percentile(vals, [50, 90])
            aging_rows.append(
                {
                    "current_stage": str(stg),
                    "stuck_applications": int(len(vals)),
                    "median_aging_days": round(float(p50), 2),
                    "p90_aging_days": round(float(p90), 2),
                }
            )
        aging_df = pd.DataFrame(aging_rows)
    else:
        aging_df = pd.DataFrame()

    # Kaplan-Meier survival curve
    total_n = app_span_n
    km_rows: list[dict] = []
    survival_prob = 1.0
    for stage_name in STAGE_ORDER:
        r = STAGE_RANK[stage_name]
        at_risk = int(np.sum(app_max_rank >= r))
        progressed = int(np.sum(app_max_rank > r))
        dropped_here = at_risk - progressed
        if r < terminal_rank and at_risk > 0:
            hazard = dropped_here / at_risk
            survival_prob *= 1.0 - hazard
        else:
            hazard = 0.0
        km_rows.append(
            {
                "stage": stage_name,
                "stage_rank": r,
                "at_risk": at_risk,
                "share_from_start_pct": round(float(at_risk / total_n * 100.0), 2) if total_n else 0.0,
                "stage_hazard_pct": round(float(hazard * 100.0), 2),
                "km_retention_pct": round(float(survival_prob * 100.0), 2),
            }
        )
    survival_df = pd.DataFrame(km_rows)

    ref_dt_str = _ns_to_datetime_str(np.array([ref_dt_ns]))[0] if ref_dt_ns != I_NAT else ""

    return {
        "available": not by_transition_df.empty,
        "by_transition": by_transition_df,
        "by_segment": by_segment_df,
        "aging_backlog": aging_df,
        "survival_curve": survival_df,
        "reference_date": ref_dt_str,
        "assumption": (
            "TAT рассчитан по фактическим датам переходов между этапами STAGE_ORDER. "
            "Незавершённые заявки выделены в отдельный блок aging_backlog во избежание смещения выжившего."
        ),
    }


def seasonality_summary(
    series_df: pd.DataFrame,
    value_col: str = "value",
    period_col: str = "period",
    period_length: int | None = None,
    mode: str = "auto",
) -> dict:
    """Выполнить декомпозицию сезонности (аддитивную или мультипликативную) и оценить силу сезонности.

    Автоматически переключается на аддитивную модель, если ряд содержит нулевые или отрицательные
    значения (например, чистая прибыль или чистый приток), и корректно обрабатывает короткие ряды (< 2 циклов).
    """
    if series_df is None or series_df.empty:
        return {
            "available": False,
            "seasonal_indices": pd.DataFrame(),
            "components": pd.DataFrame(),
            "strength_seasonal": 0.0,
            "strength_trend": 0.0,
            "mode_used": "none",
            "assumption": "Входной временной ряд пуст.",
        }

    df = series_df.copy()
    if period_col not in df.columns:
        for alt_col in ("period_start", "report_date", "date"):
            if alt_col in df.columns:
                period_col = alt_col
                break
    if value_col not in df.columns and "interest_income" in df.columns:
        df["_computed_value"] = pd.to_numeric(df["interest_income"], errors="coerce").fillna(0.0)
        if "fee_income" in df.columns:
            df["_computed_value"] += pd.to_numeric(df["fee_income"], errors="coerce").fillna(0.0)
        value_col = "_computed_value"

    if period_col not in df.columns or value_col not in df.columns:
        return {
            "available": False,
            "seasonal_indices": pd.DataFrame(),
            "components": pd.DataFrame(),
            "strength_seasonal": 0.0,
            "strength_trend": 0.0,
            "mode_used": "none",
            "assumption": f"Не найдены колонки периода ({period_col}) или значения ({value_col}).",
        }

    # Parse dates with numpy, avoid pandas datetime
    dt_ns = _parse_dates_to_ns(df[period_col].to_numpy())
    val = pd.to_numeric(df[value_col], errors="coerce").to_numpy()

    # Filter valid
    mask = (dt_ns != I_NAT) & pd.notna(val)
    dt_ns = dt_ns[mask]
    val = val[mask]

    if len(dt_ns) == 0:
        return {
            "available": False,
            "seasonal_indices": pd.DataFrame(),
            "components": pd.DataFrame(),
            "strength_seasonal": 0.0,
            "strength_trend": 0.0,
            "mode_used": "none",
            "assumption": "После очистки не осталось валидных точек.",
        }

    # Sort by date
    sort_idx = np.argsort(dt_ns)
    dt_ns = dt_ns[sort_idx]
    val = val[sort_idx]

    # Aggregate by date (sum values for same date)
    unique_dates, inverse_idx = np.unique(dt_ns, return_inverse=True)
    agg_val = np.zeros(len(unique_dates), dtype=float)
    np.add.at(agg_val, inverse_idx, val)

    y = agg_val
    dates_ns = unique_dates
    n = len(y)

    if period_length is None:
        if n >= 2:
            median_gap = float(np.median(np.diff(dates_ns).astype(float)) / 86400_000_000_000.0)
            period_length = 4 if median_gap > 45 else 12
        else:
            period_length = 12

    has_non_positive = bool(np.any(y <= 0.0))
    if mode == "auto":
        mode_used = "additive" if has_non_positive else "multiplicative"
    elif mode == "multiplicative" and has_non_positive:
        mode_used = "additive"
    else:
        mode_used = mode

    # Оценка тренда скользящим центрированным средним (или линейным сглаживанием на коротких рядах)
    if n >= period_length:
        kernel = np.ones(period_length, dtype=float) / period_length
        trend = np.convolve(y, kernel, mode="same")
        half = period_length // 2
        if n > 1:
            slope_start = (trend[half] - trend[min(half + 1, n - 1)]) if half < n - 1 else 0.0
            for i in range(half):
                trend[i] = trend[half] + slope_start * (half - i)
            last_valid = n - half - 1
            slope_end = (trend[last_valid] - trend[max(0, last_valid - 1)]) if last_valid > 0 else 0.0
            for i in range(last_valid + 1, n):
                trend[i] = trend[last_valid] + slope_end * (i - last_valid)
    else:
        x = np.arange(n, dtype=float)
        if n >= 2:
            slope, intercept = np.polyfit(x, y, 1)
            trend = intercept + slope * x
        else:
            trend = y.copy()

    if n < 2 * period_length:
        residual = y - trend
        comp_df = pd.DataFrame(
            {
                "period": _ns_to_datetime_str(dates_ns),
                "observed": np.round(y, 2),
                "trend": np.round(trend, 2),
                "seasonal": np.zeros(n),
                "residual": np.round(residual, 2),
            }
        )
        var_sr = float(np.var(y))
        var_r = float(np.var(residual))
        strength_trend = max(0.0, min(1.0, 1.0 - var_r / var_sr)) if var_sr > 0 else 0.0
        return {
            "available": False,
            "seasonal_indices": pd.DataFrame(),
            "components": comp_df,
            "strength_seasonal": 0.0,
            "strength_trend": round(strength_trend, 4),
            "period_length": int(period_length),
            "mode_used": mode_used,
            "assumption": (
                f"Длина ряда ({n} точек) меньше двух полных сезонных циклов ({2 * period_length}); "
                "сезонные индексы не оцениваются во избежание ложной сезонности, выделен только тренд."
            ),
        }

    # Cycle position
    if period_length == 12:
        cycle_pos = _ns_to_month(dates_ns)
    elif period_length == 4:
        cycle_pos = _ns_to_quarter(dates_ns)
    else:
        cycle_pos = (np.arange(n) % period_length) + 1

    if mode_used == "multiplicative":
        safe_trend = np.where(np.abs(trend) < 1e-9, np.mean(y), trend)
        detrended = y / safe_trend
        raw_indices: dict[int, float] = {}
        for pos in range(1, period_length + 1):
            mask = cycle_pos == pos
            raw_indices[pos] = float(np.median(detrended[mask])) if np.any(mask) else 1.0
        norm_factor = float(np.mean(list(raw_indices.values()))) or 1.0
        indices = {k: v / norm_factor for k, v in raw_indices.items()}
        seasonal_vec = np.array([indices[p] for p in cycle_pos], dtype=float)
        seasonal_effect = trend * (seasonal_vec - 1.0)
        residual = y - (trend * seasonal_vec)
    else:
        detrended = y - trend
        raw_indices = {}
        for pos in range(1, period_length + 1):
            mask = cycle_pos == pos
            raw_indices[pos] = float(np.median(detrended[mask])) if np.any(mask) else 0.0
        mean_offset = float(np.mean(list(raw_indices.values())))
        indices = {k: v - mean_offset for k, v in raw_indices.items()}
        seasonal_vec = np.array([indices[p] for p in cycle_pos], dtype=float)
        seasonal_effect = seasonal_vec
        residual = y - trend - seasonal_vec

    var_res = float(np.var(residual))
    var_seas_res = float(np.var(seasonal_effect + residual))
    var_trend_res = float(np.var(trend + residual))

    strength_seasonal = max(0.0, min(1.0, 1.0 - var_res / var_seas_res)) if var_seas_res > 1e-12 else 0.0
    strength_trend = max(0.0, min(1.0, 1.0 - var_res / var_trend_res)) if var_trend_res > 1e-12 else 0.0

    idx_df = pd.DataFrame(
        [
            {
                "cycle_unit": int(k),
                "seasonal_index": round(float(v), 4),
                "interpretation_pct": (
                    round((float(v) - 1.0) * 100.0, 2)
                    if mode_used == "multiplicative"
                    else round(float(v), 2)
                ),
            }
            for k, v in sorted(indices.items())
        ]
    )

    comp_df = pd.DataFrame(
        {
            "period": _ns_to_datetime_str(dates_ns),
            "observed": np.round(y, 2),
            "trend": np.round(trend, 2),
            "seasonal": np.round(seasonal_effect, 2),
            "residual": np.round(residual, 2),
        }
    )

    return {
        "available": True,
        "seasonal_indices": idx_df,
        "components": comp_df,
        "strength_seasonal": round(strength_seasonal, 4),
        "strength_trend": round(strength_trend, 4),
        "period_length": int(period_length),
        "mode_used": mode_used,
        "assumption": (
            f"Сезонность рассчитана по {n} периодам (цикл={period_length}, схема={mode_used}). "
            f"Сила сезонности Fs={strength_seasonal:.2f}, сила тренда Ft={strength_trend:.2f}."
        ),
    }