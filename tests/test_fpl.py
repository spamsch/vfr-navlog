
from vfr_navlog.exports import format_icao_fpl
from vfr_navlog.legs import compute_legs
from vfr_navlog.lnmpln import parse_lnmpln
from vfr_navlog.pdf.base import fmt_int, hms

AIRCRAFT = {
    "type": "C172S",
    "icao_type": "C172",
    "registration": "D-EIYD",
    "performance": {"tas_cruise": 93, "fuel_burn_cruise_lph": 33.3},
    "fuel": {"capacity_usable_l": 201},
}


def test_hms_formatting():
    assert hms(0) == ""
    assert hms(-5) == ""
    assert hms(1) == "1:00"
    assert hms(1.5) == "1:30"
    assert hms(65) == "1:05:00"


def test_fmt_int():
    assert fmt_int(3.4) == "3"
    assert fmt_int(3.6) == "4"
    assert fmt_int(5, width=3) == "  5"


def test_format_icao_fpl(sample_plan_path):
    plan = parse_lnmpln(sample_plan_path)
    legs = compute_legs(plan, tas=93.0, wind=(0.0, 0.0), magvar=4.0, burn_lph=33.3)
    fpl = format_icao_fpl(
        plan, AIRCRAFT, legs,
        eobt="1030", pob=2, equipment="SDFG/C", wake="L",
        alternate="EDDW", pilot_name="Mustermann",
    )
    lines = fpl.splitlines()
    assert lines[0] == "(FPL-DEIYD-VG"
    assert lines[1] == "-C172/L-SDFG/C"
    assert lines[2].startswith("-EDDV1030")
    assert "N0093" in lines[3]
    assert "A035" in lines[3]
    assert lines[4].startswith("-EDLI")
    assert "EDDW" in lines[4]
    assert "P/002" in lines[6]
    assert "C/MUSTERMANN" in lines[6]
    assert fpl.endswith(")")
