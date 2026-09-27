<script lang="ts">
  import { tick } from "svelte";

  import { app, landmarkLine } from "./state.svelte";
  import { analyseLeg, formatRadial, navLine } from "./vor";

  const r = $derived(app.doc.resolved);
  const selVor = $derived(r?.vors?.find((v) => v.ident === app.selectedVor) ?? null);
  const pad3 = (n: number) => String(n).padStart(3, "0");

  const AUTO = "__auto";
  function onNavPick(k: number, value: string) {
    app.setLegVor(k, value === AUTO ? null : value);
  }
  function onAlt(k: number, value: string) {
    const v = Number(value);
    app.setLegAlt(k, value.trim() && v > 0 ? Math.round(v) : null);
  }

  // Keep the selected waypoint card in view (map clicks select waypoints too).
  let cards: HTMLElement[] = $state([]);
  $effect(() => {
    const i = app.selectedWp;
    tick().then(() => cards[i]?.scrollIntoView({ block: "nearest", behavior: "smooth" }));
  });

  function focusWhen(node: HTMLInputElement, active: boolean) {
    if (active) node.focus();
    return {
      update(next: boolean) {
        if (next) node.focus();
      },
    };
  }

  function onRouteKey(e: KeyboardEvent) {
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) app.resolve();
  }
</script>

<section class="setup">
  <label class="route">
    Route <span class="hint">(paste from Navigraph · Ctrl+Enter to resolve)</span>
    <textarea
      rows="3"
      bind:value={app.doc.route}
      onkeydown={onRouteKey}
      placeholder="EDDG DCT 520230N0074048E DCT HMM DCT … DCT EDDK"
      spellcheck="false"
    ></textarea>
  </label>
  <div class="row">
    <label>
      Aircraft
      <select bind:value={app.doc.aircraft}>
        {#each app.aircraftList as a (a.file)}
          <option value={a.file}>{a.type} · {a.registration} · {a.tas_kt} kt</option>
        {/each}
      </select>
    </label>
    <label class="short">Cruise ft <input bind:value={app.doc.cruiseAlt} placeholder="3500" inputmode="numeric" /></label>
    <label class="short">Wind <input bind:value={app.doc.wind} placeholder="270/15" /></label>
    <label class="short">Var <input bind:value={app.doc.magvar} placeholder="4E" /></label>
  </div>
  <div class="row actions">
    <button class="primary" onclick={() => app.resolve()} disabled={!!app.busy || !app.doc.route.trim()}>Resolve route</button>
    <label class="check" title="Raise legs you have not edited to the VFR semicircular rule">
      <input type="checkbox" bind:checked={app.doc.hemispheric} /> Semicircular rule
    </label>
    <label class="check"><input type="checkbox" bind:checked={app.doc.pdf.vatsim} /> VATSIM/weather</label>
    <label class="check"><input type="checkbox" bind:checked={app.doc.pdf.phraseology} /> Sprechgruppen</label>
    <label class="check"><input type="checkbox" bind:checked={app.doc.pdf.wpMaps} /> Waypoint maps</label>
  </div>
  {#if r}
    <div class="summary">
      {r.aircraft.type} · wind {String(r.wind[0]).padStart(3, "0")}/{r.wind[1]} · var {r.magvar > 0 ? "+" : ""}{r.magvar}°
      · {r.legs.reduce((s, l) => s + l.distance_nm, 0).toFixed(1)} NM
      · {Math.round(r.legs.reduce((s, l) => s + l.ete_min, 0))} min
      · {r.legs.reduce((s, l) => s + l.fuel_l, 0).toFixed(1)} L
    </div>
  {/if}
</section>

{#if r}
  <section class="chartsec">
    <span class="lbl">Airport charts</span>
    {#each app.chartIcaos() as icao (icao)}
      {@const c = app.charts[icao]}
      {#if c === "loading"}
        <span class="chip muted">{icao} · downloading…</span>
      {:else if c && c.pages.length}
        <button class="chip" onclick={() => app.openCharts(icao)} title={c.error ?? ""}>{icao} · {c.pages.length} pages</button>
      {:else if c}
        <span class="chip muted" title={c.error ?? ""}>{icao} · none</span>
        {#if c.error && !c.error.includes("German")}
          <button class="small" onclick={() => app.fetchCharts(icao, true)} title={c.error}>↻</button>
        {/if}
      {/if}
    {/each}
  </section>
{/if}

{#if r?.vors?.length}
  <section class="vorsec">
    <label>
      VOR radials
      <select bind:value={app.selectedVor}>
        <option value={null}>— pick a VOR to see which legs you can fly on its radials —</option>
        {#each r.vors as v (v.ident + v.freq)}
          <option value={v.ident}>{v.ident} {v.freq} · {v.name} · {v.route_dist_nm} NM from route{v.dme ? " · DME" : ""}</option>
        {/each}
      </select>
    </label>
    {#if selVor}
      <table>
        <thead><tr><th>Leg</th><th>Radials</th><th>OBS</th><th>Needle</th><th></th></tr></thead>
        <tbody>
          {#each r.legs as leg, k (k)}
            {@const c = analyseLeg(selVor, r.waypoints[k], r.waypoints[k + 1])}
            <tr class:good={c.trackable} class:far={!c.inRange}>
              <td>{leg.from}→{leg.to}</td>
              <td>{formatRadial(c.rStart)}→{formatRadial(c.rEnd)}</td>
              <td>{#if c.trackable}<b>{pad3(c.obs)} {c.flag}</b>{:else}—{/if}</td>
              <td>{c.inRange ? `${Math.round(c.maxDev)}°` : "out of range"}</td>
              <td>
                {#if app.doc.legVor[k] === selVor.ident}
                  <button class="small on" onclick={() => app.setLegVor(k, null)}>used</button>
                {:else}
                  <button class="small" disabled={!c.inRange} onclick={() => app.setLegVor(k, selVor.ident)}>use</button>
                {/if}
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
      <p class="hint">
        OBS = leg course on this station. <b>Green</b>: the needle stays within half scale (5°) for the whole leg, so fly it on the radial.
        Other legs: the radial at the start and end is a progress check.
      </p>
    {/if}
  </section>
{/if}

{#if r}
  <section class="waypoints">
    {#each r.waypoints as wp, i (i)}
      {@const leg = i > 0 ? r.legs[i - 1] : null}
      <!-- svelte-ignore a11y_click_events_have_key_events, a11y_no_static_element_interactions -->
      <div class="card" class:selected={app.selectedWp === i} bind:this={cards[i]} onclick={() => (app.selectedWp = i)}>
        <div class="head">
          <span class="idx">{i}</span>
          <span class="ident">{wp.ident}</span>
          {#if wp.name}<span class="name">{wp.name}</span>{/if}
          {#if leg}
            <span class="legdata">
              <b>MH {String(leg.mh).padStart(3, "0")}°</b> · {leg.distance_nm} NM · GS {leg.gs_kt} · <b>{Math.round(leg.ete_min)} min</b>
            </span>
          {:else}
            <span class="legdata">departure</span>
          {/if}
        </div>
        {#if leg}
          {@const k = i - 1}
          {@const advice = app.altAdvice(k)}
          {@const nav = app.legNav(k)}
          <div class="legrow">
            <label class="alt">
              Alt
              <input
                type="number"
                step="100"
                value={app.doc.legAlts[k] ?? ""}
                placeholder={String(app.legAlt(k))}
                onchange={(e) => onAlt(k, e.currentTarget.value)}
              />
              ft
            </label>
            {#if app.doc.legAlts[k] != null}
              <button class="small" title="Back to cruise altitude" onclick={() => app.setLegAlt(k, null)}>↺</button>
            {/if}
            {#if advice}
              <button class="small warn" title="VFR semicircular rule for MH {pad3(leg.mh)}°" onclick={() => app.setLegAlt(k, advice)}>
                ⚠ rule: {advice}
              </button>
            {/if}
            <select class="nav" value={app.doc.legVor[k] ?? AUTO} onchange={(e) => onNavPick(k, e.currentTarget.value)}>
              <option value={AUTO}>VOR: auto (best radial)</option>
              <option value="">VOR: none</option>
              {#each app.legCandidates[k] ?? [] as c (c.vor.ident + c.vor.freq)}
                <option value={c.vor.ident}>
                  {c.vor.ident} {c.vor.freq} · {c.trackable ? `OBS ${pad3(c.obs)} ${c.flag} ≤${Math.max(1, Math.round(c.maxDev))}°` : `${formatRadial(c.rStart)}→${formatRadial(c.rEnd)}`}
                </option>
              {/each}
            </select>
          </div>
          {#if nav}
            <div class="navline" class:trackable={nav.c.trackable}>
              {navLine(nav.c)}{nav.chosen ? "" : "  (suggested)"}
            </div>
          {/if}
        {/if}
        {#if wp.fixes.length}
          <div class="fixes">{wp.fixes.join("   ·   ")}</div>
        {/if}
        {#if i === r.waypoints.length - 1 && r.dest_ils.length}
          <div class="ils">
            {#each r.dest_ils as ils (ils.runway)}<span>ILS {ils.runway} {ils.ident} {ils.freq_mhz.toFixed(2)}</span>{/each}
          </div>
        {/if}
        {#if i === r.call_leg_idx}
          <div class="call">→ call {r.waypoints[r.waypoints.length - 1].ident} tower from here</div>
        {/if}
        <textarea
          rows="2"
          bind:value={app.doc.notes[i]}
          placeholder={i === 0 ? "Departure notes (runway, departure route, first heading…)" : `What you see on the way to ${wp.ident}: landmarks, catch features, airspace…`}
        ></textarea>
        {#if i > 0}
          {#each app.landmarksForLeg(i - 1) as lm (lm.id)}
            <div class="landmark" class:selected={app.selectedLandmark === lm.id}>
              <span class="pin"></span>
              <input
                bind:value={() => lm.label, (v) => (lm.label = v)}
                placeholder="Landmark name (e.g. Autobahn A1 crossing)"
                use:focusWhen={app.selectedLandmark === lm.id && !lm.label}
                onfocus={() => (app.selectedLandmark = lm.id)}
              />
              <span class="lmdata">{landmarkLine(lm, r).replace(lm.label, "").trim()}</span>
              <button class="icon" title="Remove landmark" onclick={() => app.removeLandmark(lm.id)}>✕</button>
            </div>
          {/each}
        {/if}
      </div>
    {/each}
  </section>
{:else}
  <p class="empty">Paste a route and press <b>Resolve route</b>. Then drop landmark pins on the map and write notes per waypoint.</p>
{/if}

<style>
  .setup {
    padding: 10px 12px;
    border-bottom: 1px solid var(--line);
    display: grid;
    gap: 8px;
  }
  label {
    display: grid;
    gap: 3px;
    font-size: 12px;
    color: var(--muted);
  }
  .hint {
    font-weight: 400;
  }
  .route textarea {
    font-family: ui-monospace, Consolas, monospace;
  }
  .row {
    display: flex;
    gap: 8px;
    align-items: end;
    flex-wrap: wrap;
  }
  .row label:first-child {
    flex: 1;
    min-width: 170px;
  }
  .short input {
    width: 70px;
  }
  .actions {
    align-items: center;
  }
  .check {
    display: flex;
    align-items: center;
    gap: 4px;
    color: var(--fg);
  }
  .summary {
    font-size: 12px;
    color: var(--muted);
  }
  .waypoints {
    padding: 8px 12px 40px;
    display: grid;
    gap: 8px;
  }
  .card {
    border: 1px solid var(--line);
    border-radius: 8px;
    padding: 8px 10px;
    background: var(--card);
    display: grid;
    gap: 6px;
  }
  .card.selected {
    border-color: var(--accent);
    box-shadow: 0 0 0 1px var(--accent);
  }
  .head {
    display: flex;
    align-items: baseline;
    gap: 8px;
    flex-wrap: wrap;
  }
  .idx {
    font-size: 11px;
    color: var(--muted);
    min-width: 14px;
  }
  .ident {
    font-weight: 700;
    font-size: 15px;
  }
  .name {
    color: var(--muted);
    font-size: 12px;
  }
  .legdata {
    margin-left: auto;
    font-size: 12px;
  }
  .fixes,
  .ils {
    font-size: 11.5px;
    font-family: ui-monospace, Consolas, monospace;
    color: var(--muted);
  }
  .ils {
    display: flex;
    gap: 12px;
    flex-wrap: wrap;
    color: var(--fg);
    font-weight: 600;
  }
  .call {
    font-size: 12px;
    font-weight: 700;
    color: var(--danger);
  }
  textarea {
    width: 100%;
    resize: vertical;
  }
  .landmark {
    display: flex;
    align-items: center;
    gap: 6px;
    font-size: 12px;
  }
  .landmark input {
    flex: 1;
    min-width: 120px;
  }
  .landmark.selected input {
    border-color: var(--accent);
  }
  .lmdata {
    color: var(--muted);
    white-space: nowrap;
  }
  .pin {
    width: 9px;
    height: 9px;
    background: #1565c0;
    transform: rotate(45deg);
    flex: none;
  }
  .icon {
    padding: 2px 6px;
  }
  .empty {
    padding: 16px 12px;
    color: var(--muted);
  }
  .chartsec {
    padding: 6px 12px;
    border-bottom: 1px solid var(--line);
    display: flex;
    align-items: center;
    gap: 6px;
    flex-wrap: wrap;
    font-size: 12px;
  }
  .chartsec .lbl {
    color: var(--muted);
    margin-right: 4px;
  }
  .chip {
    padding: 2px 8px;
    font-size: 12px;
    border-radius: 12px;
  }
  .chip.muted {
    color: var(--muted);
    border: 1px dashed var(--line);
  }
  .vorsec {
    padding: 8px 12px;
    border-bottom: 1px solid var(--line);
    display: grid;
    gap: 6px;
  }
  .vorsec table {
    width: 100%;
    border-collapse: collapse;
    font: 12px ui-monospace, Consolas, monospace;
  }
  .vorsec th {
    text-align: left;
    color: var(--muted);
    font-weight: 500;
  }
  .vorsec td {
    padding: 2px 4px 2px 0;
  }
  .vorsec tr.good td {
    background: color-mix(in srgb, #2e7d32 16%, transparent);
  }
  .vorsec tr.far td {
    color: var(--muted);
  }
  .vorsec .hint {
    margin: 0;
    font-size: 11.5px;
    color: var(--muted);
  }
  .small {
    padding: 1px 7px;
    font-size: 12px;
  }
  .small.on {
    background: #2e7d32;
    border-color: #2e7d32;
    color: #fff;
  }
  .legrow {
    display: flex;
    align-items: center;
    gap: 6px;
    flex-wrap: wrap;
  }
  .alt {
    display: flex;
    align-items: center;
    gap: 4px;
    color: var(--fg);
  }
  .alt input {
    width: 76px;
  }
  .warn {
    color: #b26a00;
    border-color: #e0a800;
  }
  .nav {
    margin-left: auto;
    max-width: 230px;
    font-size: 12px;
  }
  .navline {
    font: 12px ui-monospace, Consolas, monospace;
    color: var(--muted);
  }
  .navline.trackable {
    color: #2e7d32;
    font-weight: 700;
  }
</style>
