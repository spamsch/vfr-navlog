<script lang="ts">
  import { app } from "./state.svelte";

  const REFRESH_MS = 3 * 60_000;

  const r = $derived(app.doc.resolved);
  const dest = $derived(r ? r.waypoints[r.waypoints.length - 1] : null);
  const live = $derived(app.field && dest && app.field.icao === dest.ident.toUpperCase() ? app.field : null);

  // Live VATSIM station where online (bold), else the published frequency from apt.dat.
  const ROLES: [string, string][] = [
    ["atis", "ATIS"],
    ["delivery", "Delivery"],
    ["ground", "Ground"],
    ["tower", "Tower"],
    ["approach", "Approach"],
  ];
  const rows = $derived(
    ROLES.map(([role, label]) => {
      const onl = live?.freqs[role];
      return { label, freq: onl ?? r?.dest_freqs[role] ?? "", live: !!onl };
    }).filter((row) => row.freq),
  );
  const atcOnline = $derived(!!live && ["tower", "approach", "ground"].some((k) => live.freqs[k]));
  const showTaf = $derived(!!live?.taf && live.taf.trim() !== (live.metar ?? "").trim());

  // Fetch on open, then refresh while the box stays open.
  $effect(() => {
    if (!dest) return;
    app.fetchField();
    const t = setInterval(() => app.fetchField(), REFRESH_MS);
    return () => clearInterval(t);
  });
</script>

{#if r && dest}
  <div class="dest">
    <div class="head">
      <h3>{dest.ident}{r.dest_info?.name ? ` · ${r.dest_info.name}` : ""}</h3>
      <span class="meta">
        {#if r.dest_info}Elev {r.dest_info.elevation_ft} ft{r.dest_info.transition_alt ? ` · TA ${r.dest_info.transition_alt}` : ""}{/if}
      </span>
      <button class="small" onclick={() => app.fetchField()} disabled={app.fieldBusy}>
        {app.fieldBusy ? "…" : "↻"}
        {live ? live.fetched_at : "load"}
      </button>
    </div>

    <table class="freqs">
      <tbody>
        {#each rows as row (row.label)}
          <tr class:live={row.live}><td>{row.label}</td><td>{row.freq}</td><td>{row.live ? "VATSIM" : ""}</td></tr>
        {/each}
        {#if live?.radar}
          <tr class="live"><td>{live.radar.name}</td><td>{live.radar.freq}</td><td>VATSIM</td></tr>
        {/if}
        {#if live && live.vatsim_ok && !atcOnline}
          <tr><td>UNICOM</td><td>122.800</td><td>no ATC online</td></tr>
        {/if}
      </tbody>
    </table>

    {#if r.dest_ils.length}
      <div class="ils">
        {#each r.dest_ils as ils (ils.runway)}
          <span><b>ILS {ils.runway}</b> {ils.ident} {ils.freq_mhz.toFixed(2)}</span>
        {/each}
      </div>
    {/if}
    {#if r.dest_info?.runways.length}
      <div class="rwys">{r.dest_info.runways.join("   ·   ")}</div>
    {/if}

    {#if live?.atis.length}
      <div class="atis"><span class="tag">ATIS</span>{live.atis.join(" ")}</div>
    {/if}
    {#if live?.metar}
      <div class="wx">
        <span class="tag">METAR</span>{live.metar}
        {#if live.qnh_hpa}<b class="qnh">QNH {live.qnh_hpa}</b>{/if}
        {#if live.vfr_status}<b class="cat" class:bad={live.vfr_status !== "VFR"}>{live.vfr_status}</b>{/if}
      </div>
    {/if}
    {#if showTaf}
      <div class="wx"><span class="tag">TAF</span>{live?.taf}</div>
    {/if}
    {#if app.fieldError}<div class="err">{app.fieldError}</div>{/if}
    {#if live && !live.vatsim_ok}<div class="err">VATSIM feed unreachable — published frequencies shown.</div>{/if}
  </div>
{/if}

<style>
  .dest {
    border: 1px solid var(--accent);
    border-radius: 8px;
    padding: 8px 10px;
    background: var(--card);
    display: grid;
    gap: 6px;
  }
  .head {
    display: flex;
    align-items: baseline;
    gap: 8px;
  }
  h3 {
    margin: 0;
    font-size: 15px;
  }
  .meta {
    color: var(--muted);
    font-size: 12px;
    flex: 1;
  }
  .small {
    padding: 1px 8px;
    font-size: 12px;
  }
  .freqs {
    border-collapse: collapse;
    font: 15px ui-monospace, Consolas, monospace;
  }
  .freqs td {
    padding: 1px 14px 1px 0;
  }
  .freqs td:last-child {
    font: 11px system-ui, sans-serif;
    color: var(--muted);
  }
  .freqs tr.live td:nth-child(2) {
    font-weight: 700;
    color: var(--accent);
  }
  .ils {
    display: flex;
    flex-wrap: wrap;
    gap: 4px 16px;
    font: 15px ui-monospace, Consolas, monospace;
  }
  .rwys {
    font-size: 12px;
    color: var(--muted);
  }
  .atis,
  .wx {
    font: 13px/1.45 ui-monospace, Consolas, monospace;
  }
  .tag {
    display: inline-block;
    font: 700 10px system-ui, sans-serif;
    color: var(--muted);
    margin-right: 6px;
  }
  .qnh,
  .cat {
    margin-left: 8px;
    font-family: system-ui, sans-serif;
  }
  .cat {
    color: #43a047;
  }
  .cat.bad {
    color: var(--danger);
  }
  .err {
    font-size: 12px;
    color: var(--danger);
  }
</style>
