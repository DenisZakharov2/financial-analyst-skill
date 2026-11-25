"""Tests for vintage analysis (--vintage flag)."""

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


def test_vintage_flag_adds_section(tmp_path):
    """--vintage adds vintage section to metrics.json."""
    data_dir = _sample_copy(tmp_path)
    payload = _run(data_dir, tmp_path / "out", "--vintage")

    assert "vintage" in payload["sections"], "vintage section missing"
    section = payload["sections"]["vintage"]
    assert "rows" in section
    assert section["rows"], "vintage rows empty"

    # Check structure
    rows = section["rows"]
    expected_columns = {"vintage", "accounts", "clients", "balance",
                        "par30_pct", "par60_pct", "par90_pct", "active_rate"}
    for row in rows:
        assert set(row.keys()) == expected_columns


def test_vintage_sum_balance_matches_portfolio_total(tmp_path):
    """Sum of vintage balances ≈ total portfolio balance."""
    data_dir = _sample_copy(tmp_path)
    payload = _run(data_dir, tmp_path / "out", "--vintage")

    vintage_rows = payload["sections"]["vintage"]["rows"]
    vintage_total = sum(row["balance"] for row in vintage_rows)

    # Get portfolio total from portfolio section
    portfolio_total = payload["sections"]["portfolio"]["totals"]["total_balance"]

    # Allow small difference due to filtering (as_of date)
    assert abs(vintage_total - portfolio_total) / portfolio_total < 0.01, \
        f"vintage total {vintage_total} vs portfolio {portfolio_total}"


def test_vintage_par_columns_range(tmp_path):
    """PAR percentages are in valid range [0, 100] or null."""
    data_dir = _sample_copy(tmp_path)
    payload = _run(data_dir, tmp_path / "out", "--vintage")

    rows = payload["sections"]["vintage"]["rows"]
    for row in rows:
        for col in ["par30_pct", "par60_pct", "par90_pct"]:
            val = row[col]
            if val is not None:
                assert 0.0 <= val <= 100.0, f"{col}={val} out of range"


def test_vintage_active_rate_range(tmp_path):
    """active_rate in valid range [0, 100]."""
    data_dir = _sample_copy(tmp_path)
    payload = _run(data_dir, tmp_path / "out", "--vintage")

    rows = payload["sections"]["vintage"]["rows"]
    for row in rows:
        val = row["active_rate"]
        assert 0.0 <= val <= 100.0, f"active_rate={val} out of range"


def test_vintage_monotonic_months(tmp_path):
    """Vintages sorted chronologically."""
    data_dir = _sample_copy(tmp_path)
    payload = _run(data_dir, tmp_path / "out", "--vintage")

    rows = payload["sections"]["vintage"]["rows"]
    vintages = [row["vintage"] for row in rows]
    assert vintages == sorted(vintages), "vintages not sorted"


def test_vintage_without_portfolio_is_skipped(tmp_path):
    """--vintage without portfolio produces warning, no section."""
    data_dir = _sample_copy(tmp_path)
    (data_dir / "portfolio.csv").unlink()

    payload = _run(data_dir, tmp_path / "out", "--vintage")

    assert "vintage" not in payload["sections"]
    assert any("нет таблицы portfolio" in w for w in payload["warnings"])


def test_vintage_default_flags_do_not_change_report(tmp_path):
    """Without --vintage flag, default report unchanged."""
    data_dir = _sample_copy(tmp_path)
    payload = _run(data_dir, tmp_path / "out")

    assert set(payload["sections"]) == {"portfolio", "funnel", "funnel_tat", "finance", "unit_economics"}
    assert "vintage" not in payload["sections"]


def test_vintage_report_files_created(tmp_path):
    """Reports generated with vintage section."""
    data_dir = _sample_copy(tmp_path)
    out_dir = tmp_path / "out"
    command = [
        sys.executable, str(SCRIPTS / "analyze.py"),
        "--data-dir", str(data_dir), "--out", str(out_dir),
        "--format", "docx,xlsx",
        "--vintage", "--quiet",
    ]
    result = subprocess.run(command, capture_output=True, text=True, cwd=str(ROOT))
    assert result.returncode == 0, result.stderr or result.stdout
    assert (out_dir / "financial_analyst_report.docx").exists()
    assert (out_dir / "financial_analyst_report.xlsx").exists()


def test_vintage_par30_ge_par60_ge_par90(tmp_path):
    """PAR30 >= PAR60 >= PAR90 for each vintage (contract check)."""
    data_dir = _sample_copy(tmp_path)
    payload = _run(data_dir, tmp_path / "out", "--vintage")

    rows = payload["sections"]["vintage"]["rows"]
    for row in rows:
        p30 = row["par30_pct"]
        p60 = row["par60_pct"]
        p90 = row["par90_pct"]
        if p30 is not None and p60 is not None:
            assert p30 >= p60 - 1e-9, f"par30 ({p30}) < par60 ({p60}) for {row['vintage']}"
        if p60 is not None and p90 is not None:
            assert p60 >= p90 - 1e-9, f"par60 ({p60}) < par90 ({p90}) for {row['vintage']}"


def test_vintage_with_delinquent_accounts(tmp_path):
    """Vintage correctly computes PAR for delinquent accounts."""
    data_dir = _sample_copy(tmp_path)
    # Modify portfolio to have known delinquency
    portfolio = pd.read_csv(data_dir / "portfolio.csv")
    # Make first vintage (2024-01) have delinquency
    portfolio.loc[portfolio["open_date"].str.startswith("2024-01"), "dpd"] = 45
    portfolio.to_csv(data_dir / "portfolio.csv", index=False)

    payload = _run(data_dir, tmp_path / "out", "--vintage")

    rows = payload["sections"]["vintage"]["rows"]
    jan_row = next(r for r in rows if r["vintage"] == "2024-01")
    assert jan_row["par30_pct"] > 0, "Expected PAR30 > 0 for delinquent vintage"
