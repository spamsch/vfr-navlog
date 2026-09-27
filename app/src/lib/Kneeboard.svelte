<script lang="ts">
  import { confirm } from "@tauri-apps/plugin-dialog";

  import DestBox from "./DestBox.svelte";
  import { distanceNm, mmss, pointAlong, utc } from "./geo";
  import { app, landmarkLine } from "./state.svelte";
  import { formatRadial, radialAt } from "./vor";

  const r = $derived(app.doc.resolved);
  const ato = $derived(app.doc.ato);
  const k = $derived(app.activeLeg);
  const n = $derived(r ? r.waypoints.length : 0);
  const arrived = $derived(r != null && k === n - 1);
  const leg = $derived(r && k >= 0 && k < n - 1 ? r.legs[k] : null);
  const legStart = $derived(k >= 0 ? (ato[k] ?? null) : null);
  const elapsedS = $derived(legStart != null ? (app.now - legStart) / 1000 : 0);
  const remainingS = $derived(leg ? leg.ete_min * 60 - elapsedS : 0);

  /** Actual ÷ planned time on the last completed leg: >1 means slower than planned. */
  const factor = $derived.by(() => {
    if (!r || k < 1 || ato[k] == null || ato[k - 1] == null) return 1;
    const actualMin = (ato[k]! - ato[k - 1]!) / 60000;
    return actualMin / r.legs[k - 1].ete_min;
  });
  const lastActualGs = $derived(r && k >= 1 && factor > 0 ? r.legs[k - 1].gs_kt / factor : null);

  /** Planned and revised ETA over each remaining waypoint, from the last ATO. */
  const etas = $derived.by(() => {
    if (!r || k < 0 || legStart == null) return [];
    const rows: { i: number; planned: number; revised: number }[] = [];
    let planned = legStart;
    let revised = legStart;
    for (let j = k; j < n - 1; j++) {
      planned += r.legs[j].ete_min * 60000;
      revised += r.legs[j].ete_min * 60000 * factor;
      rows.push({ i: j + 1, planned, revised });
    }
    return rows;
  });

  const checkpoints = $derived(
    leg && legStart != null
      ? app.landmarksForLeg(k).map((lm) => ({ lm, due: legStart + (lm.along / leg.gs_kt) * 3_600_000 }))
      : [],
  );

  const pad3 = (n: number) => String(Math.round(n)).padStart(3, "0");

  // NAV: the VOR for this leg (chosen or suggested), and what it should read now at the DR position.
  const navLegIdx = $derived(k < 0 ? 0 : k);
  const nav = $derived(r && navLegIdx < n - 1 ? app.legNav(navLegIdx) : null);
  const nextNav = $derived(r && k >= 0 && k + 1 < n - 1 ? app.legNav(k + 1) : null);
  const dr = $derived(
    r && leg && legStart != null
      ? pointAlong(r.waypoints[k], r.waypoints[k + 1], Math.min((leg.gs_kt * elapsedS) / 3600, leg.distance_nm))
      : null,
  );
  const drRadial = $derived(nav && dr ? radialAt(nav.c.vor, dr) : null);
  const drDme = $derived(nav && dr ? distanceNm(nav.c.vor, dr) : null);

  // Altitude change at the end of this leg.
  const altChange = $derived.by(() => {
    if (!r || k < 0 || k + 1 >= n - 1) return null;
    const now = app.legAlt(k);
    const next = app.legAlt(k + 1);
    return next === now ? null : { to: next, climb: next > now };
  });

  // Destination info opens by itself from the tower-call leg (or the last leg) on.
  const nearDest = $derived(
    !!r && k >= 0 && (arrived || k >= n - 2 || (r.call_leg_idx != null && k >= r.call_leg_idx)),
  );
  let destManual = $state<boolean | null>(null);
  const destOpen = $derived(destManual ?? nearDest);
  $effect(() => {
    nearDest; // a new phase of flight resets a manual open/close
    destManual = null;
  });

  // 1-in-60: off-track distance → heading correction.
  let offNm = $state(1);
  let offSide = $state<"L" | "R">("R");
  let flownOverride = $state<number | null>(null);
  const flownNm = $derived(flownOverride ?? (leg ? Math.max(0.1, Math.round(((leg.gs_kt * elapsedS) / 3600) * 10) / 10) : 0));
  const toGoNm = $derived(leg ? Math.max(0.1, leg.distance_nm - flownNm) : 0);
  const trackError = $derived(flownNm > 0 ? (60 * offNm) / flownNm : 0);
  const closing = $derived(toGoNm > 0 ? (60 * offNm) / toGoNm : 0);
  const turnSign = $derived(offSide === "R" ? -1 : 1); // right of track → turn left
  const hdg = (base: number, delta: number) => String(Math.round((base + delta + 360) % 360)).padStart(3, "0");
</script>

{#snippet destination()}
  <button class="desttoggle" onclick={() => (destManual = !destOpen)}>
    {destOpen ? "▾" : "▸"} Destination {r ? r.waypoints[n - 1].ident : ""} · frequencies, ATIS, ILS
  </button>
  {#if destOpen}<DestBox />{/if}
{/snippet}

{#if !r}
  <p class="empty">Resolve a route in the planning view first.</p>
{:else}
  <div class="kb">
    <div class="clock">{utc(app.now)}</div>

    {#if k < 0}
      <div class="big-leg">
        <div class="to">Ready · {r.waypoints[0].ident} → {r.waypoints[1].ident}</div>
        <div class="grid">
          <div><span>MH</span><b class="huge">{String(r.legs[0].mh).padStart(3, "0")}°</b></div>
          <div><span>ALT</span><b>{app.legAlt(0)}</b></div>
          <div><span>DIST</span><b>{r.legs[0].distance_nm}</b></div>
          <div><span>ETE</span><b>{mmss(r.legs[0].ete_min * 60)}</b></div>
        </div>
      </div>

      {#if nav}
        <div class="box navbox" class:trackable={nav.c.trackable}>
          <h3>NAV{nav.chosen ? "" : " · suggested"}</h3>
          <div class="navmain">
            <b>{nav.c.vor.ident} {nav.c.vor.freq}</b>
            {#if nav.c.trackable}
              <span>OBS <b class="obs">{pad3(nav.c.obs)}</b> {nav.c.flag}</span>
            {:else}
              <span>{formatRadial(nav.c.rStart)} → {formatRadial(nav.c.rEnd)}</span>
            {/if}
          </div>
          <p>
            {#if nav.c.trackable}{nav.c.radialLabel} · needle within {Math.max(1, Math.round(nav.c.maxDev))}°{:else}not along a radial — use as progress check{/if}
            {#if drRadial != null} · now <b>{formatRadial(drRadial)}</b>{#if nav.c.vor.dme && drDme != null} · DME <b>{drDme.toFixed(1)}</b>{/if}{/if}
          </p>
        </div>
      {/if}
      <button class="over primary" onclick={() => app.markOver()}>Start · over {r.waypoints[0].ident} now</button>
    {:else if arrived}
      <div class="big-leg">
        <div class="to">Arrived {r.waypoints[n - 1].ident}</div>
        <p>Block time {mmss((ato[n - 1]! - ato[0]!) / 1000)} · planned {mmss(r.legs.reduce((s, l) => s + l.ete_min, 0) * 60)}</p>
      </div>
      {@render destination()}
    {:else if leg}
      <div class="big-leg">
        <div class="to">{leg.from} → <b>{leg.to}</b> <small>leg {k + 1}/{n - 1}</small></div>
        <div class="grid">
          <div><span>MH</span><b class="huge">{String(leg.mh).padStart(3, "0")}°</b></div>
          <div><span>ALT</span><b>{app.legAlt(k)}</b></div>
          <div><span>GS</span><b>{leg.gs_kt}</b></div>
          <div><span>DIST</span><b>{leg.distance_nm}</b></div>
        </div>
        <div class="timer" class:late={remainingS < 0}>
          <div><span>{remainingS >= 0 ? "to " + leg.to : "overdue"}</span><b>{mmss(remainingS)}</b></div>
          <div><span>ETO</span><b>{utc(legStart! + leg.ete_min * 60000, false)}</b></div>
          <div><span>elapsed</span><b>{mmss(elapsedS)}</b></div>
        </div>
      </div>

      {#if nearDest}{@render destination()}{/if}

      {#if altChange}
        <div class="altchange">At {leg.to}: {altChange.climb ? "climb" : "descend"} to <b>{altChange.to} ft</b></div>
      {/if}

      {#if nav}
        <div class="box navbox" class:trackable={nav.c.trackable}>
          <h3>NAV{nav.chosen ? "" : " · suggested"}</h3>
          <div class="navmain">
            <b>{nav.c.vor.ident} {nav.c.vor.freq}</b>
            {#if nav.c.trackable}
              <span>OBS <b class="obs">{pad3(nav.c.obs)}</b> {nav.c.flag}</span>
            {:else}
              <span>{formatRadial(nav.c.rStart)} → {formatRadial(nav.c.rEnd)}</span>
            {/if}
          </div>
          <p>
            {#if nav.c.trackable}{nav.c.radialLabel} · needle within {Math.max(1, Math.round(nav.c.maxDev))}°{:else}not along a radial — use as progress check{/if}
            {#if drRadial != null} · now <b>{formatRadial(drRadial)}</b>{#if nav.c.vor.dme && drDme != null} · DME <b>{drDme.toFixed(1)}</b>{/if}{/if}
          </p>
        </div>
      {/if}

      {#if nextNav}
        <div class="nextnav">
          Next leg: {nextNav.c.vor.ident} {nextNav.c.vor.freq}
          {#if nextNav.c.trackable}· OBS {pad3(nextNav.c.obs)} {nextNav.c.flag}{:else}· {formatRadial(nextNav.c.rStart)}→{formatRadial(nextNav.c.rEnd)}{/if}
        </div>
      {/if}

      <div class="actions">
        <button class="over primary" onclick={() => app.markOver()}>Over {leg.to} now</button>
        <button onclick={() => app.undoOver()} title="Undo the last time over">Undo</button>
      </div>

      {#if checkpoints.length}
        <div class="box">
          <h3>Checkpoints on this leg</h3>
          {#each checkpoints as c (c.lm.id)}
            {@const dt = (c.due - app.now) / 1000}
            <div class="cp" class:past={dt < -30} class:soon={dt >= -30 && dt < 60}>
              <span class="lbl">{landmarkLine(c.lm, r)}</span>
              <b>{dt >= 0 ? "in " + mmss(dt) : "passed"}</b>
            </div>
          {/each}
        </div>
      {/if}

      {#if app.doc.notes[k + 1]?.trim() || r.waypoints[k + 1].fixes.length}
        <div class="box notes">
          <h3>Notes · {leg.to}</h3>
          {#if app.doc.notes[k + 1]?.trim()}<p>{app.doc.notes[k + 1]}</p>{/if}
          {#if r.waypoints[k + 1].fixes.length}<p class="mono">{r.waypoints[k + 1].fixes.join("   ·   ")}</p>{/if}
        </div>
      {/if}

      <div class="box">
        <h3>1 in 60</h3>
        <div class="sixty">
          <label>Off track <input type="number" step="0.1" min="0" bind:value={offNm} /> NM</label>
          <div class="seg">
            <button class:on={offSide === "L"} onclick={() => (offSide = "L")}>L</button>
            <button class:on={offSide === "R"} onclick={() => (offSide = "R")}>R</button>
          </div>
          <label>
            flown
            <input type="number" step="0.5" min="0.1" value={flownNm} oninput={(e) => (flownOverride = Number(e.currentTarget.value) || null)} /> NM
          </label>
          {#if flownOverride != null}<button class="link" onclick={() => (flownOverride = null)}>use DR</button>{/if}
        </div>
        <p>
          Track error <b>{trackError.toFixed(0)}°</b> · closing angle <b>{closing.toFixed(0)}°</b> ({toGoNm.toFixed(1)} NM to go)
        </p>
        <p>
          To {leg.to}: heading <b class="corr">{hdg(leg.mh, turnSign * (trackError + closing))}°</b>
          &nbsp;·&nbsp; parallel track: <b>{hdg(leg.mh, turnSign * trackError)}°</b>
        </p>
      </div>
    {/if}

    {#if k >= 0 && !arrived}
      <div class="box">
        <h3>ETAs {#if k >= 1}<small>last leg GS {lastActualGs?.toFixed(0)} kt · ×{factor.toFixed(2)}</small>{/if}</h3>
        <table>
          <thead><tr><th>WP</th><th>planned</th><th>revised</th><th>ATO</th></tr></thead>
          <tbody>
            {#each r.waypoints as wp, i (i)}
              {@const row = etas.find((e) => e.i === i)}
              <tr class:current={i === k + 1}>
                <td>{wp.ident}</td>
                <td>{row ? utc(row.planned, false) : ""}</td>
                <td>{row ? utc(row.revised, false) : ""}</td>
                <td>{ato[i] != null ? utc(ato[i]!, false) : ""}</td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    {/if}

    {#if !nearDest && !arrived}{@render destination()}{/if}

    {#if k >= 0}
      <button class="reset" onclick={async () => (await confirm("Clear all times over waypoints?", { title: "Reset flight log" })) && app.resetFlight()}>Reset flight log</button>
    {/if}
  </div>
{/if}

<style>
  .kb {
    padding: 12px;
    display: grid;
    gap: 10px;
  }
  .clock {
    font: 600 18px ui-monospace, Consolas, monospace;
    text-align: right;
    color: var(--muted);
  }
  .big-leg {
    background: var(--card);
    border: 1px solid var(--line);
    border-radius: 10px;
    padding: 12px;
  }
  .to {
    font-size: 20px;
    margin-bottom: 8px;
  }
  .to small {
    color: var(--muted);
    font-size: 13px;
    margin-left: 6px;
  }
  .grid,
  .timer {
    display: grid;
    grid-template-columns: 1.4fr 1fr 1fr 1fr;
    gap: 8px;
    align-items: end;
  }
  .timer {
    grid-template-columns: 1.4fr 1fr 1fr;
    margin-top: 10px;
    padding-top: 10px;
    border-top: 1px solid var(--line);
  }
  .grid span,
  .timer span {
    display: block;
    font-size: 11px;
    color: var(--muted);
    text-transform: uppercase;
  }
  .grid b {
    font-size: 26px;
    font-variant-numeric: tabular-nums;
  }
  .grid b.huge {
    font-size: 44px;
  }
  .timer b {
    font: 700 28px ui-monospace, Consolas, monospace;
  }
  .timer.late b:first-of-type,
  .timer.late div:first-child b {
    color: var(--danger);
  }
  .actions {
    display: flex;
    gap: 8px;
  }
  .over {
    flex: 1;
    font-size: 18px;
    padding: 14px;
  }
  .box {
    border: 1px solid var(--line);
    border-radius: 8px;
    padding: 8px 10px;
    background: var(--card);
  }
  h3 {
    margin: 0 0 6px;
    font-size: 12px;
    text-transform: uppercase;
    color: var(--muted);
  }
  h3 small {
    text-transform: none;
    margin-left: 8px;
  }
  .cp {
    display: flex;
    justify-content: space-between;
    gap: 8px;
    padding: 3px 0;
    font-size: 14px;
  }
  .cp.soon {
    color: var(--accent);
    font-weight: 700;
  }
  .cp.past {
    color: var(--muted);
    text-decoration: line-through;
  }
  .notes p {
    white-space: pre-wrap;
    margin: 0 0 4px;
    font-size: 15px;
  }
  .mono {
    font-family: ui-monospace, Consolas, monospace;
    font-size: 12.5px !important;
  }
  .sixty {
    display: flex;
    gap: 8px;
    align-items: center;
    flex-wrap: wrap;
    font-size: 13px;
  }
  .sixty input {
    width: 64px;
  }
  .seg button {
    padding: 4px 10px;
  }
  .seg button.on {
    background: var(--accent);
    color: #fff;
  }
  .corr {
    font-size: 20px;
  }
  .link {
    background: none;
    border: none;
    color: var(--accent);
    text-decoration: underline;
    padding: 0;
  }
  table {
    width: 100%;
    border-collapse: collapse;
    font: 13px ui-monospace, Consolas, monospace;
  }
  th {
    text-align: left;
    color: var(--muted);
    font-weight: 500;
  }
  tr.current {
    background: color-mix(in srgb, var(--accent) 18%, transparent);
    font-weight: 700;
  }
  .reset {
    justify-self: start;
    color: var(--muted);
  }
  .empty {
    padding: 16px;
    color: var(--muted);
  }
  .desttoggle {
    text-align: left;
    font-weight: 600;
  }
  .navbox.trackable {
    border-color: #43a047;
  }
  .navmain {
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    font-size: 20px;
    gap: 10px;
  }
  .obs {
    font-size: 32px;
    font-variant-numeric: tabular-nums;
  }
  .navbox p {
    margin: 4px 0 0;
    font-size: 13px;
    color: var(--muted);
  }
  .navbox p b {
    color: var(--fg);
  }
  .nextnav {
    font-size: 13px;
    color: var(--muted);
    padding: 0 4px;
  }
  .altchange {
    padding: 8px 10px;
    border-radius: 8px;
    border: 1px solid #e0a800;
    color: #ffca28;
    font-size: 16px;
  }
</style>
