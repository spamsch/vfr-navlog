// Radial tracking: can a leg be flown with the needle centred on one VOR?
//
// Set the OBS to the leg's magnetic course (measured against the station's own
// slaved variation, like every radial). If the leg's line runs through the
// station, the CDI stays centred along the whole leg; the further the line
// passes from the station, the more the needle wanders. A standard VOR CDI is
// ±10° full scale, so a leg whose needle stays within half scale (5°) all the
// way is flyable on that radial. Other legs still get the radial at each end —
// a progress check ("R210 at the start, R245 over the waypoint").

import { distanceNm, pointAlong, project, type LatLon } from "./geo";

export interface Vor {
  ident: string;
  name: string;
  freq: string;
  lat: number;
  lon: number;
  var: number; // slaved variation, East positive
  dme: boolean;
  range_nm: number;
  route_dist_nm: number;
}

export type Flag = "FROM" | "TO" | "TO→FROM";

export interface LegRadial {
  vor: Vor;
  obs: number; // OBS setting = leg course vs. the station's variation
  flag: Flag;
  radialLabel: string; // "outbound R172", "inbound R352", "inbound R352 → outbound R172"
  rStart: number; // radial FROM the station over the leg start / end
  rEnd: number;
  dStart: number; // NM station → leg start / end (DME)
  dEnd: number;
  maxDev: number; // largest CDI deflection along the leg, degrees
  inRange: boolean;
  trackable: boolean; // in range and needle within half scale the whole leg
}

/** Half-scale deflection on a standard VOR CDI (full scale ±10°). */
export const TRACKABLE_DEV = 5;
/** Close to the station the needle swings wildly; ignore it inside this radius. */
const STATION_CONE_NM = 2;

const RAD = Math.PI / 180;
const norm = (d: number) => ((d % 360) + 360) % 360;
const pad3 = (d: number) => String(Math.round(norm(d)) % 360).padStart(3, "0");

/** Initial great-circle bearing a → b, degrees true. */
export function gcBearing(a: LatLon, b: LatLon): number {
  const f1 = a.lat * RAD;
  const f2 = b.lat * RAD;
  const dl = (b.lon - a.lon) * RAD;
  const y = Math.sin(dl) * Math.cos(f2);
  const x = Math.cos(f1) * Math.sin(f2) - Math.sin(f1) * Math.cos(f2) * Math.cos(dl);
  return norm(Math.atan2(y, x) / RAD);
}

/** Signed smallest difference a − b in degrees, −180…180. */
function angDiff(a: number, b: number): number {
  return ((a - b + 540) % 360) - 180;
}

/** Magnetic radial FROM the station to p (what centres the needle with a FROM flag). */
export function radialAt(v: Vor, p: LatLon): number {
  return Math.round(norm(gcBearing(v, p) - v.var)) % 360;
}

export function formatRadial(r: number): string {
  return `R${pad3(r)}`;
}

export function analyseLeg(v: Vor, a: LatLon, b: LatLon): LegRadial {
  const courseTrue = gcBearing(a, b);
  const obs = Math.round(norm(courseTrue - v.var)) % 360;
  const { along: t, legNm } = project(a, b, v); // station's position along the leg line
  const flag: Flag = t <= 0 ? "FROM" : t >= legNm ? "TO" : "TO→FROM";

  // Needle deflection with OBS = course, sampled along the leg.
  let maxDev = 0;
  const steps = Math.max(4, Math.ceil(legNm / 0.5));
  for (let i = 0; i <= steps; i++) {
    const s = (legNm * i) / steps;
    const p = pointAlong(a, b, s);
    if (distanceNm(v, p) < STATION_CONE_NM) continue;
    // Station behind the aircraft → FROM: compare the radial; ahead → TO: compare the bearing to it.
    const brg = s >= t ? gcBearing(v, p) : gcBearing(p, v);
    maxDev = Math.max(maxDev, Math.abs(angDiff(brg, courseTrue)));
  }

  const dStart = distanceNm(v, a);
  const dEnd = distanceNm(v, b);
  const inRange = Math.max(dStart, dEnd) <= v.range_nm;
  const out = formatRadial(obs);
  const inb = formatRadial(obs + 180);
  return {
    vor: v,
    obs,
    flag,
    radialLabel: flag === "FROM" ? `outbound ${out}` : flag === "TO" ? `inbound ${inb}` : `inbound ${inb} → outbound ${out}`,
    rStart: radialAt(v, a),
    rEnd: radialAt(v, b),
    dStart,
    dEnd,
    maxDev,
    inRange,
    trackable: inRange && maxDev <= TRACKABLE_DEV,
  };
}

/** Every in-range station for a leg: trackable ones first, then by needle deflection. */
export function candidatesForLeg(vors: Vor[], a: LatLon, b: LatLon): LegRadial[] {
  return vors
    .map((v) => analyseLeg(v, a, b))
    .filter((c) => c.inRange)
    .sort((x, y) => Number(y.trackable) - Number(x.trackable) || x.maxDev - y.maxDev);
}

/** One line for cards and PDF notes: "HMM 115.65 · OBS 172 FROM (outbound R172) · needle ≤ 2°". */
export function navLine(c: LegRadial): string {
  const base = `${c.vor.ident} ${c.vor.freq} · OBS ${pad3(c.obs)} ${c.flag}`;
  if (c.trackable) return `${base} (${c.radialLabel}) · needle ≤ ${Math.max(1, Math.round(c.maxDev))}°`;
  return `${c.vor.ident} ${c.vor.freq} · ${formatRadial(c.rStart)} → ${formatRadial(c.rEnd)} (progress check)`;
}
