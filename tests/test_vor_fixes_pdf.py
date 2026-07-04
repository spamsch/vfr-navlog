"""End-to-end check that computed fixes reach the PDF: the stacked nav-table
cell (two fixes, one fix, overhead), the manual-override precedence, and the
Navaid-Referenz (Morse) block on the destination page."""
import pypdf

import vfr_navlog.pdf
from vfr_navlog.legs import apply_hemispheric_rule, compute_legs
from vfr_navlog.model import AirportInfo, Plan, RadialFix, RenderContext, Waypoint


def _render(plan, navaids, dest_info, tmp_path) -> str:
    aircraft = {
        "type": "C172S", "registration": "D-EIYD",
        "performance": {"tas_cruise": 93, "fuel_burn_cruise_lph": 33.3,
                        "fuel_burn_climb_lph": 45.0, "fuel_burn_taxi_lph": 10.0},
        "fuel": {"capacity_usable_l": 201, "reserve_minutes": 30, "taxi_minutes": 12,
                 "approach_minutes": 10, "alternate_minutes": 0},
    }
    wind, magvar = (270.0, 10.0), 4.0
    legs = compute_legs(plan, 93, wind, magvar, 33.3)
    apply_hemispheric_rule(plan, legs)
    ctx = RenderContext(
        plan=plan, aircraft=aircraft, legs=legs, wind=wind, magvar=magvar,
        vatsim=None, dest_info=dest_info, weather=None, field_wx={},
        fir_icaos=[], source_note="TEST", call_tower_nm=0.0,
        with_dfs_charts=False, navaids=navaids,
    )
    out = tmp_path / "navlog.pdf"
    vfr_navlog.pdf.render(ctx, out)
    reader = pypdf.PdfReader(str(out))
    return "\n=== PAGE ===\n".join(p.extract_text() for p in reader.pages)


def _plan():
    dep = Waypoint(name="Dep", ident="EDDV", type="AIRPORT", lat=52.46, lon=9.68)
    mid = Waypoint(name="Mid", ident="NIE", type="WAYPOINT", lat=52.64, lon=9.21)
    dst = Waypoint(name="Dst", ident="EDLI", type="AIRPORT", lat=51.96, lon=8.54)
    return Plan(waypoints=[dep, mid, dst], cruise_alt_ft=3500,
                flightplan_type="VFR", cycle="2607", created="")


def test_two_fixes_stacked_and_manual_override(tmp_path):
    plan = _plan()
    # Interior waypoint: two computed fixes.
    plan.waypoints[1].fixes = [
        RadialFix("HLZ", "Hehlingen", "116.30", 245, 34.0, False),
        RadialFix("DLE", "Diepholz", "115.20", 10, 22.0, True),
    ]
    # Destination: a computed fix that a manual entry must override.
    plan.waypoints[2].fixes = [RadialFix("BOT", "Bottrop", "113.90", 88, 12.0, False)]
    plan.waypoints[2].vor_info = "MANUAL 233 FROM"

    navaids = [RadialFix("HLZ", "Hehlingen", "116.30", 245, 34.0, False),
               RadialFix("DLE", "Diepholz", "115.20", 10, 22.0, True)]
    dest_info = AirportInfo(icao="EDLI", name="Bielefeld")

    text = _render(plan, navaids, dest_info, tmp_path)

    # Both stacked radial lines are present (DME station shows its distance).
    assert "HLZ 116.30 R245" in text
    assert "DLE 115.20 R010 22nm" in text
    # Manual override wins on the destination row; the computed BOT fix is gone.
    assert "MANUAL 233 FROM" in text
    assert "R088" not in text
    # Morse block on the destination page.
    assert "Navaid-Referenz" in text
    assert ".... .-.. --.." in text  # HLZ in Morse


def test_overhead_cell(tmp_path):
    plan = _plan()
    plan.waypoints[1].fixes = [RadialFix("HLZ", "Hehlingen", "116.30", 0, 1.2, False, overhead=True)]
    text = _render(plan, [], None, tmp_path)
    assert "overhead" in text
    assert "HLZ 116.30" in text
