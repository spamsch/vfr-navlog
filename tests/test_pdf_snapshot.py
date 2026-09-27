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

import vfr_navlog.pdf
from vfr_navlog.legs import apply_hemispheric_rule, compute_legs
from vfr_navlog.lnmpln import parse_lnmpln
from vfr_navlog.model import RenderContext

FIXTURES = Path(__file__).parent / "fixtures"
# (table_style, phraseology) → snapshot file. "classic" + phraseology is the
# original layout and must stay byte-identical; "notes" is the default.
SNAPSHOTS = {
    ("classic", True): FIXTURES / "pdf_snapshot.txt",
    ("notes", False): FIXTURES / "pdf_snapshot_notes.txt",
}


class _FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 7, 4, 10, 0, 0, tzinfo=tz)


def _render_text(tmp_path: Path, table_style: str, phraseology: bool) -> str:
    plan = parse_lnmpln(FIXTURES / "sample.lnmpln")
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
    legs = compute_legs(plan, aircraft["performance"]["tas_cruise"], wind, magvar,
                        aircraft["performance"]["fuel_burn_cruise_lph"])
    apply_hemispheric_rule(plan, legs)
    out = tmp_path / "navlog.pdf"
    ctx = RenderContext(
        plan=plan, aircraft=aircraft, legs=legs, wind=wind, magvar=magvar,
        vatsim=None, dest_info=None, weather=None, field_wx={},
        fir_icaos=[], source_note="TEST SNAPSHOT", call_tower_nm=10.0,
        with_dfs_charts=False, table_style=table_style, phraseology=phraseology,
    )
    vfr_navlog.pdf.render(ctx, out)
    reader = pypdf.PdfReader(str(out))
    return "\n=== PAGE ===\n".join(page.extract_text() for page in reader.pages)


@pytest.fixture(autouse=True)
def _freeze_time(monkeypatch):
    # render() reads datetime from the pdf package module.
    monkeypatch.setattr(vfr_navlog.pdf, "datetime", _FrozenDateTime)


@pytest.mark.parametrize("table_style,phraseology", list(SNAPSHOTS))
def test_pdf_text_snapshot(tmp_path, table_style, phraseology):
    snapshot = SNAPSHOTS[(table_style, phraseology)]
    text = _render_text(tmp_path, table_style, phraseology)
    if not snapshot.exists():
        snapshot.write_text(text, encoding="utf-8")
        pytest.skip("snapshot created; re-run to compare")
    expected = snapshot.read_text(encoding="utf-8")
    assert text == expected
