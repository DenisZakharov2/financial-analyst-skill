"""Tests for migration matrix (--migration flag)."""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "financial-analyst" / "scripts"
SAMPLE = ROOT / "skills" / "financial-analyst" / "assets" / "sample"


def _run(data_dir: Path, out_dir: Path, *extra: str) -> dict:
    """Run CLI and return parsed metrics.json."""
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


def _modify_target_for_migration(target_dir: Path) -> None:
    """Modify target portfolio to have delinquency for migration testing."""
    portfolio = pd.read_csv(target_dir / "portfolio.csv")
    # Make some accounts delinquent: first 10 -> 30+, next 5 -> 60+, next 3 -> 90+
    portfolio.loc[0:9, "dpd"] = 45
    portfolio.loc[10:14, "dpd"] = 75
    portfolio.loc[15:17, "dpd"] = 120
    portfolio.to_csv(target_dir / "portfolio.csv", index=False)


def test_migration_flag_adds_section(tmp_path):
    """--migration + --compare-dirs adds migration section to metrics.json."""
    base_dir = _sample_copy(tmp_path, "base")
    target_dir = _sample_copy(tmp_path, "target")
    _modify_target_for_migration(target_dir)

    payload = _run(base_dir, tmp_path / "out",
                   "--compare-dirs", str(base_dir), str(target_dir),
                   "--migration")

    assert "migration" in payload["sections"], "migration section missing"
    section = payload["sections"]["migration"]
    assert "matrix" in section
    assert section["mode"] == "clients"
    assert section["labels"] == ["base", "target"]

    # Check matrix structure: 4x4 with all buckets
    matrix = section["matrix"]
    assert len(matrix) == 4, f"expected 4 rows, got {len(matrix)}"
    buckets = {"current", "30+", "60+", "90+"}
    for row in matrix:
        assert row["base_bucket"] in buckets
        assert set(row.keys()) == buckets | {"base_bucket"}


def test_migration_row_sums_to_100(tmp_path):
    """Each row of migration matrix sums to 100% (or 0 for empty rows)."""
    base_dir = _sample_copy(tmp_path, "base")
    target_dir = _sample_copy(tmp_path, "target")
    _modify_target_for_migration(target_dir)

    payload = _run(base_dir, tmp_path / "out",
                   "--compare-dirs", str(base_dir), str(target_dir),
                   "--migration")

    matrix = payload["sections"]["migration"]["matrix"]
    for row in matrix:
        row_sum = sum(row[b] for b in ["current", "30+", "60+", "90+"])
        # Allow small floating point deviation
        assert abs(row_sum - 100.0) < 0.01 or abs(row_sum - 0.0) < 0.01, \
            f"row {row['base_bucket']} sums to {row_sum}, expected 100 or 0"


def test_migration_mode_balance(tmp_path):
    """--migration-mode balance computes share of balance, not clients."""
    base_dir = _sample_copy(tmp_path, "base")
    target_dir = _sample_copy(tmp_path, "target")
    _modify_target_for_migration(target_dir)

    payload = _run(base_dir, tmp_path / "out",
                   "--compare-dirs", str(base_dir), str(target_dir),
                   "--migration", "--migration-mode", "balance")

    section = payload["sections"]["migration"]
    assert section["mode"] == "balance"

    # Matrix should still be valid (rows sum to 100)
    matrix = section["matrix"]
    for row in matrix:
        row_sum = sum(row[b] for b in ["current", "30+", "60+", "90+"])
        assert abs(row_sum - 100.0) < 0.01 or abs(row_sum - 0.0) < 0.01


def test_migration_without_compare_dirs_is_skipped(tmp_path):
    """--migration without --compare-dirs produces warning, no section."""
    data_dir = _sample_copy(tmp_path)
    payload = _run(data_dir, tmp_path / "out", "--migration")

    assert "migration" not in payload["sections"]
    assert any("Миграция пропущена" in w for w in payload["warnings"])


def test_migration_without_portfolio_is_skipped(tmp_path):
    """--migration without portfolio table produces warning."""
    base_dir = _sample_copy(tmp_path, "base")
    target_dir = _sample_copy(tmp_path, "target")
    (base_dir / "portfolio.csv").unlink()
    (target_dir / "portfolio.csv").unlink()

    payload = _run(base_dir, tmp_path / "out",
                   "--compare-dirs", str(base_dir), str(target_dir),
                   "--migration")

    assert "migration" not in payload["sections"]
    assert any("нет таблицы portfolio" in w for w in payload["warnings"])


def test_migration_default_flags_do_not_change_report(tmp_path):
    """Without --migration flag, default report unchanged."""
    data_dir = _sample_copy(tmp_path)
    payload = _run(data_dir, tmp_path / "out")

    # Default sections (from test_cli_extensions.py)
    assert set(payload["sections"]) == {"portfolio", "funnel", "funnel_tat", "finance", "unit_economics"}
    assert "migration" not in payload["sections"]
    assert "vintage" not in payload["sections"]


def test_migration_report_files_created(tmp_path):
    """Reports generated with migration section."""
    base_dir = _sample_copy(tmp_path, "base")
    target_dir = _sample_copy(tmp_path, "target")
    _modify_target_for_migration(target_dir)

    out_dir = tmp_path / "out"
    command = [
        sys.executable, str(SCRIPTS / "analyze.py"),
        "--data-dir", str(base_dir), "--out", str(out_dir),
        "--format", "docx,xlsx",
        "--compare-dirs", str(base_dir), str(target_dir),
        "--migration", "--quiet",
    ]
    result = subprocess.run(command, capture_output=True, text=True, cwd=str(ROOT))
    assert result.returncode == 0, result.stderr or result.stdout
    assert (out_dir / "financial_analyst_report.docx").exists()
    assert (out_dir / "financial_analyst_report.xlsx").exists()
