import { invoke } from "@tauri-apps/api/core";
import { open, save } from "@tauri-apps/plugin-dialog";

import { distanceNm, perpendicular, pointAlong, project, tileXY, type LatLon } from "./geo";
import type { AircraftInfo, AirportCharts, FieldLive, Landmark, PlanDoc, Resolved } from "./types";
import { analyseLeg, candidatesForLeg, navCourse, navLine, type LegRadial } from "./vor";

const AUTOSAVE_KEY = "vfr-navlog-autosave";

const WINDOWS = navigator.userAgent.includes("Windows");
/** Tiles are served by the Rust `ofm` protocol (disk cache, network on a miss). */
export const TILE_BASE = WINDOWS ? "http://ofm.localhost" : "ofm://localhost";
/** Airport chart pages, served by the Rust `chart` protocol from the chart cache. */
export const CHART_BASE = WINDOWS ? "http://chart.localhost" : "chart://localhost";

export function emptyDoc(): PlanDoc {
  return {
    version: 1,
    route: "",
    aircraft: "aircraft_sr22.json",
    cruiseAlt: "",
    wind: "0/0",
    magvar: "4E",
    notes: [],
    landmarks: [],
    resolved: null,
    ato: [],
    legAlts: [],
    legVor: [],
    legVor2: [],
    hemispheric: false,
    pdf: { vatsim: true, phraseology: false, wpMaps: false },
  };
}

/**
 * Nearest VFR semicircular-rule altitude at or above alt (same rule as
 * vfr_navlog.legs.hemispheric_alt): MH 000–179 → 1500, 3500, 5500 …;
 * MH 180–359 → 2500, 4500, 6500 …. Below 1500 ft the rule does not apply.
 */
export function semicircularAlt(alt: number, mh: number): number {
  if (alt < 1500) return alt;
  const base = mh < 180 ? 1500 : 2500;
  return base + Math.max(0, Math.ceil((alt - base) / 2000)) * 2000;
}

/** Leg identity that survives route edits: "FROM>TO". */
const legKey = (r: Resolved, k: number) => `${r.waypoints[k].ident}>${r.waypoints[k + 1].ident}`;

function navlog<T>(req: Record<string, unknown>): Promise<T> {
  return invoke<T>("navlog", { req });
}

/** Attach a landmark to the leg it lies closest to, measured across track. */
export function placeLandmark(lm: Landmark, r: Resolved): Landmark {
  const wps = r.waypoints;
  let best = { leg: 0, along: 0, cross: 0, score: Infinity };
  for (let k = 0; k < wps.length - 1; k++) {
    const { along, cross, legNm } = project(wps[k], wps[k + 1], lm);
    // Off the ends of the leg, score by distance to the nearer end instead.
    const score =
      along < 0 ? distanceNm(wps[k], lm) : along > legNm ? distanceNm(wps[k + 1], lm) : Math.abs(cross);
    if (score < best.score) best = { leg: k, along, cross, score };
  }
  return { ...lm, leg: best.leg, along: best.along, cross: best.cross };
}

/** "Kirchturm (R 0.8 NM · 6.2 NM / 3 min)" — one landmark line for notes and the PDF. */
export function landmarkLine(lm: Landmark, r: Resolved): string {
  const gs = r.legs[lm.leg]?.gs_kt || 1;
  const side = Math.abs(lm.cross) < 0.2 ? "on track" : `${lm.cross > 0 ? "R" : "L"} ${Math.abs(lm.cross).toFixed(1)} NM`;
  const min = Math.round((lm.along / gs) * 60);
  return `${lm.label} (${side} · ${lm.along.toFixed(1)} NM / ${min} min)`;
}

class AppState {
  doc = $state<PlanDoc>(emptyDoc());
  filePath = $state<string | null>(null);
  savedJson = $state(JSON.stringify(emptyDoc()));
  busy = $state<string | null>(null);
  error = $state<string | null>(null);
  status = $state<string | null>(null);
  mode = $state<"plan" | "fly">("plan");
  selectedWp = $state(0);
  selectedLandmark = $state<string | null>(null);
  selectedVor = $state<string | null>(null);
  pinMode = $state(false);
  showFans = $state(true);
  showTicks = $state(true);
  /** Map background: openflightmaps chart, satellite imagery, or none. */
  mapBase = $state<"chart" | "satellite" | "none">("chart");
  aircraftList = $state<AircraftInfo[]>([]);
  now = $state(Date.now());
  /** Bumped to make the map zoom back to the whole route. */
  fitRoute = $state(0);
  field = $state<FieldLive | null>(null);
  fieldBusy = $state(false);
  fieldError = $state<string | null>(null);
  /** After the first "Send to X-Plane", plan edits are re-sent automatically. */
  xpSync = $state(false);
  /** Airport charts by ICAO; "loading" while the download runs. */
  charts = $state<Record<string, AirportCharts | "loading">>({});
  /** Open chart viewer: airport and page index, or null. */
  chartView = $state<{ icao: string; page: number } | null>(null);

  dirty = $derived(JSON.stringify(this.doc) !== this.savedJson);

  // --- Startup / persistence ------------------------------------------------

  async init() {
    try {
      const saved = localStorage.getItem(AUTOSAVE_KEY);
      if (saved) this.doc = { ...emptyDoc(), ...JSON.parse(saved) };
    } catch {
      // storage unavailable or corrupt autosave: start empty
    }
    try {
      const r = await navlog<{ aircraft: AircraftInfo[] }>({ cmd: "aircraft" });
      this.aircraftList = r.aircraft;
    } catch (e) {
      this.error = `Python bridge: ${e}`;
    }
    for (const icao of this.chartIcaos()) void this.fetchCharts(icao);
  }

  autosave() {
    try {
      localStorage.setItem(AUTOSAVE_KEY, JSON.stringify(this.doc));
    } catch {
      // ignore: autosave is a convenience
    }
  }

  newPlan() {
    this.doc = emptyDoc();
    this.filePath = null;
    this.savedJson = JSON.stringify(this.doc);
    this.selectedWp = 0;
    this.selectedLandmark = null;
  }

  async openPlan() {
    const path = await open({ filters: [{ name: "VFR plan", extensions: ["json"] }] });
    if (typeof path !== "string") return;
    try {
      const text = await invoke<string>("load_plan", { path });
      this.doc = { ...emptyDoc(), ...JSON.parse(text) };
      this.filePath = path;
      this.savedJson = JSON.stringify(this.doc);
      this.selectedWp = 0;
      this.selectedLandmark = null;
      for (const icao of this.chartIcaos()) void this.fetchCharts(icao);
    } catch (e) {
      this.error = String(e);
    }
  }

  async savePlan(saveAs = false) {
    let path = this.filePath;
    if (!path || saveAs) {
      const wps = this.doc.resolved?.waypoints;
      const name = wps ? `${wps[0].ident}-${wps[wps.length - 1].ident}.vfrplan.json` : "plan.vfrplan.json";
      const picked = await save({ defaultPath: name, filters: [{ name: "VFR plan", extensions: ["json"] }] });
      if (!picked) return;
      path = picked;
    }
    try {
      const json = JSON.stringify(this.doc);
      await invoke("save_plan", { path, content: JSON.stringify(this.doc, null, 2) });
      this.filePath = path;
      this.savedJson = json;
      this.status = `Saved ${path}`;
    } catch (e) {
      this.error = String(e);
    }
  }

  // --- Route ------------------------------------------------------------------

  async resolve() {
    const d = this.doc;
    if (!d.route.trim()) return;
    this.busy = "Resolving route…";
    this.error = null;
    try {
      const r = await navlog<Resolved>({
        cmd: "resolve",
        route: d.route,
        aircraft: d.aircraft,
        cruise_alt_ft: d.cruiseAlt ? Number(d.cruiseAlt) : null,
        wind: d.wind || "0/0",
        magvar: d.magvar || "4E",
        hemispheric: false, // leg altitudes are managed here, see legAlt()
      });
      // Per-leg settings follow their leg (same from/to) across route edits.
      if (d.resolved) {
        const old = d.resolved;
        const alts = new Map<string, number | null>();
        const vors = new Map<string, string | null>();
        const vors2 = new Map<string, string | null>();
        old.legs.forEach((_, k) => {
          alts.set(legKey(old, k), d.legAlts[k] ?? null);
          vors.set(legKey(old, k), d.legVor[k] ?? null);
          vors2.set(legKey(old, k), d.legVor2[k] ?? null);
        });
        d.legAlts = r.legs.map((_, k) => alts.get(legKey(r, k)) ?? null);
        d.legVor = r.legs.map((_, k) => vors.get(legKey(r, k)) ?? null);
        d.legVor2 = r.legs.map((_, k) => vors2.get(legKey(r, k)) ?? null);
      } else {
        d.legAlts = r.legs.map(() => null);
        d.legVor = r.legs.map(() => null);
        d.legVor2 = r.legs.map(() => null);
      }
      // Keep notes with their waypoint when the route is edited: match by ident, in order.
      const oldIdents = d.resolved?.waypoints.map((w) => w.ident) ?? [];
      const newIdents = r.waypoints.map((w) => w.ident);
      const used = new Set<number>();
      d.notes = newIdents.map((id) => {
        const j = oldIdents.findIndex((o, k) => o === id && !used.has(k));
        if (j < 0) return "";
        used.add(j);
        return d.notes[j] ?? "";
      });
      if (oldIdents.join(" ") !== newIdents.join(" ")) d.ato = newIdents.map(() => null);
      d.resolved = r;
      d.landmarks = d.landmarks.map((lm) => placeLandmark(lm, r));
      this.selectedWp = Math.min(this.selectedWp, newIdents.length - 1);
      for (const icao of this.chartIcaos()) void this.fetchCharts(icao);
    } catch (e) {
      this.error = String(e);
    } finally {
      this.busy = null;
    }
  }

  /** Current real-world wind at cruise altitude along the route (Open-Meteo), then re-resolve. */
  async loadLiveWind() {
    if (!this.doc.resolved) await this.resolve();
    const r = this.doc.resolved;
    if (!r) return;
    this.busy = "Fetching wind…";
    this.error = null;
    try {
      const alt = this.cruiseAltFt();
      const w = await navlog<{ wind: string; valid: string; points: number }>({
        cmd: "wind",
        points: r.waypoints.map((p) => [p.lat, p.lon]),
        alt_ft: alt,
      });
      this.doc.wind = w.wind;
      this.busy = null;
      await this.resolve();
      this.status = `Wind ${w.wind} at ${alt} ft · Open-Meteo forecast ${w.valid}, average of ${w.points} points along the route`;
    } catch (e) {
      this.error = `Wind: ${e}`;
    } finally {
      this.busy = null;
    }
  }

  // --- Altitudes -------------------------------------------------------------------

  /** Cruise altitude as entered, else what the route resolved to. */
  cruiseAltFt(): number {
    const v = Number(this.doc.cruiseAlt);
    return v > 0 ? v : (this.doc.resolved?.legs[0]?.alt_ft ?? 2500);
  }

  /** Altitude for leg k: the per-leg value if set, else cruise (raised to the rule if enabled). */
  legAlt(k: number): number {
    const own = this.doc.legAlts[k];
    if (own != null) return own;
    const cruise = this.cruiseAltFt();
    const leg = this.doc.resolved?.legs[k];
    return this.doc.hemispheric && leg ? semicircularAlt(cruise, leg.mh) : cruise;
  }

  /** The semicircular-rule altitude if leg k does not comply, else null. */
  altAdvice(k: number): number | null {
    const leg = this.doc.resolved?.legs[k];
    if (!leg) return null;
    const alt = this.legAlt(k);
    const ok = semicircularAlt(alt, leg.mh);
    return ok !== alt ? ok : null;
  }

  setLegAlt(k: number, alt: number | null) {
    while (this.doc.legAlts.length <= k) this.doc.legAlts.push(null);
    this.doc.legAlts[k] = alt;
  }

  // --- VOR radial tracking ---------------------------------------------------------

  /** In-range VORs per leg, best for radial tracking first. */
  legCandidates = $derived.by((): LegRadial[][] => {
    const r = this.doc.resolved;
    if (!r?.vors?.length) return [];
    return r.legs.map((_, k) => candidatesForLeg(r.vors, r.waypoints[k], r.waypoints[k + 1]));
  });

  /**
   * The VOR set up on NAV1 or NAV2 for leg k: the pilot's choice, else a suggestion.
   * NAV1 suggests the best trackable station; NAV2 the in-range station nearest the
   * waypoint that is not on NAV1 (its radial over the waypoint is the arrival check).
   */
  legNav(k: number, slot: 1 | 2 = 1): { c: LegRadial; chosen: boolean } | null {
    const r = this.doc.resolved;
    if (!r?.vors?.length) return null;
    const choice = (slot === 1 ? this.doc.legVor : this.doc.legVor2)[k];
    if (choice === "") return null;
    if (choice) {
      const vor = r.vors.find((v) => v.ident === choice);
      return vor ? { c: analyseLeg(vor, r.waypoints[k], r.waypoints[k + 1]), chosen: true } : null;
    }
    const cands = this.legCandidates[k] ?? [];
    if (slot === 1) return cands[0]?.trackable ? { c: cands[0], chosen: false } : null;
    const nav1 = this.legNav(k, 1)?.c.vor.ident;
    const second = cands.filter((c) => c.vor.ident !== nav1).sort((a, b) => a.dEnd - b.dEnd)[0];
    return second ? { c: second, chosen: false } : null;
  }

  setLegVor(k: number, ident: string | null, slot: 1 | 2 = 1) {
    const list = slot === 1 ? this.doc.legVor : this.doc.legVor2;
    while (list.length <= k) list.push(null);
    list[k] = ident;
  }

  // --- Landmarks ----------------------------------------------------------------

  addLandmark(p: LatLon) {
    const r = this.doc.resolved;
    if (!r) return;
    const lm = placeLandmark(
      { id: crypto.randomUUID(), lat: p.lat, lon: p.lon, label: "", leg: 0, along: 0, cross: 0 },
      r,
    );
    this.doc.landmarks.push(lm);
    this.selectedLandmark = lm.id;
    this.selectedWp = lm.leg + 1;
    this.pinMode = false;
  }

  moveLandmark(id: string, p: LatLon) {
    const r = this.doc.resolved;
    const i = this.doc.landmarks.findIndex((l) => l.id === id);
    if (!r || i < 0) return;
    this.doc.landmarks[i] = placeLandmark({ ...this.doc.landmarks[i], lat: p.lat, lon: p.lon }, r);
  }

  removeLandmark(id: string) {
    this.doc.landmarks = this.doc.landmarks.filter((l) => l.id !== id);
    if (this.selectedLandmark === id) this.selectedLandmark = null;
  }

  /** Landmarks along leg k (into waypoint k+1), in flying order. */
  landmarksForLeg(k: number): Landmark[] {
    return this.doc.landmarks.filter((l) => l.leg === k).sort((a, b) => a.along - b.along);
  }

  // --- PDF ------------------------------------------------------------------------

  /** Notes per waypoint for the PDF: free text, then the leg's landmarks. */
  pdfNotes(): string[] {
    const r = this.doc.resolved;
    if (!r) return [];
    return r.waypoints.map((_, i) => {
      const nav1 = i > 0 ? this.legNav(i - 1, 1) : null;
      const nav2 = i > 0 ? this.legNav(i - 1, 2) : null;
      const lines = [
        nav1 ? `NAV1 ${navLine(nav1.c)}` : "",
        nav2 ? `NAV2 ${navLine(nav2.c)}` : "",
        this.doc.notes[i] ?? "",
      ].filter((t) => t.trim());
      if (i > 0) for (const lm of this.landmarksForLeg(i - 1)) lines.push(`• ${landmarkLine(lm, r)}`);
      return lines.join("\n");
    });
  }

  async exportPdf() {
    const d = this.doc;
    if (!d.resolved) return;
    this.busy = "Generating PDF…";
    this.error = null;
    try {
      const r = await navlog<{ path: string }>({
        cmd: "pdf",
        route: d.route,
        aircraft: d.aircraft,
        cruise_alt_ft: d.cruiseAlt ? Number(d.cruiseAlt) : null,
        wind: d.wind || "0/0",
        magvar: d.magvar || "4E",
        notes: this.pdfNotes(),
        alt_profile: d.resolved.legs.map((_, k) => [d.resolved!.waypoints[k].ident, this.legAlt(k)]),
        hemispheric: false,
        vatsim: d.pdf.vatsim,
        phraseology: d.pdf.phraseology,
        wp_maps: d.pdf.wpMaps,
      });
      this.status = `PDF written: ${r.path}`;
    } catch (e) {
      this.error = String(e);
    } finally {
      this.busy = null;
    }
  }

  // --- Offline charts -------------------------------------------------------------

  /** Fetch every chart tile within 5 NM of the route at zooms 7–12 into the disk cache. */
  async cacheTiles() {
    const r = this.doc.resolved;
    if (!r) return;
    const tiles = new Set<string>();
    for (let k = 0; k < r.waypoints.length - 1; k++) {
      const a = r.waypoints[k];
      const b = r.waypoints[k + 1];
      const len = distanceNm(a, b);
      for (let s = 0; s <= len + 0.5; s += 0.5) {
        const c = pointAlong(a, b, Math.min(s, len));
        const [left, right] = perpendicular(a, b, c, 5);
        for (const p of [left, c, right]) {
          for (let z = 7; z <= 12; z++) {
            const { x, y } = tileXY(p, z);
            for (const layer of ["base", "aero"]) tiles.add(`${layer}/${z}/${x}/${y}`);
          }
        }
      }
    }
    const list = [...tiles];
    let done = 0;
    let failed = 0;
    this.error = null;
    const worker = async () => {
      while (list.length) {
        const t = list.pop()!;
        try {
          const resp = await fetch(`${TILE_BASE}/${r.ofm_cycle}/${t}`);
          if (!resp.ok) failed++;
        } catch {
          failed++;
        }
        done++;
        this.busy = `Caching charts ${done}/${tiles.size}…`;
      }
    };
    await Promise.all(Array.from({ length: 6 }, worker));
    this.busy = null;
    this.status = `Charts cached: ${tiles.size - failed} tiles` + (failed ? `, ${failed} unavailable` : "");
  }

  // --- In flight ------------------------------------------------------------------

  /** Index of the leg being flown: -1 before departure, legs.length when arrived. */
  activeLeg = $derived.by(() => {
    const ato = this.doc.ato;
    if (!ato.length || ato[0] == null) return -1;
    let k = 0;
    while (k + 1 < ato.length && ato[k + 1] != null) k++;
    return k;
  });

  markOver() {
    const r = this.doc.resolved;
    if (!r) return;
    if (this.doc.ato.length !== r.waypoints.length) this.doc.ato = r.waypoints.map(() => null);
    const next = this.activeLeg + 1;
    if (next < this.doc.ato.length) this.doc.ato[next] = Date.now();
  }

  undoOver() {
    const k = this.activeLeg;
    if (k >= 0) this.doc.ato[k] = null;
  }

  resetFlight() {
    this.doc.ato = this.doc.ato.map(() => null);
  }

  /** Back to the start of the route: clear the flight log, select departure, show the whole route. */
  resetToStart() {
    this.resetFlight();
    this.selectedWp = 0;
    this.selectedLandmark = null;
    this.pinMode = false;
    this.fitRoute++;
  }

  // --- Airport charts --------------------------------------------------------------

  /** Departure and destination, the airports charts are fetched for. */
  chartIcaos(): string[] {
    const wps = this.doc.resolved?.waypoints;
    if (!wps?.length) return [];
    return [...new Set([wps[0].ident, wps[wps.length - 1].ident].map((i) => i.toUpperCase()))];
  }

  async fetchCharts(icao: string, force = false) {
    if (this.charts[icao] === "loading") return;
    this.charts[icao] = "loading";
    try {
      this.charts[icao] = await navlog<AirportCharts>({ cmd: "charts", icao, force });
    } catch (e) {
      this.charts[icao] = { icao, pages: [], error: String(e) };
    }
  }

  chartsFor(icao: string): AirportCharts | null {
    const c = this.charts[icao.toUpperCase()];
    return c && c !== "loading" ? c : null;
  }

  openCharts(icao: string, page = 0) {
    this.chartView = { icao: icao.toUpperCase(), page };
  }

  // --- X-Plane kneeboard (FlyWithLua window) ------------------------------------

  /** Everything the in-sim kneeboard shows, precomputed (the Lua side only draws). */
  kneeboardData(): Record<string, unknown> | null {
    const r = this.doc.resolved;
    if (!r) return null;
    const wps = r.waypoints;
    const dest = wps[wps.length - 1];
    const live = this.field && this.field.icao === dest.ident.toUpperCase() ? this.field : null;
    const roles: [string, string][] = [["atis", "ATIS"], ["delivery", "Delivery"], ["ground", "Ground"], ["tower", "Tower"], ["approach", "Approach"]];
    const freqs = roles
      .map(([role, label]) => ({ label, freq: live?.freqs[role] ?? r.dest_freqs[role] ?? "", live: !!live?.freqs[role] }))
      .filter((f) => f.freq);
    if (live?.radar) freqs.push({ label: live.radar.name, freq: live.radar.freq, live: true });

    return {
      sent_at: Date.now(),
      route_key: wps.map((w) => w.ident).join(" "),
      title: `${wps[0].ident} -> ${dest.ident} (${r.aircraft.type})`,
      call_leg: r.call_leg_idx != null ? r.call_leg_idx + 1 : null,
      chart_icaos: this.chartIcaos(), // cached pages are copied next to the plan by the bridge
      waypoints: wps.map((w, i) => ({ ident: w.ident, lat: w.lat, lon: w.lon, notes: this.doc.notes[i] ?? "", fixes: w.fixes })),
      legs: r.legs.map((l, k) => {
        return {
          from: l.from,
          to: l.to,
          from_idx: k + 1,
          mh: l.mh,
          alt: this.legAlt(k),
          gs: l.gs_kt,
          dist: l.distance_nm,
          ete_min: l.ete_min,
          nav: this.kneeboardNav(k, 1),
          nav2: this.kneeboardNav(k, 2),
          checkpoints: this.landmarksForLeg(k).map((lm) => ({ label: landmarkLine(lm, r), min: (lm.along / l.gs_kt) * 60 })),
        };
      }),
      dest: {
        ident: dest.ident,
        name: r.dest_info?.name ?? "",
        elev: r.dest_info?.elevation_ft ?? null,
        freqs,
        ils: r.dest_ils.map((i) => `ILS ${i.runway} ${i.ident} ${i.freq_mhz.toFixed(2)}`),
        runways: r.dest_info?.runways ?? [],
        atis: live?.atis ?? [],
        metar: live?.metar ?? null,
        as_of: live?.fetched_at ?? null,
      },
    };
  }

  private kneeboardNav(k: number, slot: 1 | 2): Record<string, unknown> | null {
    const nav = this.legNav(k, slot);
    if (!nav) return null;
    const c = nav.c;
    const { crs, flag } = navCourse(c);
    return {
      ident: c.vor.ident,
      freq: c.vor.freq,
      freq_10khz: Math.round(parseFloat(c.vor.freq) * 100),
      crs, // what the OBS is set to (tuning sets it too)
      crs_flag: flag,
      obs: c.obs,
      flag: c.flag,
      trackable: c.trackable,
      radial_label: c.radialLabel,
      max_dev: c.maxDev,
      r_start: c.rStart,
      r_end: c.rEnd,
      lat: c.vor.lat,
      lon: c.vor.lon,
      var: c.vor.var,
      dme: c.vor.dme,
      suggested: !nav.chosen,
    };
  }

  async sendToXPlane(auto = false) {
    if (!this.doc.resolved) return;
    if (!auto && !this.field) await this.fetchField(); // include live frequencies/ATIS when reachable
    const plan = this.kneeboardData();
    try {
      const res = await navlog<{ plan_path: string; script_installed: boolean; flywithlua: boolean }>({ cmd: "kneeboard", plan });
      this.xpSync = true;
      if (!res.flywithlua) {
        this.error = "FlyWithLua not found in X-Plane — the kneeboard window needs it (Resources/plugins/FlyWithLua).";
      } else if (res.script_installed) {
        this.status = "Kneeboard script installed — in X-Plane: Plugins › FlyWithLua › Reload all Lua script files (once).";
      } else if (!auto) {
        this.status = "Sent to X-Plane — edits now sync automatically.";
      }
    } catch (e) {
      this.error = `Send to X-Plane: ${e}`;
    }
  }

  /** Live frequencies, ATIS and weather for the destination (fails soft: keeps the last good data). */
  async fetchField() {
    const r = this.doc.resolved;
    if (!r || this.fieldBusy) return;
    const icao = r.waypoints[r.waypoints.length - 1].ident;
    this.fieldBusy = true;
    this.fieldError = null;
    try {
      this.field = await navlog<FieldLive>({ cmd: "field", icao, firs: r.firs ?? [] });
    } catch (e) {
      this.fieldError = String(e);
    } finally {
      this.fieldBusy = false;
    }
  }
}

export const app = new AppState();
