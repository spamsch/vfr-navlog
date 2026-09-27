// Flat-earth navigation helpers. Legs are short (< ~100 NM), so a local
// east/north plane around the leg start is accurate to well under 0.1 NM and
// matches the straight lines Leaflet draws on the Mercator map.

export interface LatLon {
  lat: number;
  lon: number;
}

const RAD = Math.PI / 180;

/** Local plane coordinates of p relative to origin, in NM (x east, y north). */
export function toLocal(origin: LatLon, p: LatLon): { x: number; y: number } {
  return {
    x: (p.lon - origin.lon) * 60 * Math.cos(origin.lat * RAD),
    y: (p.lat - origin.lat) * 60,
  };
}

export function fromLocal(origin: LatLon, x: number, y: number): LatLon {
  return {
    lat: origin.lat + y / 60,
    lon: origin.lon + x / (60 * Math.cos(origin.lat * RAD)),
  };
}

/** Point dist NM from p on a true bearing. */
export function destination(p: LatLon, bearingDeg: number, distNm: number): LatLon {
  const b = bearingDeg * RAD;
  return fromLocal(p, Math.sin(b) * distNm, Math.cos(b) * distNm);
}

/** True bearing a → b in degrees, on the local plane. */
export function bearing(a: LatLon, b: LatLon): number {
  const { x, y } = toLocal(a, b);
  return (Math.atan2(x, y) / RAD + 360) % 360;
}

export function distanceNm(a: LatLon, b: LatLon): number {
  const { x, y } = toLocal(a, b);
  return Math.hypot(x, y);
}

/** Where p lies relative to leg a → b: NM along track, NM off track (+ right), leg length. */
export function project(a: LatLon, b: LatLon, p: LatLon): { along: number; cross: number; legNm: number } {
  const v = toLocal(a, b);
  const w = toLocal(a, p);
  const legNm = Math.hypot(v.x, v.y) || 1e-9;
  const ux = v.x / legNm;
  const uy = v.y / legNm;
  return { along: w.x * ux + w.y * uy, cross: w.x * uy - w.y * ux, legNm };
}

/** The point `along` NM from a towards b (may run past b). */
export function pointAlong(a: LatLon, b: LatLon, along: number): LatLon {
  const v = toLocal(a, b);
  const len = Math.hypot(v.x, v.y) || 1e-9;
  return fromLocal(a, (v.x / len) * along, (v.y / len) * along);
}

/** A short segment through p perpendicular to leg a → b, halfNm to each side. */
export function perpendicular(a: LatLon, b: LatLon, p: LatLon, halfNm: number): [LatLon, LatLon] {
  const brg = bearing(a, b);
  return [destination(p, brg - 90, halfNm), destination(p, brg + 90, halfNm)];
}

/** Slippy-map tile containing a point at zoom z (OFM uses the standard grid). */
export function tileXY(p: LatLon, z: number): { x: number; y: number } {
  const n = 2 ** z;
  const x = Math.floor(((p.lon + 180) / 360) * n);
  const latR = p.lat * RAD;
  const y = Math.floor(((1 - Math.log(Math.tan(latR) + 1 / Math.cos(latR)) / Math.PI) / 2) * n);
  return { x, y };
}

/** mm:ss for a duration in seconds; negative durations get a leading minus. */
export function mmss(seconds: number): string {
  const sign = seconds < 0 ? "-" : "";
  const s = Math.round(Math.abs(seconds));
  return `${sign}${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

/** hh:mm:ss UTC of an epoch-ms timestamp. */
export function utc(ms: number, withSeconds = true): string {
  const d = new Date(ms);
  const hh = String(d.getUTCHours()).padStart(2, "0");
  const mm = String(d.getUTCMinutes()).padStart(2, "0");
  const ss = String(d.getUTCSeconds()).padStart(2, "0");
  return withSeconds ? `${hh}:${mm}:${ss}Z` : `${hh}:${mm}Z`;
}
