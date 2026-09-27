<script lang="ts">
  import { app, CHART_BASE } from "./state.svelte";

  const ZOOMS = [1, 1.5, 2, 3, 4];
  let zoom = $state(0);
  let scroller: HTMLDivElement | undefined = $state();

  const view = $derived(app.chartView);
  const set = $derived(view ? app.chartsFor(view.icao) : null);
  const loading = $derived(view ? app.charts[view.icao] === "loading" : false);
  const page = $derived(set && view ? set.pages[Math.min(view.page, set.pages.length - 1)] : null);

  function go(delta: number) {
    if (!view || !set?.pages.length) return;
    const n = set.pages.length;
    app.chartView = { icao: view.icao, page: (((view.page + delta) % n) + n) % n };
    zoom = 0;
    scroller?.scrollTo(0, 0);
  }

  function onKey(e: KeyboardEvent) {
    if (!view) return;
    if (e.key === "Escape") app.chartView = null;
    else if (e.key === "ArrowRight") go(1);
    else if (e.key === "ArrowLeft") go(-1);
    else if (e.key === "+" || e.key === "=") zoom = Math.min(ZOOMS.length - 1, zoom + 1);
    else if (e.key === "-") zoom = Math.max(0, zoom - 1);
    else return;
    e.preventDefault();
  }

  // Drag to pan a zoomed chart.
  let drag: { x: number; y: number; left: number; top: number } | null = null;
  function down(e: PointerEvent) {
    if (!scroller) return;
    drag = { x: e.clientX, y: e.clientY, left: scroller.scrollLeft, top: scroller.scrollTop };
    scroller.setPointerCapture(e.pointerId);
  }
  function move(e: PointerEvent) {
    if (!drag || !scroller) return;
    scroller.scrollLeft = drag.left - (e.clientX - drag.x);
    scroller.scrollTop = drag.top - (e.clientY - drag.y);
  }
</script>

<svelte:window onkeydown={onKey} />

{#if view}
  <div class="overlay">
    <header>
      <div class="tabs">
        {#each app.chartIcaos() as icao (icao)}
          <button class:on={icao === view.icao} onclick={() => { app.chartView = { icao, page: 0 }; zoom = 0; }}>{icao}</button>
        {/each}
      </div>
      {#if page && set}
        <button onclick={() => go(-1)} title="Previous page (←)">‹</button>
        <span class="count">{view.page + 1}/{set.pages.length}</span>
        <button onclick={() => go(1)} title="Next page (→)">›</button>
        <span class="title">{page.title}</span>
        <button onclick={() => (zoom = Math.max(0, zoom - 1))} disabled={zoom === 0} title="Zoom out (−)">−</button>
        <span class="count">{ZOOMS[zoom]}×</span>
        <button onclick={() => (zoom = Math.min(ZOOMS.length - 1, zoom + 1))} disabled={zoom === ZOOMS.length - 1} title="Zoom in (+)">+</button>
      {:else}
        <span class="title"></span>
      {/if}
      <button class="close" onclick={() => (app.chartView = null)} title="Close (Esc)">✕</button>
    </header>

    <div class="body">
      {#if set && set.pages.length}
        <nav>
          {#each set.pages as p, i (p.file)}
            <button class:on={i === view.page} onclick={() => { app.chartView = { icao: view.icao, page: i }; zoom = 0; }}>{p.title}</button>
          {/each}
          <p class="note">© DFS — AIP charts for personal flight preparation only.</p>
        </nav>
      {/if}
      <!-- svelte-ignore a11y_no_static_element_interactions -->
      <div class="scroller" bind:this={scroller} onpointerdown={down} onpointermove={move} onpointerup={() => (drag = null)}>
        {#if loading}
          <p class="msg">Downloading charts for {view.icao}…</p>
        {:else if page}
          <img src={`${CHART_BASE}/${view.icao}/${page.file}`} alt={page.title} style:width={`${ZOOMS[zoom] * 100}%`} draggable="false" />
        {:else}
          <p class="msg">
            {set?.error ?? `No charts for ${view.icao}.`}
            <button onclick={() => app.fetchCharts(view.icao, true)}>Try again</button>
          </p>
        {/if}
      </div>
    </div>
  </div>
{/if}

<style>
  .overlay {
    position: fixed;
    inset: 0;
    z-index: 3000;
    display: flex;
    flex-direction: column;
    background: var(--bg);
    color: var(--fg);
  }
  header {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 6px 12px;
    border-bottom: 1px solid var(--line);
    background: var(--card);
  }
  .tabs {
    display: flex;
    gap: 4px;
    margin-right: 8px;
  }
  .tabs button.on,
  nav button.on {
    background: var(--accent);
    border-color: var(--accent);
    color: #fff;
  }
  .title {
    flex: 1;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    font-weight: 600;
  }
  .count {
    font-variant-numeric: tabular-nums;
    color: var(--muted);
  }
  .body {
    flex: 1;
    display: flex;
    min-height: 0;
  }
  nav {
    width: 240px;
    flex: none;
    overflow-y: auto;
    padding: 8px;
    display: grid;
    gap: 4px;
    align-content: start;
    border-right: 1px solid var(--line);
  }
  nav button {
    text-align: left;
    font-size: 12.5px;
  }
  .note {
    font-size: 11px;
    color: var(--muted);
  }
  .scroller {
    flex: 1;
    overflow: auto;
    background: #888;
    cursor: grab;
    touch-action: none;
  }
  .scroller:active {
    cursor: grabbing;
  }
  img {
    display: block;
    margin: 0 auto;
    background: #fff;
    user-select: none;
  }
  .msg {
    padding: 20px;
    color: #fff;
  }
</style>
