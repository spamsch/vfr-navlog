from pathlib import Path

import pypdf

import vfr_navlog.pdf
from vfr_navlog.legs import compute_legs
from vfr_navlog.lnmpln import parse_lnmpln
from vfr_navlog.model import RenderContext

FIXTURES = Path(__file__).parent / "fixtures"
AIRCRAFT = {
    "type": "C172S", "registration": "D-EIYD",
    "performance": {"tas_cruise": 93, "fuel_burn_cruise_lph": 33.3},
    "fuel": {"capacity_usable_l": 201},
}


def _render(tmp_path: Path, notes: dict[int, str], table_style: str = "notes") -> str:
    plan = parse_lnmpln(FIXTURES / "sample.lnmpln")
    for i, text in notes.items():
        plan.waypoints[i].notes = text
    legs = compute_legs(plan, 93, (270.0, 10.0), 4.0, 33.3)
    ctx = RenderContext(
        plan=plan, aircraft=AIRCRAFT, legs=legs, wind=(270.0, 10.0), magvar=4.0,
        vatsim=None, dest_info=None, weather=None, field_wx={}, fir_icaos=[],
        source_note="TEST", call_tower_nm=10.0, with_dfs_charts=False,
        table_style=table_style,
    )
    out = tmp_path / "n.pdf"
    vfr_navlog.pdf.render(ctx, out)
    return pypdf.PdfReader(str(out)).pages[0].extract_text()


def test_notes_printed_and_wrapped(tmp_path):
    long_note = " ".join(["Autobahn A2 links, Kanal rechts parallel"] * 6)
    text = _render(tmp_path, {1: "Kirchturm rechts\nSee 2 NM links", 2: long_note})
    assert "Kirchturm rechts" in text
    assert "See 2 NM links" in text
    # Long notes wrap onto several lines instead of running past the cell.
    assert text.count("Autobahn A2 links") == 6


def test_classic_table_ignores_notes(tmp_path):
    text = _render(tmp_path, {1: "Kirchturm rechts"}, table_style="classic")
    assert "Kirchturm" not in text
