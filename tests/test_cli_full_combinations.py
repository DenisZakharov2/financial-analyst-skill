"""Интеграционные тесты на комбинации расширенных флагов CLI.

Проверяется не только запуск, но и структура metrics.json: наличие разделов,
ключевые поля, тождества разложений (сумма эффектов PVM = общий дельта, разложение
сдвига PAR) и плоский список метрик (section, metric, dimension, value).
"""

import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "financial-analyst" / "scripts"
SAMPLE = ROOT / "skills" / "financial-analyst" / "assets" / "sample"

EXPECTED_METRIC_KEYS = {"section", "metric", "dimension", "value"}


def _run(data_dir: Path, out_dir: Path, *extra: str) -> dict:
    """Запустить CLI и вернуть разобранный metrics.json."""
    command = [
        sys.executable, str(SCRIPTS / "analyze.py"),
        "--data-dir", str(data_dir),
        "--out", str(out_dir),
        "--format", "xlsx",
        "--quiet",
        *extra,
    ]
    result = subprocess.run(command, capture_output=True, text=True, cwd=str(ROOT))
    assert result.returncode == 0, result.stderr or result.stdout
    return json.loads((out_dir / "metrics.json").read_text(encoding="utf-8"))


def _sample_copy(tmp_path: Path, name: str = "data") -> Path:
    target = tmp_path / name
    target.mkdir(parents=True, exist_ok=True)
    for path in SAMPLE.glob("*.csv"):
        target.joinpath(path.name).write_bytes(path.read_bytes())
    return target


def _add_experiment_group(data_dir: Path, seed: int = 7, effect: float = 1.05) -> None:
    """Разметить сделки по клиентам: половина control, половина treatment."""
    rng = np.random.default_rng(seed)
    path = data_dir / "sales.csv"
    frame = pd.read_csv(path)
    clients = sorted(frame["client_id"].unique())
    rng.shuffle(clients)
    half = len(clients) // 2
    mapping = {client: ("control" if index < half else "treatment")
               for index, client in enumerate(clients)}
    frame["experiment_group"] = frame["client_id"].map(mapping)
    frame["amount"] = frame["amount"] * np.where(frame["experiment_group"] == "treatment", effect, 1.0)
    frame.to_csv(path, index=False)


def _assert_metric_rows(metrics: list[dict]) -> None:
    assert metrics, "список метрик пуст"
    for row in metrics:
        assert set(row) == EXPECTED_METRIC_KEYS, row
        assert row["value"] is None or isinstance(row["value"], (int, float)), row


def test_full_combo_writes_all_sections(tmp_path):
    """--forecast-model auto --ab-test --segment --drivers --compare-dirs вместе."""
    base_dir = _sample_copy(tmp_path, "base")
    target_dir = _sample_copy(tmp_path, "target")
    _add_experiment_group(base_dir)
    sales_t = pd.read_csv(target_dir / "sales.csv")
    sales_t["amount"] = sales_t["amount"] * 1.10
    sales_t.to_csv(target_dir / "sales.csv", index=False)

    payload = _run(base_dir, tmp_path / "out",
                   "--forecast-model", "auto",
                   "--ab-test", "--ab-metric", "amount",
                   "--segment",
                   "--drivers",
                   "--compare-dirs", str(base_dir), str(target_dir),
                   "--labels", "base", "target")

    sections = payload["sections"]
    for key in ("portfolio", "funnel", "funnel_tat", "finance", "unit_economics",
                "ab_test", "segmentation", "comparison", "pvm", "par_shift"):
        assert key in sections, f"раздел {key!r} отсутствует в metrics.json"

    ab = sections["ab_test"]
    assert ab.get("available") is True
    checks = {check["check_id"]: check for check in payload["quality"]["checks"]}
    assert checks["ab_srm_check"]["status"] == "pass"

    fin_forecast = sections.get("finance_forecast")
    assert fin_forecast is not None and fin_forecast.get("available") is True
    assert fin_forecast.get("method_used") in {"linear", "ets", "arx"}
    assert len(fin_forecast["scenarios"]["базовый"]) > 0

    pvm = sections["pvm"]["effects"]
    assert abs(sum(pvm.values()) - sections["pvm"]["total_delta"]) < 0.01, pvm
    par = sections["par_shift"]
    assert abs(par["within_risk_effect_pp"] + par["structural_mix_effect_pp"] - par["delta_par_pp"]) < 0.01, par

    _assert_metric_rows(payload["metrics"])
    for top in ("meta", "quality", "assumptions", "warnings", "sections", "metrics", "headline", "notes"):
        assert top in payload, f"корневой ключ {top!r} отсутствует"


def test_compare_drivers_without_ab_and_segment(tmp_path):
    """--compare-dirs + --drivers: разделы сравнения без пилота и сегментации."""
    base_dir = _sample_copy(tmp_path, "base")
    target_dir = _sample_copy(tmp_path, "target")
    sales_t = pd.read_csv(target_dir / "sales.csv")
    sales_t["amount"] = sales_t["amount"] * 1.10
    sales_t.to_csv(target_dir / "sales.csv", index=False)

    payload = _run(base_dir, tmp_path / "out",
                   "--compare-dirs", str(base_dir), str(target_dir),
                   "--labels", "base", "target", "--drivers")

    sections = payload["sections"]
    comparison = sections.get("comparison")
    assert comparison is not None
    assert comparison["labels"] == ["base", "target"]
    assert "schema" in comparison and "portfolio_bridge" in comparison and "finance_comparison" in comparison
    assert sections["pvm"]["effects"]
    assert sections["par_shift"]["delta_par_pp"] is not None
    assert "ab_test" not in sections and "segmentation" not in sections


def test_forecast_auto_and_segment_structure(tmp_path):
    """--forecast-model auto + --segment: прогноз и сегменты в metrics.json и metrics.csv."""
    data_dir = _sample_copy(tmp_path)
    payload = _run(data_dir, tmp_path / "out",
                   "--forecast-model", "auto", "--segment")

    sections = payload["sections"]
    fin_forecast = sections.get("finance_forecast")
    assert fin_forecast is not None and fin_forecast.get("available") is True
    assert set(fin_forecast["scenarios"]) == {"пессимистичный", "базовый", "оптимистичный"}
    periods = fin_forecast["periods"]
    assert len(periods) == len(fin_forecast["scenarios"]["базовый"]) > 0

    profiles = sections["segmentation"]["profiles"]
    assert profiles and sum(row["clients_count"] for row in profiles) == len(pd.read_csv(data_dir / "clients.csv"))

    _assert_metric_rows(payload["metrics"])
    assert (tmp_path / "out" / "metrics.csv").exists()
    metrics_csv = pd.read_csv(tmp_path / "out" / "metrics.csv")
    assert list(metrics_csv.columns) == ["section", "metric", "dimension", "value"]
