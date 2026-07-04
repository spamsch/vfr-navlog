"""End-to-end PDF text snapshot.

Runs render() on the fixture plan with all network features off and a fixed
wind, extracts the text of every page with pypdf, and compares it to a stored
snapshot. This catches content regressions across the refactor. Layout is a
separate manual eyeball check against a reference PDF.
"""
from datetime import datetime
from pathlib import Path

import pypdf
import pytest

import navlog

FIXTURES = Path(__file__).parent / "fixtures"
SNAPSHOT = FIXTURES / "pdf_snapshot.txt"


class _FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 7, 4, 10, 0, 0, tzinfo=tz)


def _render_text(tmp_path: Path) -> str:
    plan = navlog.parse_lnmpln(FIXTURES / "sample.lnmpln")
    aircraft = {
        "type": "C172S",
        "icao_type": "C172",
        "registration": "D-EIYD",
        "performance": {"tas_cruise": 93, "fuel_burn_cruise_lph": 33.3,
                        "fuel_burn_climb_lph": 45.0, "fuel_burn_taxi_lph": 10.0},
        "fuel": {"capacity_usable_l": 201, "reserve_minutes": 30, "taxi_minutes": 12,
                 "approach_minutes": 10, "alternate_minutes": 0},
    }
    wind = (270.0, 10.0)
    magvar = 4.0
    legs = navlog.compute_legs(plan, aircraft["performance"]["tas_cruise"], wind, magvar,
                               aircraft["performance"]["fuel_burn_cruise_lph"])
    navlog.apply_hemispheric_rule(plan, legs)
    out = tmp_path / "navlog.pdf"
    navlog.render(
        plan, aircraft, legs, wind, magvar, out,
        vatsim=None, call_tower_nm=10.0, dest_info=None,
        source_note="TEST SNAPSHOT", fir_icaos=[], weather=None,
        dfs_charts=False, field_wx={},
    )
    reader = pypdf.PdfReader(str(out))
    return "\n=== PAGE ===\n".join(page.extract_text() for page in reader.pages)


@pytest.fixture(autouse=True)
def _freeze_time(monkeypatch):
    monkeypatch.setattr(navlog, "datetime", _FrozenDateTime)


def test_pdf_text_snapshot(tmp_path):
    text = _render_text(tmp_path)
    if not SNAPSHOT.exists():
        SNAPSHOT.write_text(text, encoding="utf-8")
        pytest.skip("snapshot created; re-run to compare")
    expected = SNAPSHOT.read_text(encoding="utf-8")
    assert text == expected
