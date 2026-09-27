<script lang="ts">
  import { onMount } from "svelte";

  import ChartViewer from "$lib/ChartViewer.svelte";
  import { pointAlong, type LatLon } from "$lib/geo";
  import Kneeboard from "$lib/Kneeboard.svelte";
  import MapView from "$lib/MapView.svelte";
  import PlanPanel from "$lib/PlanPanel.svelte";
  import { app } from "$lib/state.svelte";

  onMount(() => {
    app.init();
    const timer = setInterval(() => (app.now = Date.now()), 1000);
    return () => clearInterval(timer);
  });

  // Autosave the working plan so a crash or accidental close never loses notes or the flight log.
  $effect(() => {
    JSON.stringify(app.doc);
    app.autosave();
  });

  // Once sent, keep the X-Plane kneeboard in step with plan edits and fresh destination data.
  $effect(() => {
    if (!app.xpSync) return;
    JSON.stringify(app.doc);
    app.field;
    JSON.stringify(app.charts);
    const t = setTimeout(() => app.sendToXPlane(true), 2000);
    return () => clearTimeout(t);
  });

  // Clear the status line after a few seconds.
  $effect(() => {
    if (!app.status) return;
    const t = setTimeout(() => (app.status = null), 6000);
    return () => clearTimeout(t);
  });

  /** Dead-reckoning position: planned GS × time since the last waypoint, along the current leg. */
  const drPos = $derived.by((): LatLon | null => {
    const r = app.doc.resolved;
    const k = app.activeLeg;
    if (app.mode !== "fly" || !r || k < 0 || k >= r.waypoints.length - 1) return null;
    const start = app.doc.ato[k];
    if (start == null) return null;
    const leg = r.legs[k];
    const along = Math.min((leg.gs_kt * (app.now - start)) / 3_600_000, leg.distance_nm * 1.5);
    return pointAlong(r.waypoints[k], r.waypoints[k + 1], along);
  });
  const focusLeg = $derived(app.mode === "fly" && app.doc.resolved ? Math.max(0, app.activeLeg) : null);

  const title = $derived.by(() => {
    const wps = app.doc.resolved?.waypoints;
    const route = wps ? `${wps[0].ident} → ${wps[wps.length - 1].ident}` : "New plan";
    const file = app.filePath ? app.filePath.split(/[\\/]/).pop() : "unsaved";
    return `${route} · ${file}${app.dirty ? " •" : ""}`;
  });

  function onKey(e: KeyboardEvent) {
    const mod = e.ctrlKey || e.metaKey;
    if (mod && e.key === "s") {
      e.preventDefault();
      app.savePlan(e.shiftKey);
    } else if (mod && e.key === "o") {
      e.preventDefault();
      app.openPlan();
    } else if (e.key === "Escape") {
      app.pinMode = false;
    }
  }
</script>

<svelte:window onkeydown={onKey} />

<ChartViewer />

<div class="app" class:fly={app.mode === "fly"}>
  <header>
    <strong>VFR Navlog</strong>
    <span class="title">{title}</span>
    <div class="group">
      <button onclick={() => app.newPlan()}>New</button>
      <button onclick={() => app.openPlan()}>Open</button>
      <button onclick={() => app.savePlan()}>Save</button>
      <button onclick={() => app.savePlan(true)}>Save as</button>
    </div>
    {#if app.mode === "plan"}
      <button onclick={() => app.resetToStart()} disabled={!app.doc.resolved} title="Clear the flight log, select the departure and show the whole route">
        ⟲ Reset to start
      </button>
    {/if}
    <div class="group seg">
      <button class:on={app.mode === "plan"} onclick={() => (app.mode = "plan")}>Plan</button>
      <button class:on={app.mode === "fly"} onclick={() => (app.mode = "fly")}>Fly</button>
    </div>
    <div class="group">
      <button
        onclick={() => app.sendToXPlane()}
        disabled={!app.doc.resolved}
        title="Show this plan in the X-Plane kneeboard window (FlyWithLua)"
      >
        {app.xpSync ? "● X-Plane synced" : "Send to X-Plane"}
      </button>
      <button onclick={() => app.cacheTiles()} disabled={!app.doc.resolved || !!app.busy} title="Download charts along the route for offline use">Cache charts</button>
      <button class="primary" onclick={() => app.exportPdf()} disabled={!app.doc.resolved || !!app.busy}>Export PDF</button>
    </div>
  </header>

  {#if app.busy || app.error || app.status}
    <div class="bar" class:error={!!app.error}>
      {#if app.busy}<span class="spinner"></span>{app.busy}{/if}
      {#if app.error}<span>{app.error}</span><button class="icon" onclick={() => (app.error = null)}>✕</button>{/if}
      {#if !app.busy && !app.error && app.status}{app.status}{/if}
    </div>
  {/if}

  <main>
    <aside>
      {#if app.mode === "plan"}
        <PlanPanel />
      {:else}
        <Kneeboard />
      {/if}
    </aside>
    <section class="mapwrap">
      <div class="maptools">
        {#if app.mode === "plan"}
          <button class:on={app.pinMode} onclick={() => (app.pinMode = !app.pinMode)} disabled={!app.doc.resolved}>
            {app.pinMode ? "Click the map… (Esc)" : "＋ Landmark"}
          </button>
        {/if}
        <label><input type="checkbox" bind:checked={app.showTicks} /> Minute ticks</label>
        <label><input type="checkbox" bind:checked={app.showFans} /> ±10° lines</label>
      </div>
      <MapView {drPos} {focusLeg} />
    </section>
  </main>
</div>

<style>
  :global(:root) {
    --bg: #f4f6f8;
    --fg: #1d2327;
    --muted: #5f6b73;
    --line: #d5dce1;
    --card: #ffffff;
    --accent: #1565c0;
    --danger: #c62828;
    --input: #ffffff;
    color-scheme: light;
  }
  :global(.app.fly) {
    /* Kneeboard: dark, low glare. */
    --bg: #111416;
    --fg: #e8ecef;
    --muted: #93a1ab;
    --line: #2c3338;
    --card: #1a1f22;
    --accent: #4fc3f7;
    --danger: #ff6e6e;
    --input: #0d1012;
    color-scheme: dark;
  }
  :global(html, body) {
    margin: 0;
    height: 100%;
    font: 14px/1.4 system-ui, "Segoe UI", sans-serif;
  }
  :global(input, select, textarea) {
    font: inherit;
    color: var(--fg);
    background: var(--input);
    border: 1px solid var(--line);
    border-radius: 5px;
    padding: 4px 6px;
    box-sizing: border-box;
  }
  :global(button) {
    font: inherit;
    color: var(--fg);
    background: var(--card);
    border: 1px solid var(--line);
    border-radius: 6px;
    padding: 5px 10px;
    cursor: pointer;
  }
  :global(button:disabled) {
    opacity: 0.5;
    cursor: default;
  }
  :global(button.primary) {
    background: var(--accent);
    border-color: var(--accent);
    color: #fff;
    font-weight: 600;
  }
  .app {
    height: 100vh;
    display: flex;
    flex-direction: column;
    background: var(--bg);
    color: var(--fg);
  }
  header {
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 6px 12px;
    border-bottom: 1px solid var(--line);
    background: var(--card);
  }
  .title {
    color: var(--muted);
    flex: 1;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .group {
    display: flex;
    gap: 4px;
  }
  .seg button.on,
  .maptools button.on {
    background: var(--accent);
    border-color: var(--accent);
    color: #fff;
  }
  .bar {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 5px 12px;
    font-size: 13px;
    background: color-mix(in srgb, var(--accent) 12%, var(--card));
    border-bottom: 1px solid var(--line);
  }
  .bar.error {
    background: color-mix(in srgb, var(--danger) 18%, var(--card));
  }
  .bar .icon {
    margin-left: auto;
    padding: 1px 6px;
  }
  .spinner {
    width: 12px;
    height: 12px;
    border: 2px solid var(--accent);
    border-right-color: transparent;
    border-radius: 50%;
    animation: spin 0.8s linear infinite;
  }
  @keyframes spin {
    to {
      transform: rotate(360deg);
    }
  }
  main {
    flex: 1;
    display: flex;
    min-height: 0;
  }
  aside {
    width: 460px;
    flex: none;
    overflow-y: auto;
    border-right: 1px solid var(--line);
  }
  .fly aside {
    width: 520px;
  }
  .mapwrap {
    flex: 1;
    position: relative;
  }
  .maptools {
    position: absolute;
    z-index: 1000;
    top: 10px;
    right: 10px;
    display: flex;
    gap: 8px;
    align-items: center;
    padding: 6px 8px;
    border-radius: 8px;
    background: color-mix(in srgb, var(--card) 92%, transparent);
    border: 1px solid var(--line);
    font-size: 13px;
  }
  .maptools label {
    display: flex;
    align-items: center;
    gap: 4px;
  }
</style>
