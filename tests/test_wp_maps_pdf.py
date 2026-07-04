"""Waypoint map pages reach the PDF: header strip, VOR fix block, attribution.

Uses a stub excerpt image (no network). Flag-off behaviour — an empty wp_maps
producing no extra pages — is covered by the byte-identical snapshot test.
"""
from datetime import datetime

import pypdf
from PIL import Image

import vfr_navlog.pdf
from vfr_navlog.legs import apply_hemispheric_rule, compute_legs
from vfr_navlog.model import Plan, RadialFix, RenderContext, Waypoint
from vfr_navlog.ofm import WaypointMap


class _FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 7, 4, 10, 0, 0, tzinfo=tz)


def _plan():
    dep = Waypoint(name="Hannover", ident="EDDV", type="AIRPORT", lat=52.46, lon=9.68, alt_ft=183)
    mid = Waypoint(name="Nienburg", ident="NIE", type="WAYPOINT", lat=52.64, lon=9.21, alt_ft=3500)
    dst = Waypoint(name="Bielefeld", ident="EDLI", type="AIRPORT", lat=51.96, lon=8.54, alt_ft=433)
    mid.fixes = [RadialFix("HLZ", "Hehlingen", "116.30", 245, 34.0, True)]
    return Plan(waypoints=[dep, mid, dst], cruise_alt_ft=3500,
                flightplan_type="VFR", cycle="2606", created="")


def _render(tmp_path, wp_maps, monkeypatch):
    monkeypatch.setattr(vfr_navlog.pdf, "datetime", _FrozenDateTime)
    plan = _plan()
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
        vatsim=None, dest_info=None, weather=None, field_wx={},
        fir_icaos=[], source_note="TEST", call_tower_nm=0.0,
        with_dfs_charts=False, wp_maps=wp_maps,
    )
    out = tmp_path / "navlog.pdf"
    vfr_navlog.pdf.render(ctx, out)
    reader = pypdf.PdfReader(str(out))
    return reader


def _img():
    return Image.new("RGB", (400, 400), (120, 170, 90))


def test_wp_pages_present_with_header_fix_and_attribution(tmp_path, monkeypatch):
    wp_maps = [
        WaypointMap(_img(), "2606"),
        WaypointMap(_img(), "2606"),
        WaypointMap(_img(), "2606"),
    ]
    reader = _render(tmp_path, wp_maps, monkeypatch)
    text = "\n".join(p.extract_text() for p in reader.pages)
    # One page per waypoint: nav table (1) + 3 waypoint pages + phraseology.
    assert "WP 1/3" in text
    assert "WP 2/3" in text and "EDDV" in text
    assert "VOR fixes" in text or "VOR-Kreuzpeilung" in text
    assert "HLZ 116.30  R245" in text
    assert "AIRAC 2606" in text
    assert "OFMA General Users" in text


def test_none_entries_skip_pages(tmp_path, monkeypatch):
    # Middle waypoint has no coverage -> its page is skipped, others render.
    wp_maps = [WaypointMap(_img(), "2606"), None, WaypointMap(_img(), "2606")]
    reader = _render(tmp_path, wp_maps, monkeypatch)
    text = "\n".join(p.extract_text() for p in reader.pages)
    assert "WP 1/3" in text
    assert "WP 3/3" in text
    assert "WP 2/3" not in text
