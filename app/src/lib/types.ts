// Shapes returned by the Python bridge (vfr_navlog/api.py) and the saved plan file.

import type { Vor } from "./vor";

export interface Waypoint {
  ident: string;
  name: string;
  type: string;
  lat: number;
  lon: number;
  freq: string | null;
  fixes: string[]; // VOR cross-checks, e.g. "HMM 115.65 R172 12nm"
}

export interface Leg {
  from: string;
  to: string;
  tc: number;
  mh: number;
  wca: number;
  distance_nm: number;
  gs_kt: number;
  ete_min: number;
  fuel_l: number;
  alt_ft: number;
}

export interface Ils {
  runway: string;
  ident: string;
  freq_mhz: number;
}

export interface Resolved {
  waypoints: Waypoint[];
  legs: Leg[];
  call_leg_idx: number | null;
  dest_ils: Ils[];
  dep_freqs: Record<string, string>;
  dest_freqs: Record<string, string>;
  aircraft: { type: string; registration: string; tas_kt: number };
  wind: [number, number];
  magvar: number;
  ofm_cycle: string;
  vors: Vor[]; // VOR stations near the route
  dest_info: { name: string; elevation_ft: number; transition_alt: string; runways: string[] } | null;
  firs: string[]; // FIRs the route crosses (for the en-route radar frequency)
}

/** Airport chart pages (DFS AIP) from the bridge's "charts" command, cached on disk. */
export interface ChartPage {
  title: string;
  file: string; // file name in ~/.cache/vfr-navlog/charts/<ICAO>/
  width: number;
  height: number;
}

export interface AirportCharts {
  icao: string;
  name?: string;
  pages: ChartPage[];
  error?: string;
}

/** Live airfield data from the bridge's "field" command. */
export interface FieldLive {
  icao: string;
  fetched_at: string;
  vatsim_ok: boolean;
  freqs: Record<string, string>; // online VATSIM stations by role
  atis: string[];
  radar: { name: string; freq: string } | null;
  metar: string | null;
  taf: string | null;
  vfr_status: string | null;
  qnh_hpa: number | null;
}

export interface AircraftInfo {
  file: string;
  type: string;
  registration: string;
  tas_kt: number | null;
  burn_lph: number | null;
}

/** A map pin, attached to the leg it lies along (leg k runs waypoint k → k+1). */
export interface Landmark {
  id: string;
  lat: number;
  lon: number;
  label: string;
  leg: number;
  along: number; // NM from the leg start
  cross: number; // NM off track, + = right of track
}

export interface PdfOptions {
  vatsim: boolean;
  phraseology: boolean;
  wpMaps: boolean;
}

export interface PlanDoc {
  version: 1;
  route: string;
  aircraft: string;
  cruiseAlt: string;
  wind: string;
  magvar: string;
  notes: string[]; // per waypoint, notes for the leg into it
  landmarks: Landmark[];
  resolved: Resolved | null;
  ato: (number | null)[]; // actual time over each waypoint (epoch ms), set in flight
  legAlts: (number | null)[]; // per-leg altitude override; null = cruise altitude
  legVor: (string | null)[]; // per-leg NAV1 VOR ident; null = suggest the best, "" = none
  legVor2: (string | null)[]; // per-leg NAV2 VOR ident; null = suggest a second station, "" = none
  hemispheric: boolean; // raise un-edited legs to the semicircular rule automatically
  pdf: PdfOptions;
}
