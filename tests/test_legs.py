import pytest

from vfr_navlog.legs import apply_hemispheric_rule, compute_legs, hemispheric_alt
from vfr_navlog.lnmpln import parse_lnmpln


def _plan(sample_plan_path):
    return parse_lnmpln(sample_plan_path)


def test_compute_legs_three_waypoints(sample_plan_path):
    plan = _plan(sample_plan_path)
    legs = compute_legs(plan, tas=93.0, wind=(0.0, 0.0), magvar=4.0, burn_lph=33.3)
    assert len(legs) == 2
    # Calm wind: WCA is zero, TH equals TC, GS equals TAS.
    for leg in legs:
        assert leg.wca == pytest.approx(0.0, abs=1e-9)
        assert leg.gs_kt == pytest.approx(93.0, abs=1e-6)
        # East variation of +4 subtracts from TH to give MH.
        assert leg.mh == pytest.approx((leg.th - 4.0) % 360, abs=1e-9)
        assert leg.distance_nm > 0
        assert leg.ete_min == pytest.approx((leg.distance_nm / 93.0) * 60, abs=1e-6)
        assert leg.fuel_l == pytest.approx((leg.ete_min / 60) * 33.3, abs=1e-6)


def test_hemispheric_alt_below_floor_unchanged():
    assert hemispheric_alt(1000.0, 90.0) == 1000.0


def test_hemispheric_alt_eastbound():
    # MH < 180 -> odd thousands + 500: 1500, 3500, 5500 ...
    assert hemispheric_alt(1500.0, 90.0) == 1500.0
    assert hemispheric_alt(3000.0, 90.0) == 3500.0
    assert hemispheric_alt(3500.0, 90.0) == 3500.0
    assert hemispheric_alt(4000.0, 90.0) == 5500.0


def test_hemispheric_alt_westbound():
    # MH >= 180 -> even thousands + 500: 2500, 4500, 6500 ...
    assert hemispheric_alt(2500.0, 270.0) == 2500.0
    assert hemispheric_alt(3000.0, 270.0) == 4500.0
    assert hemispheric_alt(4500.0, 270.0) == 4500.0


def test_apply_hemispheric_rule_adjusts_plan(sample_plan_path):
    plan = _plan(sample_plan_path)
    legs = compute_legs(plan, tas=93.0, wind=(0.0, 0.0), magvar=4.0, burn_lph=33.3)
    apply_hemispheric_rule(plan, legs)
    # After correction, the first leg's cruise altitude must be a compliant level.
    first_mh = legs[0].mh
    assert plan.cruise_alt_ft == hemispheric_alt(3500.0, first_mh)
