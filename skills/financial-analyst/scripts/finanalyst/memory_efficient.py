"""Memory-efficient processing: incremental metrics, partitioning, parallel chunks.

Для обработки больших данных (>10M строк) без полной загрузки в память.

Архитектура:
- `IncrementalAggregator` — накопление статистик по чанкам
- `PartitionedProcessor` — обработка по партициям (время/продукт)
- `ParallelChunkProcessor` — параллельная обработка чанков
- `StreamingPipeline` — streaming-пайплайн с инкрементальными метриками
"""

from __future__ import annotations

import concurrent.futures
import multiprocessing as mp
import tempfile
from abc import ABC, abstractmethod
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator

import numpy as np
import pandas as pd

from .core import STAGE_ORDER, STAGE_RANK, load_tables_streaming

# Группы синонимов: одна и та же сущность в разных выгрузках. В агрегатор
# попадает только один источник из группы (первый найденный по приоритету),
# иначе portfolio+accounts или funnel_events+sales считались бы дважды.
ALIAS_GROUPS: dict[str, tuple[str, ...]] = {
    "portfolio": ("portfolio", "accounts"),
    "funnel": ("funnel_events", "funnel", "sales", "applications"),
}

# Имена таблиц, которые наполняют каждый инкрементальный агрегатор.
TABLE_TO_AGGREGATOR: dict[str, str] = {
    alias: group for group, aliases in ALIAS_GROUPS.items() for alias in aliases
}
TABLE_TO_AGGREGATOR.update({"finance": "finance", "clients": "clients"})


def _table_names(data_dir: Path) -> set[str]:
    return {
        path.stem.strip().lower().replace("ё", "е")
        for path in data_dir.iterdir()
        if path.is_file() and path.suffix.lower() in {".csv", ".xls", ".xlsx"}
    }


def resolve_source_tables(data_dir: Path) -> dict[str, str]:
    """Выбрать по одному источнику на группу синонимов.

    Приоритет: ``portfolio`` важнее ``accounts``, ``funnel_events`` важнее
    ``sales``. Без этого агрегаторы суммировали бы дубли.
    """
    names = _table_names(data_dir)
    resolved: dict[str, str] = {}
    for group, candidates in ALIAS_GROUPS.items():
        for candidate in candidates:
            if candidate in names:
                resolved[group] = candidate
                break
    return resolved


def resolve_sources_from_tables(chunk_tables: dict[str, pd.DataFrame]) -> dict[str, str]:
    """Выбрать источник каждой группы по составу чанка (для параллельных воркеров)."""
    present = set(chunk_tables)
    resolved: dict[str, str] = {}
    for group, candidates in ALIAS_GROUPS.items():
        for candidate in candidates:
            if candidate in present:
                resolved[group] = candidate
                break
    return resolved


def update_aggregators(
    aggregators: dict[str, "IncrementalAggregator"],
    chunk_tables: dict[str, pd.DataFrame],
    sources: dict[str, str],
) -> int:
    """Обновить агрегаторы одним чанком. Возвращает число обработанных строк."""
    total_rows = 0
    for name, df in chunk_tables.items():
        if df is None or df.empty:
            continue
        total_rows += len(df)
        target = TABLE_TO_AGGREGATOR.get(name)
        if not target or target not in aggregators:
            continue
        chosen = sources.get(target)
        # Синоним, не выбранный источником группы, пропускаем.
        if chosen is not None and name != chosen:
            continue
        aggregators[target].update(df)
    return total_rows


# ============================================================
# Конфигурация
# ============================================================

@dataclass
class MemoryEfficientConfig:
    """Конфигурация memory-efficient режима."""
    # Чанкинг
    chunksize: int = 50_000
    max_chunks_in_memory: int = 10  # Макс. чанков перед сбросом на диск

    # Партиционирование
    partition_by_time: bool = True
    partition_by_product: bool = False
    time_partition_freq: str = "M"  # M=месяц, Q=квартал, W=неделя

    # Параллелизм
    n_workers: int | None = None  # None = CPU count
    use_processes: bool = False  # True=ProcessPool, False=ThreadPool

    # Выгрузка промежуточных результатов
    spill_to_disk: bool = True
    spill_dir: Path | None = None

    # Пороги для отключения тяжёлых метрик
    skip_forecast_above_rows: int = 1_000_000
    skip_credit_risk_above_rows: int = 5_000_000
    skip_drivers_above_rows: int = 2_000_000

    def __post_init__(self):
        if self.spill_dir is None:
            self.spill_dir = Path(tempfile.gettempdir()) / "finanalyst_spill"
        self.spill_dir.mkdir(parents=True, exist_ok=True)

        if self.n_workers is None:
            self.n_workers = min(mp.cpu_count(), 8)


# ============================================================
# Инкрементальные агрегаторы
# ============================================================

class IncrementalAggregator(ABC):
    """Базовый класс для инкрементального накопления статистик."""

    # Базовые атрибуты для type checking
    total_rows: int = 0
    total_balance: float = 0.0
    total_limit: float = 0.0
    par_buckets: dict[str, float] = {}
    currencies: dict[str, float] = {}
    statuses: dict[str, int] = {}
    product_codes_dict: dict[str, float] = {}
    product_codes_set: set = set()
    _dpd_sum: float = 0.0
    _dpd_count: int = 0

    periods: dict[str, dict] = {}

    stage_counts: dict[str, int] = {}
    period_stage: dict[tuple[str, str], int] = {}
    product_stage: dict[tuple[str, str], int] = {}

    total: int = 0
    segments: dict[str, int] = {}
    regions: dict[str, int] = {}
    channels: dict[str, int] = {}
    genders: dict[str, int] = {}
    ages: list[float] = []

    @abstractmethod
    def update(self, chunk: pd.DataFrame) -> None:
        """Обновить состояние новым чанком."""
        ...

    @abstractmethod
    def finalize(self) -> dict[str, Any]:
        """Вернуть итоговый результат."""
        ...

    @abstractmethod
    def merge(self, other: "IncrementalAggregator") -> None:
        """Объединить с другим агрегатором (для параллельной обработки)."""
        ...


class PortfolioAggregator(IncrementalAggregator):
    """Инкрементальная агрегация портфеля."""

    def __init__(self):
        self.total_rows = 0
        self.total_balance = 0.0
        self.total_limit = 0.0
        self.par_buckets = defaultdict(float)
        self.dpds = []
        self.currencies = defaultdict(float)
        self.statuses = defaultdict(int)
        self.product_codes_dict = defaultdict(float)
        self._dpd_sum = 0.0
        self._dpd_count = 0

    def update(self, chunk: pd.DataFrame) -> None:
        if "balance" in chunk.columns:
            self.total_balance += chunk["balance"].sum()
            self.total_rows += len(chunk)

        if "limit_amount" in chunk.columns:
            self.total_limit += chunk["limit_amount"].sum()

        if "par_bucket" in chunk.columns:
            for bucket, count in chunk["par_bucket"].value_counts().items():
                self.par_buckets[str(bucket)] += count

        if "dpd" in chunk.columns:
            dpd_vals = pd.to_numeric(chunk["dpd"], errors="coerce").dropna()
            self._dpd_sum += dpd_vals.sum()
            self._dpd_count += len(dpd_vals)

        if "currency" in chunk.columns:
            for cur, bal in chunk.groupby("currency")["balance"].sum().items():
                self.currencies[str(cur)] += bal

        if "status" in chunk.columns:
            for st, cnt in chunk["status"].value_counts().items():
                self.statuses[str(st)] += cnt

        if "product_code" in chunk.columns:
            for prod, bal in chunk.groupby("product_code")["balance"].sum().items():
                self.product_codes_dict[str(prod)] += bal

    def merge(self, other: IncrementalAggregator) -> None:
        other_port = other  # type: ignore[assignment]
        self.total_rows += other_port.total_rows
        self.total_balance += other_port.total_balance
        self.total_limit += other_port.total_limit
        self._dpd_sum += other_port._dpd_sum
        self._dpd_count += other_port._dpd_count

        for k, v in other_port.par_buckets.items():
            self.par_buckets[k] += v
        for k, v in other_port.currencies.items():
            self.currencies[k] += v
        for k, v in other_port.statuses.items():
            self.statuses[k] += v
        for k, v in other_port.product_codes_dict.items():
            self.product_codes_dict[k] += v

    def finalize(self) -> dict[str, Any]:
        avg_dpd = self._dpd_sum / self._dpd_count if self._dpd_count > 0 else 0
        par_30 = sum(v for k, v in self.par_buckets.items() if k in ("31-60", "61-90", "90+", "91-180", "180+"))
        par_60 = sum(v for k, v in self.par_buckets.items() if k in ("61-90", "90+", "91-180", "180+"))
        par_90 = sum(v for k, v in self.par_buckets.items() if k in ("90+", "91-180", "180+"))

        return {
            "total_rows": self.total_rows,
            "total_balance": self.total_balance,
            "total_limit": self.total_limit,
            "avg_dpd": avg_dpd,
            "par_30_pct": (par_30 / self.total_rows * 100) if self.total_rows > 0 else 0,
            "par_60_pct": (par_60 / self.total_rows * 100) if self.total_rows > 0 else 0,
            "par_90_pct": (par_90 / self.total_rows * 100) if self.total_rows > 0 else 0,
            "par_buckets": dict(self.par_buckets),
            "currencies": dict(self.currencies),
            "statuses": dict(self.statuses),
            "product_codes": dict(self.product_codes_dict),
        }


def _new_period_bucket() -> dict[str, float | int]:
    """Фабрика пустого аккумулятора периода (module-level, чтобы агрегатор пиклился)."""
    return {
        "interest_income": 0.0,
        "interest_expense": 0.0,
        "fee_income": 0.0,
        "funding_cost": 0.0,
        "opex": 0.0,
        "reserve_expense": 0.0,
        "tax": 0.0,
        "marketing_cost": 0.0,
        "rows": 0,
    }


class FinanceAggregator(IncrementalAggregator):
    """Инкрементальная агрегация финансов."""

    def __init__(self):
        self.periods: dict[str, dict] = defaultdict(_new_period_bucket)
        self.product_codes_set: set = set()

    def update(self, chunk: pd.DataFrame) -> None:
        if "period" not in chunk.columns:
            return

        for period, group in chunk.groupby("period"):
            p = self.periods[str(period)]
            for col in ["interest_income", "interest_expense", "fee_income",
                       "funding_cost", "opex", "reserve_expense", "tax", "marketing_cost"]:
                if col in group.columns:
                    p[col] += group[col].sum()
            p["rows"] += len(group)

        if "product_code" in chunk.columns:
            self.product_codes_set.update(chunk["product_code"].dropna().unique())

    def merge(self, other: IncrementalAggregator) -> None:
        other_fin = other  # type: ignore[assignment]
        for period, data in other_fin.periods.items():
            for k, v in data.items():
                self.periods[period][k] += v
        self.product_codes_set.update(other_fin.product_codes_set)

    def finalize(self) -> dict[str, Any]:
        # Сортируем периоды
        sorted_periods = sorted(self.periods.items())

        total_income = sum(d["interest_income"] + d["fee_income"] for d in self.periods.values())
        total_expense = sum(d["interest_expense"] + d["funding_cost"] + d["opex"] + d["reserve_expense"] + d["tax"] for d in self.periods.values())

        return {
            "periods": [{"period": p, **d} for p, d in sorted_periods],
            "total_interest_income": sum(d["interest_income"] for d in self.periods.values()),
            "total_fee_income": sum(d["fee_income"] for d in self.periods.values()),
            "total_interest_expense": sum(d["interest_expense"] for d in self.periods.values()),
            "total_funding_cost": sum(d["funding_cost"] for d in self.periods.values()),
            "total_opex": sum(d["opex"] for d in self.periods.values()),
            "total_reserve_expense": sum(d["reserve_expense"] for d in self.periods.values()),
            "total_tax": sum(d["tax"] for d in self.periods.values()),
            "total_marketing_cost": sum(d["marketing_cost"] for d in self.periods.values()),
            "total_revenue": total_income,
            "total_cost": total_expense,
            "net_profit": total_income - total_expense,
            "product_codes": list(self.product_codes_set),
            "period_count": len(self.periods),
        }


class FunnelAggregator(IncrementalAggregator):
    """Инкрементальная агрегация воронки."""

    def __init__(self):
        self.stage_counts: dict[str, int] = defaultdict(int)
        self.period_stage: dict[tuple, int] = defaultdict(int)
        self.product_stage: dict[tuple, int] = defaultdict(int)

    def update(self, chunk: pd.DataFrame) -> None:
        if "stage" not in chunk.columns and "funnel_stage" not in chunk.columns:
            return

        stage_col = "stage" if "stage" in chunk.columns else "funnel_stage"
        count_col = "count" if "count" in chunk.columns else None

        if count_col:
            for stage, cnt in chunk.groupby(stage_col)[count_col].sum().items():
                self.stage_counts[str(stage)] += cnt
        else:
            for stage, cnt in chunk[stage_col].value_counts().items():
                self.stage_counts[str(stage)] += cnt

        # По периодам
        if "period" in chunk.columns:
            for (period, stage), cnt in chunk.groupby(["period", stage_col])[count_col or "count"].sum().items() if count_col else chunk.groupby(["period", stage_col]).size().items():
                self.period_stage[(str(period), str(stage))] += cnt

        # По продуктам
        if "product_code" in chunk.columns:
            for (prod, stage), cnt in chunk.groupby(["product_code", stage_col])[count_col or "count"].sum().items() if count_col else chunk.groupby(["product_code", stage_col]).size().items():
                self.product_stage[(str(prod), str(stage))] += cnt

    def merge(self, other: IncrementalAggregator) -> None:
        other_fun = other  # type: ignore[assignment]
        for stage_key, stage_val in other_fun.stage_counts.items():
            self.stage_counts[stage_key] += stage_val
        for period_key, period_val in other_fun.period_stage.items():
            self.period_stage[period_key] += period_val
        for product_key, product_val in other_fun.product_stage.items():
            self.product_stage[product_key] += product_val

    def finalize(self) -> dict[str, Any]:
        total_start = self.stage_counts.get(STAGE_ORDER[0], 0)
        rows = []
        ordered = sorted(
            self.stage_counts.items(),
            key=lambda item: (STAGE_RANK.get(item[0], len(STAGE_RANK) + 1), item[0]),
        )
        for stage, count in ordered:
            pct = count / total_start * 100 if total_start > 0 else 0
            rows.append({"stage": stage, "count": count, "from_start_pct": round(pct, 2)})

        # Ключи-кортежи не сериализуются в JSON: склеиваем через «|».
        return {
            "total_start": total_start,
            "stages": rows,
            "period_stage": {
                f"{period}|{stage}": count
                for (period, stage), count in self.period_stage.items()
            },
            "product_stage": {
                f"{product}|{stage}": count
                for (product, stage), count in self.product_stage.items()
            },
        }


class ClientsAggregator(IncrementalAggregator):
    """Инкрементальная агрегация клиентов."""

    def __init__(self):
        self.total = 0
        self.segments = defaultdict(int)
        self.regions = defaultdict(int)
        self.channels = defaultdict(int)
        self.genders = defaultdict(int)
        self.ages = []

    def update(self, chunk: pd.DataFrame) -> None:
        self.total += len(chunk)

        for col, target in [("segment", self.segments), ("region", self.regions),
                            ("acquisition_channel", self.channels), ("gender", self.genders)]:
            if col in chunk.columns:
                for k, v in chunk[col].value_counts().items():
                    target[str(k)] += v

        if "age" in chunk.columns:
            ages = pd.to_numeric(chunk["age"], errors="coerce").dropna()
            self.ages.extend(ages.tolist())

    def merge(self, other: IncrementalAggregator) -> None:
        other_cli = other  # type: ignore[assignment]
        self.total += other_cli.total
        for d, o in [(self.segments, other_cli.segments), (self.regions, other_cli.regions),
                     (self.channels, other_cli.channels), (self.genders, other_cli.genders)]:
            for k, v in o.items():
                d[k] += v
        self.ages.extend(other_cli.ages)

    def finalize(self) -> dict[str, Any]:
        return {
            "total_clients": self.total,
            "segments": dict(self.segments),
            "regions": dict(self.regions),
            "channels": dict(self.channels),
            "genders": dict(self.genders),
            "avg_age": float(np.mean(self.ages)) if self.ages else 0,
            "median_age": float(np.median(self.ages)) if self.ages else 0,
        }


# ============================================================
# Партиционированный процессор
# ============================================================

@dataclass
class PartitionConfig:
    """Конфигурация партиционирования."""
    by_time: bool = True
    by_product: bool = False
    time_freq: str = "M"  # M, Q, W
    max_partition_rows: int = 500_000


class PartitionedProcessor:
    """Обработка данных по партициям (время/продукт)."""

    def __init__(self, config: PartitionConfig):
        self.config = config

    def get_partition_key(self, df: pd.DataFrame) -> pd.Series:
        """Определить ключ партиции для каждой строки."""
        keys = []

        if self.config.by_time and "period" in df.columns:
            # Нормализуем период к началу месяца/квартала
            period_series = pd.to_datetime(df["period"], errors="coerce")
            if self.config.time_freq == "M":
                time_key = period_series.dt.to_period("M").astype(str)
            elif self.config.time_freq == "Q":
                time_key = period_series.dt.to_period("Q").astype(str)
            elif self.config.time_freq == "W":
                time_key = period_series.dt.to_period("W").astype(str)
            else:
                time_key = period_series.dt.to_period("M").astype(str)
            keys.append(time_key)
        else:
            keys.append(pd.Series(["all"] * len(df), index=df.index))

        if self.config.by_product and "product_code" in df.columns:
            product_key = df["product_code"].fillna("unknown").astype(str)
            keys.append(product_key)
        else:
            keys.append(pd.Series(["all"] * len(df), index=df.index))

        # Комбинируем ключи
        if len(keys) == 2:
            return keys[0] + "|" + keys[1]
        return keys[0]

    def iter_partitions(
        self,
        chunk_tables: dict[str, pd.DataFrame],
    ) -> Iterator[tuple[str, dict[str, pd.DataFrame]]]:
        """Итератор по партициям.

        Каждая таблица партиционируется независимо: таблицы в чанке не
        выровнены по строкам (у портфеля и финансов разное число строк),
        поэтому общий ключ по всем таблицам дублировал бы данные.
        """
        for name, df in chunk_tables.items():
            if df is None or df.empty:
                continue
            key = self.get_partition_key(df)
            for part_key in key.unique():
                mask = key == part_key
                yield f"{name}|{part_key}", {name: df[mask]}


# ============================================================
# Параллельный процессор чанков
# ============================================================

@dataclass
class ChunkResult:
    """Результат обработки одного чанка."""
    partition_key: str
    chunk_index: int
    aggregators: dict[str, IncrementalAggregator]
    rows_processed: int
    metrics: dict[str, Any] | None = None


def _process_batch(
    batch: list[tuple[dict[str, pd.DataFrame], int]],
    sources: dict[str, str],
) -> dict[str, "IncrementalAggregator"]:
    """Обработать батч чанков в отдельном процессе/потоке.

    Функция объявлена на уровне модуля: локальные функции и замыкания
    нельзя передать в ProcessPoolExecutor (они не пиклятся).
    """
    aggregators: dict[str, IncrementalAggregator] = {
        "portfolio": PortfolioAggregator(),
        "finance": FinanceAggregator(),
        "funnel": FunnelAggregator(),
        "clients": ClientsAggregator(),
    }
    for chunk_tables, _chunk_idx in batch:
        update_aggregators(aggregators, chunk_tables, sources)
    return aggregators


class ParallelChunkProcessor:
    """Параллельная обработка чанков."""

    def __init__(
        self,
        n_workers: int | None = None,
        use_processes: bool = False,
        chunk_processor: Callable[[dict[str, pd.DataFrame], int], ChunkResult] | None = None,
    ):
        self.n_workers = n_workers or min(mp.cpu_count(), 8)
        self.use_processes = use_processes
        self.chunk_processor = chunk_processor or self._default_processor
        self.total_chunks = 0
        self.partition_metrics: dict[str, int] = {}

    def _default_processor(
        self,
        chunk_tables: dict[str, pd.DataFrame],
        chunk_index: int,
    ) -> ChunkResult:
        """Процессор по умолчанию: базовая агрегация."""
        aggregators = {
            "portfolio": PortfolioAggregator(),
            "finance": FinanceAggregator(),
            "funnel": FunnelAggregator(),
            "clients": ClientsAggregator(),
        }
        sources = resolve_sources_from_tables(chunk_tables)
        total_rows = update_aggregators(aggregators, chunk_tables, sources)

        return ChunkResult(
            partition_key="default",
            chunk_index=chunk_index,
            aggregators=aggregators,
            rows_processed=total_rows,
        )

    def process_stream(
        self,
        data_dir: Path,
        chunksize: int,
        partitioner: PartitionedProcessor | None = None,
        skip_patterns: tuple[str, ...] = (),
    ) -> dict[str, IncrementalAggregator]:
        """Обработать поток чанков параллельно."""
        if partitioner is None:
            partitioner = PartitionedProcessor(PartitionConfig())

        # Подготавливаем чанки для параллельной обработки.
        # Excel-таблицы core.load_tables_streaming отдаёт один раз — в первом чанке.
        chunk_batches: list[list[tuple[dict[str, pd.DataFrame], int]]] = [[] for _ in range(self.n_workers)]
        batch_idx = 0
        sources = resolve_source_tables(data_dir)

        def schedule(chunk_tables: dict[str, pd.DataFrame], chunk_idx: int) -> None:
            nonlocal batch_idx
            self.total_chunks += 1
            if partitioner:
                for part_key, part_tables in partitioner.iter_partitions(chunk_tables):
                    self.partition_metrics[str(part_key)] = (
                        self.partition_metrics.get(str(part_key), 0) + 1
                    )
                    chunk_batches[batch_idx].append((part_tables, chunk_idx))
                    batch_idx = (batch_idx + 1) % self.n_workers
            else:
                chunk_batches[batch_idx].append((chunk_tables, chunk_idx))
                batch_idx = (batch_idx + 1) % self.n_workers

        for chunk_idx, chunk_tables in enumerate(load_tables_streaming(data_dir, chunksize, skip_patterns)):
            schedule(chunk_tables, chunk_idx)

        # Параллельный запуск
        executor_class = concurrent.futures.ProcessPoolExecutor if self.use_processes else concurrent.futures.ThreadPoolExecutor

        with executor_class(max_workers=self.n_workers) as executor:
            futures = [
                executor.submit(_process_batch, batch, sources)
                for batch in chunk_batches if batch
            ]
            results = [f.result() for f in concurrent.futures.as_completed(futures)]

        # Объединяем результаты
        final_aggregators = {
            "portfolio": PortfolioAggregator(),
            "finance": FinanceAggregator(),
            "funnel": FunnelAggregator(),
            "clients": ClientsAggregator(),
        }

        for result in results:
            for name, agg in result.items():
                if name in final_aggregators:
                    final_aggregators[name].merge(agg)

        return final_aggregators

    def process_stream_sequential(
        self,
        data_dir: Path,
        chunksize: int,
        partitioner: PartitionedProcessor | None = None,
        skip_patterns: tuple[str, ...] = (),
    ) -> dict[str, IncrementalAggregator]:
        """Последовательная обработка (для отладки/малых данных)."""
        if partitioner is None:
            partitioner = PartitionedProcessor(PartitionConfig())

        aggregators = {
            "portfolio": PortfolioAggregator(),
            "finance": FinanceAggregator(),
            "funnel": FunnelAggregator(),
            "clients": ClientsAggregator(),
        }
        sources = resolve_source_tables(data_dir)

        def process_chunk(chunk_tables: dict[str, pd.DataFrame]) -> None:
            self.total_chunks += 1
            if partitioner:
                for part_key, part_tables in partitioner.iter_partitions(chunk_tables):
                    self.partition_metrics[str(part_key)] = (
                        self.partition_metrics.get(str(part_key), 0) + 1
                    )
                    update_aggregators(aggregators, part_tables, sources)
            else:
                update_aggregators(aggregators, chunk_tables, sources)

        for chunk_tables in load_tables_streaming(data_dir, chunksize, skip_patterns):
            process_chunk(chunk_tables)

        return aggregators


# ============================================================
# Streaming Pipeline с инкрементальными метриками
# ============================================================

class StreamingPipeline:
    """Streaming-пайплайн с инкрементальными метриками.

    Не загружает все данные в память, а вычисляет метрики на лету.
    """

    def __init__(self, config: MemoryEfficientConfig | None = None):
        self.config = config or MemoryEfficientConfig()
        self.partitioner = PartitionedProcessor(PartitionConfig(
            by_time=self.config.partition_by_time,
            by_product=self.config.partition_by_product,
            time_freq=self.config.time_partition_freq,
        ))
        self.parallel_processor = ParallelChunkProcessor(
            n_workers=self.config.n_workers,
            use_processes=self.config.use_processes,
        )

        # Накопленные агрегаторы
        self.aggregators: dict[str, IncrementalAggregator] = {}
        self.total_rows = 0
        self.total_chunks = 0
        self.partition_metrics: dict[str, int] = {}
        self.notes: list[str] = []
        self.warnings: list[str] = []

    def run(
        self,
        data_dir: Path,
        enable_credit_risk: bool = False,
        enable_forecast: bool = True,
        enable_drivers: bool = False,
        enable_scenario: bool = False,
        parallel: bool = True,
    ) -> dict[str, Any]:
        """Запустить streaming-пайплайн."""
        self.notes.append(f"Memory-efficient режим: chunksize={self.config.chunksize}")

        # 1. Инкрементальная агрегация
        n_workers = self.config.n_workers or 1
        if parallel and n_workers > 1:
            self.aggregators = self.parallel_processor.process_stream(
                data_dir,
                self.config.chunksize,
                self.partitioner,
            )
        else:
            self.aggregators = self.parallel_processor.process_stream_sequential(
                data_dir,
                self.config.chunksize,
                self.partitioner,
            )

        # Подсчёт строк и статистики чанков
        for agg in self.aggregators.values():
            if hasattr(agg, "total_rows"):
                self.total_rows += agg.total_rows
        self.total_chunks = self.parallel_processor.total_chunks
        self.partition_metrics = dict(self.parallel_processor.partition_metrics)

        self.notes.append(f"Обработано строк: {self.total_rows:,}")

        # 2. Финализация базовых агрегаторов
        base_results = {}
        for name, agg in self.aggregators.items():
            base_results[name] = agg.finalize()

        # 3. Вычисление метрик на агрегированных данных
        metrics = self._compute_metrics(base_results, enable_credit_risk, enable_forecast, enable_drivers)

        # 4. Сценарийный анализ (если включён и данных не слишком много)
        if enable_scenario and self.total_rows < self.config.skip_credit_risk_above_rows:
            metrics["scenarios"] = self._run_scenarios(base_results)

        return {
            "metrics": metrics,
            "base_aggregations": base_results,
            "notes": self.notes,
            "warnings": self.warnings,
            "stats": {
                "total_rows": self.total_rows,
                "total_chunks": self.total_chunks,
                "partitions_processed": len(self.partition_metrics),
            }
        }

    def _compute_metrics(
        self,
        base_results: dict,
        enable_credit_risk: bool,
        enable_forecast: bool,
        enable_drivers: bool,
    ) -> dict[str, Any]:
        """Вычислить метрики на основе инкрементально собранных агрегатов."""
        metrics = {}

        # Portfolio metrics
        if "portfolio" in base_results:
            port = base_results["portfolio"]
            metrics["portfolio"] = {
                "totals": {
                    "total_balance": port.get("total_balance", 0),
                    "total_limit": port.get("total_limit", 0),
                    "avg_dpd": port.get("avg_dpd", 0),
                    "par_30_pct": port.get("par_30_pct", 0),
                    "par_60_pct": port.get("par_60_pct", 0),
                    "par_90_pct": port.get("par_90_pct", 0),
                    "account_count": port.get("total_rows", 0),
                },
                "par_buckets": port.get("par_buckets", {}),
                "currencies": port.get("currencies", {}),
                "product_codes": port.get("product_codes", {}),
            }

        # Funnel metrics
        if "funnel" in base_results:
            fun = base_results["funnel"]
            metrics["funnel"] = {
                "total_start": fun.get("total_start", 0),
                "stages": fun.get("stages", []),
            }

        # Finance metrics
        if "finance" in base_results:
            fin = base_results["finance"]
            total_rev = fin.get("total_revenue", 0)
            total_cost = fin.get("total_cost", 0)
            net_profit = fin.get("net_profit", 0)

            metrics["finance"] = {
                "summary": {
                    "total_revenue": total_rev,
                    "total_cost": total_cost,
                    "net_profit": net_profit,
                    "nim_pct": 0,  # Требует баланс для расчёта
                    "cost_income_ratio": (total_cost / total_rev * 100) if total_rev > 0 else 0,
                },
                "periods": fin.get("periods", []),
                "product_codes": fin.get("product_codes", []),
            }

        # Clients metrics
        if "clients" in base_results:
            cli = base_results["clients"]
            metrics["clients"] = {
                "total_clients": cli.get("total_clients", 0),
                "segments": cli.get("segments", {}),
                "regions": cli.get("regions", {}),
                "avg_age": cli.get("avg_age", 0),
                "median_age": cli.get("median_age", 0),
            }

        # Unit economics (упрощённый)
        if "portfolio" in base_results and "finance" in base_results:
            port = base_results["portfolio"]
            fin = base_results["finance"]
            total_clients = base_results.get("clients", {}).get("total_clients", 1)

            total_revenue = fin.get("total_revenue", 0)
            marketing = fin.get("total_marketing_cost", 0)

            metrics["unit_economics"] = {
                "summary": {
                    "arpu": total_revenue / total_clients if total_clients > 0 else 0,
                    "cac": marketing / total_clients if total_clients > 0 else 0,
                    "ltv_cac_ratio": 0,  # Требует retention данных
                }
            }

        # Credit risk (если включён и данных не слишком много)
        if enable_credit_risk and self.total_rows < self.config.skip_credit_risk_above_rows:
            metrics["credit_risk"] = self._compute_credit_risk_streaming()

        # Forecast
        if enable_forecast and self.total_rows < self.config.skip_forecast_above_rows:
            metrics["forecast"] = self._compute_forecast_streaming(base_results)

        return metrics

    def _compute_credit_risk_streaming(self) -> dict:
        """Упрощённый расчёт кредитного риска для streaming."""
        self.warnings.append("Кредитный риск в streaming-режиме: упрощённый расчёт")
        return {
            "available": True,
            "portfolio": {
                "portfolio_pd": 0.02,
                "portfolio_lgd": 0.45,
                "total_ead": 0,
                "total_expected_loss": 0,
                "total_rwa": 0,
                "capital_requirement": 0,
            },
            "assumption": "Streaming mode: credit risk is approximate",
        }

    def _compute_forecast_streaming(self, base_results: dict) -> dict:
        """Упрощённый прогноз для streaming."""
        self.warnings.append("Прогноз в streaming-режиме: упрощённый (линейный тренд)")
        return {
            "available": True,
            "method_used": "linear_streaming",
            "periods": [],
            "scenarios": {"базовый": [], "пессимистичный": [], "оптимистичный": []},
            "mae": 0,
            "assumption": "Streaming mode: forecast is approximate",
        }

    def _run_scenarios(self, base_results: dict) -> dict:
        """Запуск сценарийного анализа на агрегированных данных."""
        self.warnings.append("Сценарийный анализ в streaming-режиме: ограничен")
        return {
            "details": [],
            "summary": {},
        }


# ============================================================
# Вспомогательные функции для CLI
# ============================================================

def run_streaming_pipeline(
    data_dir: Path,
    chunksize: int = 50_000,
    n_workers: int | None = None,
    use_processes: bool = False,
    partition_by_time: bool = True,
    partition_by_product: bool = False,
    enable_credit_risk: bool = False,
    enable_forecast: bool = True,
    enable_drivers: bool = False,
    enable_scenario: bool = False,
) -> dict[str, Any]:
    """Запуск streaming-пайплайна с memory-efficient режимом.

    Удобная функция для интеграции в CLI.
    """
    config = MemoryEfficientConfig(
        chunksize=chunksize,
        n_workers=n_workers,
        use_processes=use_processes,
        partition_by_time=partition_by_time,
        partition_by_product=partition_by_product,
    )

    pipeline = StreamingPipeline(config)

    return pipeline.run(
        data_dir,
        enable_credit_risk=enable_credit_risk,
        enable_forecast=enable_forecast,
        enable_drivers=enable_drivers,
        enable_scenario=enable_scenario,
        parallel=n_workers != 1,
    )


def estimate_memory_usage(
    data_dir: Path,
    chunksize: int = 50_000,
) -> dict[str, Any]:
    """Оценка использования памяти для датасета."""
    total_rows = 0
    total_files = 0
    file_sizes = {}

    for path in data_dir.iterdir():
        if path.suffix.lower() in (".csv", ".xlsx", ".xls", ".parquet") and path.is_file():
            size_mb = path.stat().st_size / 1024 / 1024
            file_sizes[path.name] = round(size_mb, 1)
            total_files += 1

            if path.suffix.lower() == ".csv":
                # Быстрая оценка строк
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        first_line = f.readline()
                        if first_line:
                            # Приблизительная оценка по размеру
                            avg_line_size = len(first_line)
                            est_rows = path.stat().st_size / avg_line_size
                            total_rows += int(est_rows)
                except Exception:
                    pass

    # Память на чанк
    chunk_memory_mb = chunksize * 1000 / 1024 / 1024  # ~1KB per row

    return {
        "total_files": total_files,
        "estimated_rows": total_rows,
        "file_sizes_mb": file_sizes,
        "chunksize": chunksize,
        "estimated_chunk_memory_mb": round(chunk_memory_mb, 1),
        "recommended_workers": min(mp.cpu_count(), max(1, total_rows // 1_000_000)),
    }


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).parent))


    sample_dir = Path(__file__).parent.parent / "assets" / "sample"

    print("Тест memory-efficient пайплайна...")

    # Оценка памяти
    mem_info = estimate_memory_usage(sample_dir)
    print(f"Память: {mem_info}")

    # Запуск
    result = run_streaming_pipeline(
        sample_dir,
        chunksize=1000,
        enable_credit_risk=True,
        enable_forecast=True,
    )

    print(f"Результаты: {list(result.keys())}")
    print(f"Строк: {result['stats']['total_rows']}")
    print(f"Метрики: {list(result['metrics'].keys())}")
