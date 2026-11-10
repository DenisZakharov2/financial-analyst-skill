"""Тесты CLI-флагов версии 2.1.0: --segment, --ab-test, --forecast-model, --drivers.

Проверяется не только расчёт, но и то, что новые разделы попадают в отчёт и
в metrics.json, а блокирующее состояние пилота (SRM) останавливает строгий
запуск с кодом 2.
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "financial-analyst" / "scripts"
SAMPLE = ROOT / "skills" / "financial-analyst" / "assets" / "sample"


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
        shutil.copy(path, target / path.name)
    return target


def _add_experiment_group(data_dir: Path, seed: int = 5, effect: float = 1.12) -> None:
    """Разметить сделки по клиентам: половина — control, половина — treatment."""
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


def test_segment_flag_adds_section(tmp_path):
    data_dir = _sample_copy(tmp_path)
    payload = _run(data_dir, tmp_path / "out", "--segment")
    assert "segmentation" in payload["sections"]
    profiles = payload["sections"]["segmentation"]["profiles"]
    assert profiles, "профили сегментов не пусты"
    total = sum(row["clients_count"] for row in profiles)
    assert total == len(pd.read_csv(data_dir / "clients.csv"))


def test_ab_test_flag_adds_section_and_quality_check(tmp_path):
    data_dir = _sample_copy(tmp_path)
    _add_experiment_group(data_dir)
    payload = _run(data_dir, tmp_path / "out", "--ab-test", "--ab-metric", "amount")
    section = payload["sections"].get("ab_test")
    assert section is not None
    assert section["available"] is True
    assert section["srm_check"]["srm_ok"] is True
    checks = {check["check_id"]: check for check in payload["quality"]["checks"]}
    assert checks["ab_srm_check"]["status"] == "pass"
    assert checks["ab_assignment_overlap"]["status"] == "pass"


def test_ab_test_without_group_column_is_skipped(tmp_path):
    data_dir = _sample_copy(tmp_path)
    payload = _run(data_dir, tmp_path / "out", "--ab-test")
    assert "ab_test" not in payload["sections"]
    assert any("A/B-тест пропущен" in note for note in payload["notes"])


def test_srm_violation_fails_strict_run(tmp_path):
    """Нарушенный сплит обязан останавливать строгий запуск с кодом 2.

    Порог SRM намеренно жёсткий (p < 0.001), поэтому перекос должен быть
    грубым: 90 % клиентов в одной группе.
    """
    data_dir = _sample_copy(tmp_path)
    path = data_dir / "sales.csv"
    frame = pd.read_csv(path)
    clients = sorted(frame["client_id"].unique())
    moved = set(clients[: int(len(clients) * 0.9)])
    frame["experiment_group"] = np.where(
        frame["client_id"].isin(moved), "treatment", "control")
    frame.to_csv(path, index=False)

    command = [
        sys.executable, str(SCRIPTS / "analyze.py"),
        "--data-dir", str(data_dir), "--out", str(tmp_path / "out"),
        "--format", "xlsx", "--strict", "--ab-test", "--ab-metric", "amount", "--quiet",
    ]
    result = subprocess.run(command, capture_output=True, text=True, cwd=str(ROOT))
    assert result.returncode == 2, result.stdout
    assert "ab_srm_check" in result.stderr


def test_forecast_model_flag_switches_engine(tmp_path):
    data_dir = _sample_copy(tmp_path)
    legacy = _run(data_dir, tmp_path / "legacy")
    assert "finance_forecast" not in legacy["sections"]

    advanced = _run(data_dir, tmp_path / "advanced", "--forecast-model", "auto")
    forecast = advanced["sections"].get("finance_forecast")
    assert forecast is not None
    assert forecast["method_used"] in {"linear", "ets", "arx"}
    assert forecast["smape_pct"] is not None


def test_drivers_flag_adds_pvm_and_par_shift(tmp_path):
    base_dir = _sample_copy(tmp_path, "base")
    target_dir = _sample_copy(tmp_path, "target")
    sales = pd.read_csv(target_dir / "sales.csv")
    sales["amount"] = sales["amount"] * 1.10
    sales.to_csv(target_dir / "sales.csv", index=False)

    payload = _run(base_dir, tmp_path / "out",
                   "--compare-dirs", str(base_dir), str(target_dir),
                   "--labels", "Q1", "Q2", "--drivers")
    pvm = payload["sections"].get("pvm")
    assert pvm is not None
    assert abs(sum(pvm["effects"].values()) - pvm["total_delta"]) < 0.01
    par_shift = payload["sections"].get("par_shift")
    assert par_shift is not None
    total = par_shift["within_risk_effect_pp"] + par_shift["structural_mix_effect_pp"]
    assert abs(total - par_shift["delta_par_pp"]) < 0.01


def test_new_flags_do_not_change_default_report(tmp_path):
    """Без новых флагов набор разделов прежний: обратная совместимость."""
    data_dir = _sample_copy(tmp_path)
    payload = _run(data_dir, tmp_path / "out")
    assert set(payload["sections"]) == {"portfolio", "funnel", "funnel_tat", "finance", "unit_economics"}


def test_report_files_created_with_new_sections(tmp_path):
    data_dir = _sample_copy(tmp_path)
    out_dir = tmp_path / "out"
    command = [
        sys.executable, str(SCRIPTS / "analyze.py"),
        "--data-dir", str(data_dir), "--out", str(out_dir),
        "--format", "docx,xlsx", "--segment", "--forecast-model", "auto", "--quiet",
    ]
    result = subprocess.run(command, capture_output=True, text=True, cwd=str(ROOT))
    assert result.returncode == 0, result.stderr or result.stdout
    assert (out_dir / "financial_analyst_report.docx").exists()
    assert (out_dir / "financial_analyst_report.xlsx").exists()
