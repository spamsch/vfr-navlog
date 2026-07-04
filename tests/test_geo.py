import math

import pytest

from vfr_navlog.geo import apply_wind, great_circle


def test_great_circle_due_east():
    tc, dist = great_circle(52.0, 9.0, 52.0, 10.0)
    # Eastbound along a parallel: initial true course just under 090 in the N hemisphere.
    assert tc == pytest.approx(89.6, abs=0.2)
    # One degree of longitude at 52 N is ~36.9 nm.
    assert dist == pytest.approx(36.9, abs=0.3)


def test_great_circle_due_north():
    tc, dist = great_circle(50.0, 8.0, 51.0, 8.0)
    assert tc == pytest.approx(0.0, abs=1e-6)
    # One degree of latitude ~60 nm.
    assert dist == pytest.approx(60.0, abs=0.2)


def test_great_circle_zero_distance():
    tc, dist = great_circle(52.0, 9.0, 52.0, 9.0)
    assert dist == pytest.approx(0.0, abs=1e-9)


def test_apply_wind_no_wind_is_no_correction():
    wca, th, gs = apply_wind(90.0, 100.0, 270.0, 0.0)
    assert wca == pytest.approx(0.0)
    assert th == pytest.approx(90.0)
    assert gs == pytest.approx(100.0)


def test_apply_wind_direct_headwind():
    # Wind from 090 blowing onto a 090 course: pure headwind, no drift.
    wca, th, gs = apply_wind(90.0, 100.0, 90.0, 20.0)
    assert wca == pytest.approx(0.0, abs=1e-9)
    assert th == pytest.approx(90.0, abs=1e-9)
    assert gs == pytest.approx(80.0, abs=1e-6)


def test_apply_wind_direct_tailwind():
    wca, th, gs = apply_wind(90.0, 100.0, 270.0, 20.0)
    assert wca == pytest.approx(0.0, abs=1e-9)
    assert gs == pytest.approx(120.0, abs=1e-6)


def test_apply_wind_crosswind_drifts_into_wind():
    # Wind from the left (000) on an eastbound course pushes the heading left (negative WCA).
    wca, th, gs = apply_wind(90.0, 100.0, 0.0, 30.0)
    assert wca < 0
    assert th == pytest.approx(90.0 + wca)
    # sin(WCA) = (30/100) * sin(0 - 90) = -0.3
    assert wca == pytest.approx(math.degrees(math.asin(-0.3)), abs=1e-6)


def test_apply_wind_zero_tas():
    wca, th, gs = apply_wind(90.0, 0.0, 270.0, 20.0)
    assert (wca, th, gs) == (0.0, 90.0, 0.0)
