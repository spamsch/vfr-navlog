"""Waypoint map pages reach the PDF: header, VOR band, side-by-side maps, both
attributions, and the degrade cases (chart-only, skip).

Uses stub annotated images (no network). Flag-off behaviour — an empty wp_maps
producing no extra pages — is covered by the byte-identical snapshot test.
"""
from datetime import datetime

import pypdf
from PIL import Image

import vfr_navlog.pdf
from vfr_navlog.baselayers import WaypointLayers
from vfr_navlog.legs import apply_hemispheric_rule, compute_legs
from vfr_navlog.model import Plan, RadialFix, RenderContext, Waypoint

OFM_ATTR = "© open flightmaps — OFMA General Users' License — AIRAC 2606"
PHOTO_ATTR = ("Sentinel-2 cloudless - https://s2maps.eu by EOX IT Services GmbH "
              "(Contains modified Copernicus Sentinel data 2024)")


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
    return pypdf.PdfReader(str(out))


def _img():
    return Image.new("RGB", (400, 400), (120, 170, 90))


def _both():
    return WaypointLayers(_img(), _img(), OFM_ATTR, PHOTO_ATTR)


def test_both_layers_page_has_header_band_and_two_attributions(tmp_path, monkeypatch):
    reader = _render(tmp_path, [_both(), _both(), _both()], monkeypatch)
    text = "\n".join(p.extract_text() for p in reader.pages)
    assert "WP 1/3" in text
    assert "WP 2/3" in text and "EDDV" in text
    assert "VOR fixes" in text or "VOR-Kreuzpeilung" in text
    assert "HLZ 116.30" in text
    assert "AIRAC 2606" in text and "OFMA General Users" in text
    assert "Sentinel-2 cloudless" in text  # photo attribution present too


def test_chart_only_fallback_renders_full_width(tmp_path, monkeypatch):
    # Photo missing on the middle waypoint → chart-only page, no photo caption.
    wp_maps = [_both(),
               WaypointLayers(_img(), None, OFM_ATTR, None),
               _both()]
    reader = _render(tmp_path, wp_maps, monkeypatch)
    per_page = [p.extract_text() for p in reader.pages]
    # The middle waypoint's page carries the OFM line but no Sentinel-2 line.
    wp2 = next(t for t in per_page if "WP 2/3" in t)
    assert "AIRAC 2606" in wp2
    assert "Sentinel-2 cloudless" not in wp2


def test_photo_only_page_renders(tmp_path, monkeypatch):
    wp_maps = [WaypointLayers(None, _img(), None, PHOTO_ATTR), _both(), _both()]
    reader = _render(tmp_path, wp_maps, monkeypatch)
    per_page = [p.extract_text() for p in reader.pages]
    wp1 = next(t for t in per_page if "WP 1/3" in t)
    assert "Sentinel-2 cloudless" in wp1
    assert "OFMA General Users" not in wp1


def test_none_and_empty_entries_skip_pages(tmp_path, monkeypatch):
    wp_maps = [_both(), None, WaypointLayers(None, None, None, None)]
    reader = _render(tmp_path, wp_maps, monkeypatch)
    text = "\n".join(p.extract_text() for p in reader.pages)
    assert "WP 1/3" in text
    assert "WP 2/3" not in text
    assert "WP 3/3" not in text
