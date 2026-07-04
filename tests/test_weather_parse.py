from navlog import parse_atis, parse_metar


def test_parse_metar_basic():
    raw = "EDDV 041020Z 27012G22KT 9999 FEW030 BKN045 14/09 Q1018 NOSIG"
    p = parse_metar(raw)
    assert p.wind_dir == 270
    assert p.wind_kt == 12
    assert p.wind_gust_kt == 22
    assert p.wind_vrb is False
    assert p.vis_m == 9999
    assert p.ceiling_ft == 4500
    assert "FEW030" in p.clouds
    assert "BKN045" in p.clouds
    assert p.temp_c == 14
    assert p.dewpoint_c == 9
    assert p.qnh_hpa == 1018
    assert p.vfr_status() == "VFR"


def test_parse_metar_cavok_and_vrb():
    p = parse_metar("EDLI 041020Z VRB03KT CAVOK 12/M01 Q1025")
    assert p.wind_vrb is True
    assert p.wind_kt == 3
    assert p.cavok is True
    assert p.vis_m == 9999
    assert p.temp_c == 12
    assert p.dewpoint_c == -1
    assert p.qnh_hpa == 1025
    assert p.vfr_status() == "VFR"


def test_parse_metar_ifr_low_ceiling():
    p = parse_metar("EDDV 041020Z 18008KT 2000 BR OVC004 05/05 Q0998")
    assert p.vis_m == 2000
    assert p.ceiling_ft == 400
    assert "BR" in p.phenomena
    assert p.vfr_status() == "IFR"


def test_parse_metar_altimeter_inhg():
    p = parse_metar("KJFK 041020Z 27010KT 10SM CLR 15/05 A2992")
    assert p.qnh_hpa == round(2992 * 0.338639)


def test_parse_atis_embedded_metar():
    lines = ["EDDV ATIS INFO C 1020Z", "27012KT 9999 FEW030 14/09 Q1018 RWY 27 IN USE"]
    p = parse_atis(lines)
    assert p.wind_dir == 270
    assert p.wind_kt == 12
    assert p.qnh_hpa == 1018
    assert p.temp_c == 14
    assert p.dewpoint_c == 9


def test_parse_atis_verbose():
    lines = ["Hannover Information Charlie", "WIND 250 DEGREES 8 KNOTS", "QNH 1018", "TEMPERATURE 14 DEWPOINT 09"]
    p = parse_atis(lines)
    assert p.wind_dir == 250
    assert p.wind_kt == 8
    assert p.qnh_hpa == 1018
    assert p.temp_c == 14
    assert p.dewpoint_c == 9
