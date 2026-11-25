#!/usr/bin/env python3
"""Run postgres integration tests in a subprocess with clean environment."""

import os
import subprocess
import sys

# Add the project root to path
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Set up environment
env = os.environ.copy()
env["PGHOST"] = "localhost"
env["PGPORT"] = "5432"
env["PGDATABASE"] = "testdb"
env["PGUSER"] = "testuser"
env["PGPASSWORD"] = "testpass"
env["PYTHONDONTWRITEBYTECODE"] = "1"

# Run pytest in a subprocess with clean environment
cmd = [
    sys.executable, "-m", "pytest",
    "-q", "tests/test_postgres.py",
    "--cov=finanalyst.adapters.postgres",
    "--cov-report=term-missing",
    "--import-mode=importlib",
    "-p", "no:cacheprovider"
]

result = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True)
print(result.stdout)
if result.stderr:
    print(result.stderr, file=sys.stderr)
sys.exit(result.returncode)
