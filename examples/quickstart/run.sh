#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
OUT="${1:-$ROOT/reports}"

if [[ -z "${PYTHON:-}" ]]; then
  if [[ -x "$ROOT/.venv/bin/python" ]]; then
    PYTHON="$ROOT/.venv/bin/python"
  else
    PYTHON="python3"
  fi
fi

"$PYTHON" "$ROOT/skills/financial-analyst/scripts/analyze.py" \
  --data-dir "$ROOT/skills/financial-analyst/assets/sample" \
  --out "$OUT" \
  --strict

echo "Отчёты: $OUT"
