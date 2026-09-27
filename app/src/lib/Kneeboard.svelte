<script lang="ts">
  import DestBox from "./DestBox.svelte";
  import { distanceNm, mmss, pointAlong, utc, type LatLon } from "./geo";
  import { app, landmarkLine } from "./state.svelte";
  import { formatRadial, navCourse, navUse, radialAt, type LegRadial } from "./vor";

  const r = $derived(app.doc.resolved);
  const ato = $derived(app.doc.ato);
  const k = $derived(app.activeLeg);
  const n = $derived(r ? r.waypoints.length : 0);
  const arrived = $derived(r != null && k === n - 1);
  /** The leg shown: the one being flown, or the first one before departure. */
  const li = $derived(k < 0 ? 0 : k);
  const leg = $derived(r && li < n - 1 ? r.legs[li] : null);
  const legStart = $derived(k >= 0 ? (ato[k] ?? null) : null);
  const elapsedS = $derived(legStart != null ? (app.now - legStart) / 1000 : 0);
  const remainingS = $derived(leg ? leg.ete_min * 60 - elapsedS : 0);

  const checkpoints = $derived(
    leg && legStart != null
      ? app.landmarksForLeg(k).map((lm) => ({ lm, due: legStart + (lm.along / leg.gs_kt) * 3_600_000 }))
      : [],
  );

  const pad3 = (n: number) => String(Math.round(n) % 360).padStart(3, "0");

  // NAV1/NAV2 for this leg and the next; this leg's also show what they should read now at the DR position.
  const navs = $derived(r && leg ? [app.legNav(li, 1), app.legNav(li, 2)] : []);
  const nextNavs = $derived(r && li + 1 < n - 1 ? [app.legNav(li + 1, 1), app.legNav(li + 1, 2)] : []);
  const dr = $derived(
    r && leg && legStart != null
      ? pointAlong(r.waypoints[k], r.waypoints[k + 1], Math.min((leg.gs_kt * elapsedS) / 3600, leg.distance_nm))
      : null,
  );
  const notes = $derived(app.doc.notes[li + 1]?.trim() ?? "");

  // Altitude change at the end of this leg.
  const altChange = $derived.by(() => {
    if (!r || li + 1 >= n - 1) return null;
    const now = app.legAlt(li);
    const next = app.legAlt(li + 1);
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

  /** " · now R245 · DME 12.3" at the DR position, or nothing before departure. */
  function nowLine(c: LegRadial, p: LatLon | null): string {
    if (!p) return "";
    const dme = c.vor.dme ? ` · DME ${distanceNm(c.vor, p).toFixed(1)}` : "";
    return ` · now ${formatRadial(radialAt(c.vor, p))}${dme}`;
  }
</script>

{#snippet navRow(slot: number, nav: { c: LegRadial; chosen: boolean } | null, p: LatLon | null, small: boolean)}
  {#if nav}
    {@const crs = navCourse(nav.c)}
    <div class="navrow" class:small class:trackable={nav.c.trackable}>
      <span class="navlbl">NAV{slot}</span>
      <span class="crs"><b>{pad3(crs.crs)}</b><small>{crs.flag}</small></span>
      <span class="station">{nav.c.vor.ident} <b>{nav.c.vor.freq}</b></span>
      <span class="use">{navUse(nav.c)}{nav.chosen ? "" : " · suggested"}{nowLine(nav.c, p)}</span>
    </div>
  {:else}
    <div class="navrow none" class:small><span class="navlbl">NAV{slot}</span><span class="station">—</span></div>
  {/if}
{/snippet}

{#snippet destination()}
  <button class="desttoggle" onclick={() => (destManual = !destOpen)}>
    {destOpen ? "▾" : "▸"} Destination {r ? r.waypoints[n - 1].ident : ""} · frequencies, ATIS, ILS
  </button>
  {#if destOpen}<DestBox />{/if}
{/snippet}

{#if !r}
  <p class="empty">Resolve a route in the planning view first.</p>
{:else if arrived}
  <div class="kb">
    <div class="box">
      <div class="to">Arrived {r.waypoints[n - 1].ident} <span class="clock">{utc(app.now)}</span></div>
      <p>Block time {mmss((ato[n - 1]! - ato[0]!) / 1000)} · planned {mmss(r.legs.reduce((s, l) => s + l.ete_min, 0) * 60)}</p>
    </div>
    {@render destination()}
  </div>
{:else if leg}
  <div class="kb">
    <!-- 1: what to fly -->
    <div class="primary-row">
      <div><span>MH</span><b>{pad3(leg.mh)}°</b></div>
      <div><span>ALT</span><b>{app.legAlt(li)}</b></div>
      <div><span>GS</span><b>{leg.gs_kt}</b></div>
    </div>

    <!-- 2: radios for this leg -->
    <div class="navs">
      {@render navRow(1, navs[0], dr, false)}
      {@render navRow(2, navs[1], dr, false)}
    </div>

    <!-- 3: notes for the waypoint ahead -->
    {#if notes}
      <div class="box notes"><h3>Notes · {leg.to}</h3><p>{notes}</p></div>
    {/if}

    <!-- 4: radios for the next leg, to set up before the turn -->
    {#if nextNavs.length}
      {@const nl = r.legs[li + 1]}
      <div class="navs next">
        <h3 class="nexthead">Next · {nl.from} → {nl.to}</h3>
        <div class="primary-row small">
          <div><span>MH</span><b>{pad3(nl.mh)}°</b></div>
          <div><span>ALT</span><b class:change={!!altChange}>{app.legAlt(li + 1)}</b></div>
          <div><span>GS</span><b>{nl.gs_kt}</b></div>
        </div>
        {@render navRow(1, nextNavs[0], null, true)}
        {@render navRow(2, nextNavs[1], null, true)}
      </div>
    {/if}

    <!-- 5: everything else -->
    <div class="box">
      <div class="to">
        {leg.from} → <b>{leg.to}</b>
        <small>leg {li + 1}/{n - 1} · {leg.distance_nm} NM · ETE {mmss(leg.ete_min * 60)}</small>
        <span class="clock">{utc(app.now)}</span>
      </div>
      {#if k >= 0}
        <div class="timer" class:late={remainingS < 0}>
          <div><span>{remainingS >= 0 ? "to " + leg.to : "overdue"}</span><b>{mmss(remainingS)}</b></div>
          <div><span>ETO</span><b>{utc(legStart! + leg.ete_min * 60000, false)}</b></div>
          <div><span>elapsed</span><b>{mmss(elapsedS)}</b></div>
        </div>
      {/if}
    </div>

    {#if k < 0}
      <button class="over primary" onclick={() => app.markOver()}>Start · over {r.waypoints[0].ident} now</button>
    {:else}
      <div class="actions">
        <button class="over primary" onclick={() => app.markOver()}>Over {leg.to} now</button>
        <button onclick={() => app.undoOver()} title="Undo the last time over">Undo</button>
      </div>
    {/if}

    {#if altChange}
      <div class="altchange">At {leg.to}: {altChange.climb ? "climb" : "descend"} to <b>{altChange.to} ft</b></div>
    {/if}

    {#if nearDest}{@render destination()}{/if}

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

    {#if r.waypoints[li + 1].fixes.length}
      <div class="box">
        <h3>VOR fixes · {leg.to}</h3>
        <p class="mono">{r.waypoints[li + 1].fixes.join("   ·   ")}</p>
      </div>
    {/if}

    {#if k >= 0}
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

    {#if !nearDest}{@render destination()}{/if}
  </div>
{/if}

<style>
  .kb {
    padding: 12px;
    display: grid;
    gap: 10px;
  }
  .clock {
    font: 600 14px ui-monospace, Consolas, monospace;
    color: var(--muted);
    margin-left: auto;
  }
  .primary-row {
    display: grid;
    grid-template-columns: 1.3fr 1.1fr 0.8fr;
    gap: 8px;
    background: var(--card);
    border: 1px solid var(--line);
    border-radius: 10px;
    padding: 8px 12px;
  }
  .primary-row span,
  .timer span {
    display: block;
    font-size: 12px;
    color: var(--muted);
    text-transform: uppercase;
  }
  .primary-row b {
    font: 700 60px/1.05 ui-monospace, Consolas, monospace;
  }
  .primary-row.small {
    padding: 4px 12px;
  }
  .primary-row.small b {
    font-size: 40px;
  }
  .primary-row b.change {
    color: #ffca28;
  }
  .nexthead {
    margin: 0;
    color: #ffca28;
    font-size: 13px;
  }
  .navs {
    display: grid;
    gap: 6px;
  }
  .navrow {
    display: grid;
    grid-template-columns: 44px auto 1fr;
    column-gap: 12px;
    align-items: baseline;
    background: var(--card);
    border: 1px solid var(--line);
    border-left: 4px solid var(--line);
    border-radius: 8px;
    padding: 4px 10px;
  }
  .navrow.trackable {
    border-left-color: #43a047;
  }
  .navlbl {
    font-size: 13px;
    font-weight: 700;
    color: var(--muted);
  }
  .crs b {
    font: 700 44px/1.05 ui-monospace, Consolas, monospace;
  }
  .crs small {
    font-size: 15px;
    font-weight: 700;
    margin-left: 4px;
    color: var(--muted);
  }
  .station {
    font-size: 20px;
  }
  .station b {
    font-variant-numeric: tabular-nums;
  }
  .use {
    grid-column: 2 / -1;
    font-size: 12px;
    color: var(--muted);
  }
  .navrow.small .crs b {
    font-size: 28px;
  }
  .navrow.small .station {
    font-size: 16px;
  }
  .navrow.none {
    opacity: 0.6;
  }
  .navs.next {
    padding: 6px 8px;
    border: 1px dashed var(--line);
    border-radius: 10px;
  }
  .to {
    font-size: 18px;
    display: flex;
    align-items: baseline;
    gap: 6px;
    flex-wrap: wrap;
  }
  .to small {
    color: var(--muted);
    font-size: 13px;
  }
  .timer {
    display: grid;
    grid-template-columns: 1.4fr 1fr 1fr;
    gap: 8px;
    margin-top: 8px;
    padding-top: 8px;
    border-top: 1px solid var(--line);
  }
  .timer b {
    font: 700 26px ui-monospace, Consolas, monospace;
  }
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
    margin: 0;
    font-size: 17px;
  }
  .mono {
    font-family: ui-monospace, Consolas, monospace;
    font-size: 12.5px;
    margin: 0;
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
  .empty {
    padding: 16px;
    color: var(--muted);
  }
  .desttoggle {
    text-align: left;
    font-weight: 600;
  }
  .altchange {
    padding: 8px 10px;
    border-radius: 8px;
    border: 1px solid #e0a800;
    color: #ffca28;
    font-size: 16px;
  }
</style>
