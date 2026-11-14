"""Расчёт поэтапного времени прохождения воронки (TAT) и декомпозиции сезонности."""

from __future__ import annotations

import numpy as np
import pandas as pd

from .core import drop_total_rows, parse_period
from .metrics import STAGE_ORDER, STAGE_RANK, _normalize_stage

I_NAT = np.iinfo(np.int64).min


def _parse_dates_to_ns(date_strings: np.ndarray) -> np.ndarray:
    """Разобрать массив дат/периодов в наносекунды int64 с фолбэком на parse_period.

    Основная часть выгрузок в формате ISO-8601 («2024-05-01», «2024-05»)
    разбирается векторно; точечный разбор `parse_period` остаётся только для
    значений, которые pandas не понял («15.05.2024», «1к24», «январь 2024»).
    """
    count = len(date_strings)
    result = np.full(count, I_NAT, dtype="int64")
    if count == 0:
        return result

    series = pd.Series(date_strings, dtype=object)
    try:
        fast = pd.to_datetime(series, format="ISO8601", errors="coerce")
    except (ValueError, TypeError):
        fast = None

    if fast is not None:
        if isinstance(fast.dtype, pd.DatetimeTZDtype):
            fast = fast.dt.tz_convert("UTC").dt.tz_localize(None)
        values = fast.to_numpy(dtype="datetime64[ns]", na_value=np.datetime64("NaT"))
        result = values.astype("int64")

    unresolved = result == I_NAT
    if unresolved.any():
        for idx in np.flatnonzero(unresolved):
            val = date_strings[idx]
            if pd.isna(val) or str(val).strip() == "":
                continue
            ts, _ = parse_period(str(val).strip())
            if pd.notna(ts):
                iso = f"{int(ts.year):04d}-{int(ts.month):02d}-{int(ts.day):02d}"
                result[idx] = np.datetime64(iso, "ns").astype("int64")
    return result


def _ns_to_datetime_str(ns_array: np.ndarray) -> np.ndarray:
    """Convert int64 nanoseconds to YYYY-MM-DD string array."""
    result = np.full(len(ns_array), "", dtype=object)
    valid = ns_array != I_NAT
    if valid.any():
        dates = ns_array[valid].astype("datetime64[ns]").astype(str)
        result[valid] = np.array([value[:10] for value in dates], dtype=object)
    return result


def _ns_to_month(ns_array: np.ndarray) -> np.ndarray:
    """Extract month (1-12) from int64 nanoseconds."""
    result = np.zeros(len(ns_array), dtype=int)
    valid = ns_array != I_NAT
    if valid.any():
        # datetime64[M] даёт индекс месяца с эпохи: номер месяца в году —
        # остаток от деления на 12 плюс один.
        months = ns_array[valid].astype("datetime64[ns]").astype("datetime64[M]")
        result[valid] = (months.astype("int64") % 12) + 1
    return result


def _ns_to_quarter(ns_array: np.ndarray) -> np.ndarray:
    """Extract quarter (1-4) from int64 nanoseconds."""
    result = np.zeros(len(ns_array), dtype=int)
    months = _ns_to_month(ns_array)
    valid = ns_array != I_NAT
    result[valid] = (months[valid] - 1) // 3 + 1
    return result


def funnel_tat(
    events: pd.DataFrame,
    sales: pd.DataFrame | None = None,
    sla_days: dict[str, float] | float = 3.0,
    reference_date: str | None = None,
) -> dict:
    """Рассчитать время прохождения этапов воронки (TAT), выполнение SLA и зависший бэклог.

    Переходы делятся на смежные (``лид -> обращение``) и «сквозные» (``лид ->
    одобрение`` без промежуточного этапа). В расчёт TAT и SLA входят только
    смежные переходы: «сквозной» переход измеряет охват нескольких этапов, и
    подставлять его в норматив одного этапа нельзя. «Сквозные» переходы
    возвращаются отдельной таблицей `skipped_transitions`. Возврат на более
    ранний этап считается отдельно и в TAT не входит.
    """
    if events is None or events.empty:
        return {
            "available": False,
            "by_transition": pd.DataFrame(),
            "skipped_transitions": pd.DataFrame(),
            "skipped_count": 0,
            "backward_count": 0,
            "by_segment": pd.DataFrame(),
            "aging_backlog": pd.DataFrame(),
            "survival_curve": pd.DataFrame(),
            "assumption": "Таблица событий воронки пуста или не передана.",
        }

    df = events.copy()
    entity_col = "application_id" if "application_id" in df.columns else "client_id"
    date_col = next((c for c in ("event_date", "stage_date", "application_date") if c in df.columns), None)
    if entity_col not in df.columns or "stage" not in df.columns or date_col is None:
        return {
            "available": False,
            "by_transition": pd.DataFrame(),
            "skipped_transitions": pd.DataFrame(),
            "skipped_count": 0,
            "backward_count": 0,
            "by_segment": pd.DataFrame(),
            "aging_backlog": pd.DataFrame(),
            "survival_curve": pd.DataFrame(),
            "assumption": "В таблице воронки отсутствуют обязательные колонки (application_id/client_id, stage, event_date).",
        }

    stage_norm = np.array(
        [_normalize_stage(s) if pd.notna(s) else "" for s in df["stage"].to_numpy()],
        dtype=object,
    )
    stage_rank = np.array([STAGE_RANK.get(s, 0) for s in stage_norm], dtype=int)
    entity_ids = df[entity_col].to_numpy()
    event_dt_ns = _parse_dates_to_ns(df[date_col].to_numpy())

    has_product = "product_code" in df.columns
    has_channel = "channel" in df.columns
    product_codes = df["product_code"].to_numpy() if has_product else np.array([""] * len(entity_ids))
    channels = df["channel"].to_numpy() if has_channel else np.array([""] * len(entity_ids))

    mask = pd.notna(entity_ids) & (stage_rank > 0) & (event_dt_ns != I_NAT)
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
            "skipped_transitions": pd.DataFrame(),
            "skipped_count": 0,
            "backward_count": 0,
            "by_segment": pd.DataFrame(),
            "aging_backlog": pd.DataFrame(),
            "survival_curve": pd.DataFrame(),
            "assumption": "После очистки дат и этапов не осталось валидных событий воронки.",
        }

    sort_idx = np.lexsort((stage_rank, event_dt_ns, entity_ids))
    entity_ids = entity_ids[sort_idx]
    stage_norm = stage_norm[sort_idx]
    stage_rank = stage_rank[sort_idx]
    event_dt_ns = event_dt_ns[sort_idx]
    product_codes = product_codes[sort_idx]
    channels = channels[sort_idx]

    # Переходы — по журналу событий во времени, а не по рангу этапа.
    # Сортировка по рангу стирала возвраты: «следующим» всегда оказывался
    # более поздний этап, и счётчик backward_count оставался нулевым.
    n_events = len(entity_ids)
    g_entity = entity_ids
    g_stage_rank = stage_rank
    g_stage_norm = stage_norm
    g_event_dt_ns = event_dt_ns
    g_product = product_codes
    g_channel = channels

    next_stage = np.full(n_events, "", dtype=object)
    next_rank = np.full(n_events, -1, dtype=int)
    next_dt_ns = np.full(n_events, I_NAT, dtype="int64")
    for i in range(n_events - 1):
        if g_entity[i] == g_entity[i + 1]:
            next_stage[i] = g_stage_norm[i + 1]
            next_rank[i] = g_stage_rank[i + 1]
            next_dt_ns[i] = g_event_dt_ns[i + 1]

    has_next = next_rank >= 0
    rank_gap = np.zeros(n_events, dtype=int)
    rank_gap[has_next] = next_rank[has_next] - g_stage_rank[has_next]
    tat_days = np.full(n_events, np.nan, dtype="float64")
    timed = has_next & (next_dt_ns != I_NAT) & (g_event_dt_ns != I_NAT) & (next_dt_ns >= g_event_dt_ns)
    tat_days[timed] = (next_dt_ns[timed] - g_event_dt_ns[timed]) / 86400_000_000_000.0

    # gap == 0 — повтор того же этапа, в TAT не идёт и возвратом не считается.
    adjacent_mask = timed & (rank_gap == 1) & (tat_days >= 0)
    skipped_mask = timed & (rank_gap > 1) & (tat_days >= 0)
    backward_mask = has_next & (rank_gap < 0)
    skipped_count = int(skipped_mask.sum())
    backward_count = int(backward_mask.sum())

    v_transition = g_stage_norm[adjacent_mask] + " -> " + next_stage[adjacent_mask]
    v_from_stage = g_stage_norm[adjacent_mask]
    v_tat = tat_days[adjacent_mask]

    def _threshold_for(trans_name: str, from_stage: str) -> float:
        if isinstance(sla_days, dict):
            return float(sla_days.get(trans_name, sla_days.get(from_stage, 3.0)))
        return float(sla_days)

    transition_rows: list[dict] = []
    for trans_name in np.unique(v_transition):
        t_mask = v_transition == trans_name
        vals = v_tat[t_mask]
        from_stage = v_from_stage[t_mask][0]
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
    if not by_transition_df.empty:
        by_transition_df["_ord"] = [
            STAGE_RANK.get(str(name).split(" -> ")[0], 99)
            for name in by_transition_df["transition"]
        ]
        by_transition_df = (
            by_transition_df.sort_values(["_ord", "transition"])
            .drop(columns="_ord")
            .reset_index(drop=True)
        )

    # Переходы с пропущенным этапом («лид → одобрение» без «заявки»): это
    # суммарный охват нескольких этапов, а не время одного этапа, поэтому они
    # не участвуют в расчёте SLA и идут отдельной таблицей.
    skipped_rows: list[dict] = []
    if np.any(skipped_mask):
        s_transition = g_stage_norm[skipped_mask] + " -> " + next_stage[skipped_mask]
        s_gap = rank_gap[skipped_mask]
        s_tat = tat_days[skipped_mask]
        for trans_name in np.unique(s_transition):
            t_mask = s_transition == trans_name
            vals = s_tat[t_mask]
            p50, p90 = np.percentile(vals, [50, 90])
            skipped_rows.append(
                {
                    "transition": str(trans_name),
                    "stages_skipped": int(np.max(s_gap[t_mask]) - 1),
                    "count": int(len(vals)),
                    "mean_days": round(float(np.mean(vals)), 2),
                    "median_p50_days": round(float(p50), 2),
                    "p90_days": round(float(p90), 2),
                }
            )
    skipped_df = pd.DataFrame(skipped_rows)

    unique_entities = np.unique(g_entity)
    app_span_rows = []
    for ent in unique_entities:
        ent_mask = g_entity == ent
        ent_event_dt_ns = g_event_dt_ns[ent_mask]
        ent_stage_rank = g_stage_rank[ent_mask]
        ent_stage_norm = g_stage_norm[ent_mask]
        ent_product = g_product[ent_mask]
        ent_channel = g_channel[ent_mask]

        first_idx = int(np.argmin(ent_event_dt_ns))
        max_dt = np.max(ent_event_dt_ns)
        # Если в день максимальной даты было несколько этапов, берём этап с наибольшим рангом
        last_candidates = np.where(ent_event_dt_ns == max_dt)[0]
        last_idx = int(last_candidates[np.argmax(ent_stage_rank[last_candidates])])

        app_span_rows.append({
            "entity": ent,
            "first_dt_ns": ent_event_dt_ns[first_idx],
            "last_dt_ns": ent_event_dt_ns[last_idx],
            "min_rank": int(np.min(ent_stage_rank)),
            "max_rank": int(np.max(ent_stage_rank)),
            "last_stage": str(ent_stage_norm[last_idx]),
            "product_code": str(ent_product[first_idx]) if has_product and pd.notna(ent_product[first_idx]) else "",
            "channel": str(ent_channel[first_idx]) if has_channel and pd.notna(ent_channel[first_idx]) else "",
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

    valid_total = (app_first_dt_ns != I_NAT) & (app_last_dt_ns != I_NAT) & (app_last_dt_ns >= app_first_dt_ns)
    total_tat_days = np.full(app_span_n, np.nan, dtype="float64")
    total_tat_days[valid_total] = (
        (app_last_dt_ns[valid_total] - app_first_dt_ns[valid_total]) / 86400_000_000_000.0
    )

    app_manager = np.full(app_span_n, "", dtype=object)
    app_result = np.full(app_span_n, "", dtype=object)
    has_sales = sales is not None and not sales.empty and entity_col in sales.columns
    if has_sales:
        sales_cols = [c for c in (entity_col, "manager_id", "result") if c in sales.columns]
        if len(sales_cols) > 1:
            sales_meta = sales[sales_cols].drop_duplicates(subset=[entity_col])
            # itertuples вместо iterrows: словарь строится по всей таблице sales,
            # и на больших выгрузках разница на порядки.
            sales_dict: dict[str, dict] = {}
            for row in sales_meta.itertuples(index=False):
                key_value = getattr(row, entity_col)
                if pd.notna(key_value):
                    sales_dict[str(key_value)] = {
                        "manager_id": getattr(row, "manager_id", None),
                        "result": getattr(row, "result", None),
                    }
            for i, ent in enumerate(app_entity):
                sr = sales_dict.get(str(ent))
                if sr is None:
                    continue
                if sr["manager_id"] is not None and pd.notna(sr["manager_id"]):
                    app_manager[i] = str(sr["manager_id"]).strip()
                if sr["result"] is not None and pd.notna(sr["result"]):
                    app_result[i] = str(sr["result"]).strip()

    segment_rows: list[dict] = []
    for seg_col_name, seg_col_data in [
        ("product_code", app_product),
        ("channel", app_channel),
        ("manager_id", app_manager),
    ]:
        unique_segments = [s for s in np.unique(seg_col_data) if str(s).strip() != ""]
        for seg_val in unique_segments:
            s_mask = seg_col_data == seg_val
            completed_mask = s_mask & (app_max_rank > app_min_rank)
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
                    "segment": str(seg_val),
                    "applications": int(len(vals)),
                    "mean_total_days": round(float(np.mean(vals)), 2),
                    "p50_total_days": round(float(p50), 2),
                    "p90_total_days": round(float(p90), 2),
                }
            )
    by_segment_df = pd.DataFrame(segment_rows)

    if reference_date is not None:
        parsed_ref = _parse_dates_to_ns(np.array([reference_date], dtype=object))
        ref_dt_ns = int(parsed_ref[0])
    else:
        ref_dt_ns = int(np.max(g_event_dt_ns)) if len(g_event_dt_ns) > 0 else I_NAT

    terminal_rank = max(STAGE_RANK.values())
    in_progress_mask = app_max_rank < terminal_rank
    if has_sales:
        result_lower = np.array([str(r).lower() for r in app_result])
        completed_mask = np.isin(result_lower, ["отказ", "выдано", "закрыто"])
        in_progress_mask = in_progress_mask & ~completed_mask

    if ref_dt_ns != I_NAT and np.any(in_progress_mask):
        ip_last_dt_ns = app_last_dt_ns[in_progress_mask]
        ip_last_stage = app_last_stage[in_progress_mask]
        valid_aging = (ip_last_dt_ns != I_NAT) & (ref_dt_ns >= ip_last_dt_ns)
        aging_days = np.full(len(ip_last_dt_ns), np.nan, dtype="float64")
        aging_days[valid_aging] = (ref_dt_ns - ip_last_dt_ns[valid_aging]) / 86400_000_000_000.0

        aging_rows: list[dict] = []
        for stg in np.unique(ip_last_stage):
            a_mask = ip_last_stage == stg
            vals = aging_days[a_mask]
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

    total_n = app_span_n
    km_rows: list[dict] = []
    survival_prob = 1.0
    for stage_name in STAGE_ORDER:
        r = STAGE_RANK[stage_name]
        at_risk = int(np.sum(app_max_rank >= r))
        progressed = int(np.sum(app_max_rank > r))
        dropped_here = at_risk - progressed
        # Удержание на этапе i — произведение (1 - hazard) по предыдущим этапам,
        # как в metric-contracts.md. Hazard текущего этапа применяется после записи.
        if r < terminal_rank and at_risk > 0:
            hazard = dropped_here / at_risk
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
        if r < terminal_rank and at_risk > 0:
            survival_prob *= 1.0 - hazard
    survival_df = pd.DataFrame(km_rows)

    ref_dt_str = _ns_to_datetime_str(np.array([ref_dt_ns]))[0] if ref_dt_ns != I_NAT else ""

    return {
        "available": not by_transition_df.empty,
        "by_transition": by_transition_df,
        "skipped_transitions": skipped_df,
        "skipped_count": skipped_count,
        "backward_count": backward_count,
        "by_segment": by_segment_df,
        "aging_backlog": aging_df,
        "survival_curve": survival_df,
        "reference_date": ref_dt_str,
        "assumption": (
            ("TAT рассчитан по фактическим датам переходов между соседними этапами STAGE_ORDER. "
             if not by_transition_df.empty
             else "Смежных переходов между этапами нет, TAT по этапам не рассчитан. ")
            + (f"Пропущено этапов в {skipped_count} переходах — они вынесены в отдельную таблицу "
               "и не участвуют в расчёте SLA. " if skipped_count else "")
            + (f"Ещё {backward_count} переходов возвращают клиента на предыдущий этап — "
               "в TAT не включены. " if backward_count else "")
            + f"Незавершённые заявки выделены в блок aging_backlog на дату среза {ref_dt_str or 'макс. даты'}."
        ),
    }


def seasonality_summary(
    series_df: pd.DataFrame,
    value_col: str = "value",
    period_col: str = "period",
    period_length: int | None = None,
    mode: str = "auto",
) -> dict:
    """Выполнить декомпозицию сезонности (аддитивную или мультипликативную) и оценить силу сезонности."""
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
        for alt_col in ("period_start", "report_date", "date", "event_date"):
            if alt_col in df.columns:
                period_col = alt_col
                break

    if "product_code" in df.columns:
        df, _ = drop_total_rows(df, group=(period_col,) if period_col in df.columns else ())

    if value_col not in df.columns and "interest_income" in df.columns:
        df["_computed_value"] = pd.to_numeric(df["interest_income"], errors="coerce").fillna(0.0)
        if "fee_income" in df.columns:
            df["_computed_value"] += pd.to_numeric(df["fee_income"], errors="coerce").fillna(0.0)
        if "premium" in df.columns:
            df["_computed_value"] += pd.to_numeric(df["premium"], errors="coerce").fillna(0.0)
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

    dt_ns = _parse_dates_to_ns(df[period_col].to_numpy())
    val = pd.to_numeric(df[value_col], errors="coerce").to_numpy()

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

    sort_idx = np.argsort(dt_ns)
    dt_ns = dt_ns[sort_idx]
    val = val[sort_idx]

    unique_dates, inverse_idx = np.unique(dt_ns, return_inverse=True)
    agg_val = np.zeros(len(unique_dates), dtype=float)
    np.add.at(agg_val, inverse_idx, val)

    y = agg_val
    dates_ns = unique_dates
    n = len(y)

    if period_length is None:
        if n >= 2:
            median_gap = float(np.median(np.diff(dates_ns).astype(float)) / 86400_000_000_000.0)
            if median_gap > 300:
                period_length = 1
            elif median_gap > 150:
                period_length = 2
            elif median_gap > 45:
                period_length = 4
            elif median_gap > 20:
                period_length = 12
            else:
                period_length = 7
        else:
            period_length = 12

    has_non_positive = bool(np.any(y <= 0.0))
    if mode == "auto":
        mode_used = "additive" if has_non_positive else "multiplicative"
    elif mode == "multiplicative" and has_non_positive:
        mode_used = "additive"
    else:
        mode_used = mode

    if n < 2 * period_length:
        x = np.arange(n, dtype=float)
        if n >= 2:
            slope, intercept = np.polyfit(x, y, 1)
            trend = intercept + slope * x
        else:
            trend = y.copy()
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

    # Симметричное центрированное скользящее среднее (2xP-MA для чётного P)
    if period_length % 2 == 0:
        kernel = np.ones(period_length + 1, dtype=float) / period_length
        kernel[0] = 0.5 / period_length
        kernel[-1] = 0.5 / period_length
    else:
        kernel = np.ones(period_length, dtype=float) / period_length
    half = len(kernel) // 2
    valid_conv = np.convolve(y, kernel, mode="valid")
    trend = np.zeros(n, dtype=float)
    trend[half : n - half] = valid_conv
    slope_start = (trend[half] - trend[half + 1]) if (half + 1 < n - half) else 0.0
    for i in range(half):
        trend[i] = trend[half] + slope_start * (half - i)
    last_valid = n - half - 1
    slope_end = (trend[last_valid] - trend[last_valid - 1]) if (last_valid - 1 >= half) else 0.0
    for i in range(last_valid + 1, n):
        trend[i] = trend[last_valid] + slope_end * (i - last_valid)

    if period_length == 12:
        cycle_pos = _ns_to_month(dates_ns)
    elif period_length == 4:
        cycle_pos = _ns_to_quarter(dates_ns)
    elif period_length == 2:
        cycle_pos = np.where(_ns_to_month(dates_ns) <= 6, 1, 2)
    else:
        cycle_pos = (np.arange(n) % period_length) + 1

    # Индексы считаются только по внутренней части центрированного среднего.
    # Края, где тренд достроен линейной экстраполяцией, в медиану не входят:
    # иначе сезонный индекс опирается на выдуманный тренд.
    interior = np.zeros(n, dtype=bool)
    if n - half > half:
        interior[half: n - half] = True

    if mode_used == "multiplicative":
        safe_trend = np.where(np.abs(trend) < 1e-9, np.mean(y), trend)
        detrended = y / safe_trend
        raw_indices: dict[int, float] = {}
        for pos in range(1, period_length + 1):
            c_mask = (cycle_pos == pos) & interior
            raw_indices[pos] = float(np.median(detrended[c_mask])) if np.any(c_mask) else 1.0
        norm_factor = float(np.mean(list(raw_indices.values()))) or 1.0
        indices = {k: v / norm_factor for k, v in raw_indices.items()}
        seasonal_vec = np.array([indices[p] for p in cycle_pos], dtype=float)
        seasonal_effect = trend * (seasonal_vec - 1.0)
        residual = y - (trend * seasonal_vec)
    else:
        detrended = y - trend
        raw_indices = {}
        for pos in range(1, period_length + 1):
            c_mask = (cycle_pos == pos) & interior
            raw_indices[pos] = float(np.median(detrended[c_mask])) if np.any(c_mask) else 0.0
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
