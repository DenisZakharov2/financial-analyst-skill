#!/usr/bin/env python3
"""Run postgres integration tests without pytest's import mechanism."""

import sys
import os
import unittest

# Add the project root to path
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# Import test module and run with unittest
from tests.test_postgres import *  # noqa: F403,F401

if __name__ == "__main__":
    # Run with unittest instead of pytest
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromModule(sys.modules[__name__])
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)