<script lang="ts">
  import L from "leaflet";
  import "leaflet/dist/leaflet.css";
  import { onMount } from "svelte";

  import { bearing, destination, perpendicular, pointAlong, type LatLon } from "./geo";
  import { app, landmarkLine, TILE_BASE } from "./state.svelte";
  import { analyseLeg, formatRadial, radialAt } from "./vor";

  let { drPos = null, focusLeg = null }: { drPos?: LatLon | null; focusLeg?: number | null } = $props();

  let el: HTMLDivElement;
  let map = $state.raw<L.Map | null>(null);
  let tiles: L.TileLayer[] = [];
  const routeLayer = L.layerGroup();
  const aidsLayer = L.layerGroup();
  const markLayer = L.layerGroup();
  const drLayer = L.layerGroup();
  const vorLayer = L.layerGroup();

  /** AIRAC cycle in force today (same arithmetic as vfr_navlog.ofm), used before a route is resolved. */
  function airacCycle(d = new Date()): string {
    const DAY = 86_400_000;
    const anchor = Date.UTC(2026, 0, 22); // cycle 2601
    const today = Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate());
    const eff = new Date(anchor + Math.floor((today - anchor) / DAY / 28) * 28 * DAY);
    const n = Math.floor((eff.getTime() - Date.UTC(eff.getUTCFullYear(), 0, 1)) / DAY / 28) + 1;
    return `${String(eff.getUTCFullYear()).slice(2)}${String(n).padStart(2, "0")}`;
  }

  const ll = (p: LatLon): L.LatLngExpression => [p.lat, p.lon];

  onMount(() => {
    const m = L.map(el, { zoomSnap: 0.5 }).setView([51.3, 7.3], 9);
    for (const g of [vorLayer, routeLayer, aidsLayer, markLayer, drLayer]) g.addTo(m);
    m.on("click", (e: L.LeafletMouseEvent) => {
      if (app.pinMode) app.addLandmark({ lat: e.latlng.lat, lon: e.latlng.lng });
    });
    const ro = new ResizeObserver(() => m.invalidateSize());
    ro.observe(el);
    map = m;
    return () => {
      ro.disconnect();
      m.remove();
    };
  });

  // Background: OFM chart (512 px tiles, so Leaflet zoom z shows OFM zoom z-1), satellite
  // imagery (online only, not cached), or nothing so route, waypoints and radials stand out.
  $effect(() => {
    const cycle = app.doc.resolved?.ofm_cycle ?? airacCycle();
    const base = app.mapBase;
    if (!map) return;
    for (const t of tiles) t.remove();
    if (base === "chart") {
      const attribution = `© <a href="https://www.openflightmaps.org">openflightmaps</a> · AIRAC ${cycle}`;
      const opts = { tileSize: 512, zoomOffset: -1, minZoom: 6, maxZoom: 15 };
      tiles = [
        L.tileLayer(`${TILE_BASE}/${cycle}/base/{z}/{x}/{y}`, { ...opts, maxNativeZoom: 13, attribution }),
        L.tileLayer(`${TILE_BASE}/${cycle}/aero/{z}/{x}/{y}`, { ...opts, maxNativeZoom: 12 }),
      ];
    } else if (base === "satellite") {
      tiles = [
        L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}", {
          minZoom: 6,
          maxZoom: 17,
          attribution: "Imagery © Esri, Maxar, Earthstar Geographics",
        }),
      ];
    } else {
      tiles = [];
    }
    for (const t of tiles) t.addTo(map).bringToBack();
    tiles[1]?.bringToFront();
  });

  // Zoom to the whole route whenever a new route is resolved, a plan is opened, or on "reset to start".
  $effect(() => {
    app.fitRoute;
    const r = app.doc.resolved;
    if (!map || !r || focusLeg != null) return;
    map.fitBounds(L.latLngBounds(r.waypoints.map(ll)), { padding: [40, 40] });
  });

  // In flight: keep the current leg in view.
  $effect(() => {
    const r = app.doc.resolved;
    if (!map || !r || focusLeg == null) return;
    const a = r.waypoints[Math.max(0, Math.min(focusLeg, r.waypoints.length - 2))];
    const b = r.waypoints[Math.max(1, Math.min(focusLeg + 1, r.waypoints.length - 1))];
    map.fitBounds(L.latLngBounds([ll(a), ll(b)]), { padding: [60, 60], maxZoom: 12 });
  });

  // Route, waypoints, minute ticks and drift lines.
  $effect(() => {
    const r = app.doc.resolved;
    const showTicks = app.showTicks;
    const showFans = app.showFans;
    const selected = app.selectedWp;
    routeLayer.clearLayers();
    aidsLayer.clearLayers();
    if (!r) return;
    const wps = r.waypoints;

    L.polyline(wps.map(ll), { color: "#c2185b", weight: 4, opacity: 0.85 }).addTo(routeLayer);
    if (focusLeg != null && focusLeg >= 0 && focusLeg < wps.length - 1) {
      L.polyline([ll(wps[focusLeg]), ll(wps[focusLeg + 1])], { color: "#ff6d00", weight: 7 }).addTo(routeLayer);
    }
    wps.forEach((w, i) => {
      L.circleMarker(ll(w), {
        radius: i === selected ? 9 : 7,
        color: "#c2185b",
        weight: 3,
        fillColor: i === selected ? "#ffeb3b" : "#fff",
        fillOpacity: 1,
      })
        .bindTooltip(w.ident, { permanent: true, direction: "right", className: "wp-label", offset: [8, 0] })
        .on("click", () => (app.selectedWp = i))
        .addTo(routeLayer);
    });

    for (let k = 0; k < wps.length - 1; k++) {
      const a = wps[k];
      const b = wps[k + 1];
      const leg = r.legs[k];
      if (showTicks && leg) {
        for (let m = 1; (leg.gs_kt * m) / 60 < leg.distance_nm - 0.2; m++) {
          const p = pointAlong(a, b, (leg.gs_kt * m) / 60);
          const major = m % 5 === 0;
          L.polyline(perpendicular(a, b, p, major ? 0.9 : 0.45).map(ll), {
            color: "#c2185b",
            weight: major ? 3 : 2,
          }).addTo(aidsLayer);
          if (major) {
            L.marker(ll(p), {
              icon: L.divIcon({ className: "tick-label", html: `${m}'`, iconSize: [30, 14], iconAnchor: [-8, 7] }),
              interactive: false,
            }).addTo(aidsLayer);
          }
        }
      }
      if (showFans && leg) {
        // ±10° from the leg start (track error) and into the waypoint (closing angle).
        const brg = bearing(a, b);
        for (const off of [-10, 10]) {
          L.polyline([ll(a), ll(destination(a, brg + off, leg.distance_nm))], {
            color: "#6a1b9a", weight: 1.5, dashArray: "6 6", opacity: 0.8, interactive: false,
          }).addTo(aidsLayer);
          L.polyline([ll(b), ll(destination(b, brg + 180 + off, leg.distance_nm))], {
            color: "#6a1b9a", weight: 1.5, dashArray: "2 6", opacity: 0.8, interactive: false,
          }).addTo(aidsLayer);
        }
      }
    }
  });

  // Landmark pins (draggable in planning), with a dotted line to their foot point on track.
  $effect(() => {
    const r = app.doc.resolved;
    const selected = app.selectedLandmark;
    const lms = app.doc.landmarks.map((l) => ({ ...l }));
    markLayer.clearLayers();
    if (!r) return;
    for (const lm of lms) {
      const a = r.waypoints[lm.leg];
      const b = r.waypoints[lm.leg + 1];
      if (a && b) {
        L.polyline([ll(lm), ll(pointAlong(a, b, lm.along))], {
          color: "#0d47a1", weight: 1.5, dashArray: "3 4", interactive: false,
        }).addTo(markLayer);
      }
      const cls = lm.id === selected ? "lm-pin selected" : "lm-pin";
      L.marker(ll(lm), {
        draggable: app.mode === "plan",
        icon: L.divIcon({ className: cls, html: "<span></span>", iconSize: [16, 16], iconAnchor: [8, 8] }),
      })
        .bindTooltip(lm.label || "(unnamed)", { direction: "top", offset: [0, -8] })
        .bindPopup(landmarkLine(lm, r))
        .on("click", () => {
          app.selectedLandmark = lm.id;
          app.selectedWp = lm.leg + 1;
        })
        .on("dragend", (e: L.LeafletEvent) => {
          const p = (e.target as L.Marker).getLatLng();
          app.moveLandmark(lm.id, { lat: p.lat, lon: p.lng });
        })
        .addTo(markLayer);
    }
  });

  const textIcon = (html: string, cls: string) =>
    L.divIcon({ className: cls, html, iconSize: [90, 16], iconAnchor: [45, 8] });

  // VOR stations; the selected one gets spokes (radial over each waypoint) and
  // the legs flyable on its radials are drawn green with their OBS setting.
  $effect(() => {
    const r = app.doc.resolved;
    const sel = app.selectedVor;
    const dr = drPos;
    const navLeg = focusLeg;
    vorLayer.clearLayers();
    if (!r?.vors) return;
    for (const v of r.vors) {
      L.marker(ll(v), {
        icon: L.divIcon({
          className: v.ident === sel ? "vor-pin selected" : "vor-pin",
          html: `<span></span><b>${v.ident}</b>`,
          iconSize: [14, 14],
          iconAnchor: [7, 7],
        }),
      })
        .bindTooltip(`${v.ident} ${v.freq} · ${v.name}${v.dme ? " · DME" : ""}`, { direction: "top", offset: [0, -8] })
        .on("click", () => (app.selectedVor = v.ident === app.selectedVor ? null : v.ident))
        .addTo(vorLayer);
    }

    const selVor = r.vors.find((v) => v.ident === sel);
    if (selVor) {
      for (const w of r.waypoints) {
        L.polyline([ll(selVor), ll(w)], { color: "#1565c0", weight: 1.5, dashArray: "4 6", opacity: 0.9, interactive: false }).addTo(vorLayer);
        const at = { lat: selVor.lat + (w.lat - selVor.lat) * 0.8, lon: selVor.lon + (w.lon - selVor.lon) * 0.8 };
        L.marker(ll(at), { icon: textIcon(formatRadial(radialAt(selVor, w)), "radial-label"), interactive: false }).addTo(vorLayer);
      }
      r.legs.forEach((_, k) => {
        const a = r.waypoints[k];
        const b = r.waypoints[k + 1];
        const c = analyseLeg(selVor, a, b);
        if (!c.trackable) return;
        L.polyline([ll(a), ll(b)], { color: "#2e7d32", weight: 8, opacity: 0.55, interactive: false }).addTo(vorLayer);
        const mid = { lat: (a.lat + b.lat) / 2, lon: (a.lon + b.lon) / 2 };
        L.marker(ll(mid), {
          icon: textIcon(`OBS ${String(c.obs).padStart(3, "0")} ${c.flag}`, "obs-label"),
          interactive: false,
        }).addTo(vorLayer);
      });
    }

    // In flight: the radial you should be on right now, from the leg's VOR to the DR position.
    if (dr && navLeg != null) {
      const nav = app.legNav(navLeg);
      if (nav) {
        L.polyline([ll(nav.c.vor), ll(dr)], { color: "#e65100", weight: 2, dashArray: "8 5", interactive: false }).addTo(vorLayer);
      }
    }
  });

  // Dead-reckoning position (where the clock says you are).
  $effect(() => {
    drLayer.clearLayers();
    if (!drPos) return;
    L.circleMarker(ll(drPos), { radius: 10, color: "#e65100", weight: 3, fillColor: "#ffab40", fillOpacity: 0.9 })
      .bindTooltip("DR", { permanent: true, direction: "left", className: "dr-label", offset: [-10, 0] })
      .addTo(drLayer);
  });
</script>

<div class="map" class:pin-mode={app.pinMode} class:plain={app.mapBase === "none"} bind:this={el}></div>

<style>
  .map {
    width: 100%;
    height: 100%;
    background: #dfe6e9;
  }
  .map.plain {
    background: #fafbfc;
  }
  .pin-mode :global(.leaflet-container),
  .map.pin-mode {
    cursor: crosshair;
  }
  :global(.wp-label) {
    font: 700 12px/1.2 system-ui, sans-serif;
    padding: 1px 5px;
  }
  :global(.dr-label) {
    font: 700 12px system-ui, sans-serif;
    color: #e65100;
  }
  :global(.tick-label) {
    font: 700 11px system-ui, sans-serif;
    color: #c2185b;
    text-shadow: 0 0 3px #fff, 0 0 3px #fff;
    white-space: nowrap;
  }
  :global(.vor-pin span) {
    display: block;
    width: 10px;
    height: 10px;
    margin: 1px;
    border: 2px solid #1565c0;
    border-radius: 50%;
    background: #fff;
    box-shadow: inset 0 0 0 2px #fff, inset 0 0 0 4px #1565c0;
  }
  :global(.vor-pin b) {
    position: absolute;
    left: 16px;
    top: -2px;
    font: 700 11px system-ui, sans-serif;
    color: #0d47a1;
    text-shadow: 0 0 3px #fff, 0 0 3px #fff;
  }
  :global(.vor-pin.selected span) {
    background: #ffeb3b;
    transform: scale(1.4);
  }
  :global(.radial-label),
  :global(.obs-label) {
    font: 700 11px system-ui, sans-serif;
    color: #0d47a1;
    text-align: center;
    text-shadow: 0 0 3px #fff, 0 0 3px #fff, 0 0 3px #fff;
    white-space: nowrap;
  }
  :global(.obs-label) {
    color: #1b5e20;
    font-size: 12px;
  }
  :global(.lm-pin span) {
    display: block;
    width: 12px;
    height: 12px;
    margin: 2px;
    background: #1565c0;
    border: 2px solid #fff;
    transform: rotate(45deg);
    box-shadow: 0 0 0 1px #0d47a1;
  }
  :global(.lm-pin.selected span) {
    background: #ffeb3b;
    box-shadow: 0 0 0 2px #0d47a1;
  }
</style>
