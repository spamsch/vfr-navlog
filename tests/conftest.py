"""Shared test fixtures and path helpers.

Phase 0 imports the code under test from the single-file `navlog` module.
Phase 1 re-points these imports at the `vfr_navlog` package; the assertions
stay identical so the split is verifiably behaviour-preserving.
"""
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def sample_plan_path() -> Path:
    return FIXTURES / "sample.lnmpln"
