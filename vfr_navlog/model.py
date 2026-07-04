"""Plain data types shared across the package."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class VorStation:
    """A VOR from earth_nav.dat, with the two fields the old parser dropped."""
    ident: str
    name: str
    freq: str            # "116.30", MHz as printed on the chart
    lat: float
    lon: float
    range_nm: float      # published reception range (earth_nav.dat parts[5])
    slaved_var: float    # slaved variation, deg, East positive (parts[6])
    has_dme: bool = False


@dataclass
class RadialFix:
    """One VOR cross-check line for a waypoint: 'HLZ 116.30 R245'."""
    vor_ident: str       # "HLZ"
    vor_name: str        # "Hehlingen" — for the navaid reference table
    freq: str            # "116.30"
    radial: int          # 0–359, magnetic FROM station, slaved-variation corrected
    dist_nm: float       # station → waypoint, for the DME cross-check
    has_dme: bool
    overhead: bool = False  # waypoint is (near) station passage


@dataclass
class Waypoint:
    name: str
    ident: str
    type: str
    lat: float
    lon: float
    alt_ft: float | None = None
    region: str | None = None
    freq: str | None = None
    vor_info: str | None = None  # free-text VOR/navaid reference, e.g. "233 FROM"
    fixes: list[RadialFix] = field(default_factory=list)  # 0–2 computed cross-checks


@dataclass
class Plan:
    waypoints: list[Waypoint]
    cruise_alt_ft: float
    flightplan_type: str
    cycle: str
    created: str
    # Optional step-altitude profile: list of (waypoint_ident, alt_ft) pairs in route order.
    # Each entry means "from this waypoint onwards, cruise at alt_ft."
    alt_profile: list[tuple[str, float]] = field(default_factory=list)


@dataclass
class VatsimSnapshot:
    fetched_at: str
    update_time: str
    frequencies: dict[str, dict[str, str]]  # icao -> {role -> "118.300"}
    atis_text: dict[str, list[str]]         # icao -> raw ATIS lines

    def empty(self) -> bool:
        return not any(self.frequencies.values())


@dataclass
class Runway:
    ident_a: str
    ident_b: str
    surface: str
    width_m: float
    length_m: float


@dataclass
class IlsLoc:
    runway: str
    ident: str
    freq_mhz: float
    type_desc: str


@dataclass
class AirportInfo:
    icao: str
    name: str
    elevation_ft: float = 0.0
    city: str = ""
    transition_alt: str = ""
    transition_level: str = ""
    iata: str = ""
    lat: float | None = None   # averaged runway-endpoint position
    lon: float | None = None
    runways: list[Runway] = None  # type: ignore[assignment]
    ils_locs: list[IlsLoc] = None  # type: ignore[assignment]

    def __post_init__(self):
        if self.runways is None:
            self.runways = []
        if self.ils_locs is None:
            self.ils_locs = []


@dataclass
class Leg:
    from_wp: Waypoint
    to_wp: Waypoint
    tc: float
    wca: float
    th: float
    mh: float
    distance_nm: float
    gs_kt: float
    ete_min: float
    fuel_l: float


@dataclass
class ParsedMetar:
    raw: str
    wind_dir: int | None = None
    wind_kt: int | None = None
    wind_gust_kt: int | None = None
    wind_vrb: bool = False
    vis_m: int | None = None
    cavok: bool = False
    ceiling_ft: int | None = None   # lowest BKN or OVC layer
    clouds: list = None             # type: ignore[assignment]
    temp_c: float | None = None
    dewpoint_c: float | None = None
    qnh_hpa: int | None = None
    phenomena: list = None          # type: ignore[assignment]

    def __post_init__(self):
        if self.clouds is None:
            self.clouds = []
        if self.phenomena is None:
            self.phenomena = []

    def vfr_status(self) -> str:
        if self.cavok:
            return "VFR"
        vis  = self.vis_m      if self.vis_m      is not None else 9999
        ceil = self.ceiling_ft if self.ceiling_ft is not None else 99999
        if ceil < 1500 or vis < 3000:
            return "IFR"
        if ceil < 3000 or vis < 5000:
            return "MVFR"
        return "VFR"


@dataclass
class WeatherBriefing:
    dep_icao: str
    dest_icao: str
    dep_metar_raw: str | None
    dest_metar_raw: str | None
    dep_taf_raw: str | None
    dest_taf_raw: str | None
    dep_metar: ParsedMetar | None
    dest_metar: ParsedMetar | None
    fetched_at: str


@dataclass
class FieldWx:
    icao: str
    source: str               # "VATSIM ATIS" oder "METAR (real)"
    parsed: ParsedMetar       # Wind / Temp / QNH
    atis_code: str | None = None
    rwy: str | None = None
    time_z: str | None = None


@dataclass
class RenderContext:
    """Everything render() needs, in one object instead of 14 positional params."""
    plan: Plan
    aircraft: dict
    legs: list[Leg]
    wind: tuple[float, float]
    magvar: float
    vatsim: VatsimSnapshot | None
    dest_info: AirportInfo | None
    weather: WeatherBriefing | None
    field_wx: dict[str, FieldWx]
    fir_icaos: list[str]
    source_note: str
    call_tower_nm: float
    with_dfs_charts: bool
    navaids: list = field(default_factory=list)  # distinct RadialFix per station, for the reference block
    wp_maps: list = field(default_factory=list)  # per-waypoint WaypointMap | None, in route order


@dataclass
class RunConfig:
    """A fully-resolved run request. Both cli.main() and tui._tui() produce one;
    cli.run() consumes it. Replaces the forged argparse.Namespace.
    """
    navigraph: bool
    plan_path: Path | None            # None → Navigraph source
    aircraft_path: Path
    wind: tuple[float, float]         # parsed (direction_deg, speed_kt)
    wind_was_default: bool            # wind arg was the "0/0" default → allow METAR substitution
    magvar: float
    registration: str | None
    cruise_alt_ft: float | None
    alt_profile: list[tuple[str, float]]
    output: Path | None
    xplane_path: Path | None
    vatsim: bool
    vor_info: bool
    with_dfs_charts: bool
    call_tower_nm: float
    fms: bool
    fpl_fields: dict | None
    vor_fixes: bool = False  # compute automatic VOR radial cross-checks per waypoint
