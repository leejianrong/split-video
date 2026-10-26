<script lang="ts">
  import { thumbs } from '../state/thumbs.svelte';
  import { filmstripTiles, frameUrl } from './thumbs';

  // The thumbnails behind a main-track clip: only the tiles for the part of the clip that is on screen, each a frame
  // from `GET /assets/{id}/frame` (the browser keeps the ones already seen). A tile that has not arrived is simply not
  // drawn: the clip's own colour shows through, so the placeholder is the clip.
  let {
    clipId,
    assetId,
    clipIn,
    clipOut,
    pxPerSecond,
    visibleFrom,
    visibleTo,
    height,
  }: {
    clipId: string;
    assetId: string;
    clipIn: number;
    clipOut: number;
    pxPerSecond: number;
    /** The on-screen part of the clip, in source seconds. */
    visibleFrom: number;
    visibleTo: number;
    height: number;
  } = $props();

  const tileWidth = $derived(Math.ceil((height * 16) / 9));
  const tiles = $derived(
    assetId
      ? filmstripTiles({ clipIn, clipOut, pxPerSecond, tileWidth, visibleFrom, visibleTo }).map(
          (tile) => ({ ...tile, url: frameUrl(assetId, tile.time) }),
        )
      : [],
  );

  $effect(() => {
    thumbs.want(
      clipId,
      tiles.map((t) => t.url),
    );
    return () => thumbs.drop(clipId);
  });
</script>

<div class="strip" aria-hidden="true" data-testid="filmstrip">
  {#each tiles as tile (tile.index)}
    {#if thumbs.isLoaded(tile.url)}
      <img
        src={tile.url}
        alt=""
        draggable="false"
        style:left="{tile.left}px"
        style:width="{tile.width}px"
        data-testid="thumb"
      />
    {/if}
  {/each}
</div>

<style>
  .strip {
    position: absolute;
    inset: 0;
    overflow: hidden;
    pointer-events: none;
  }

  img {
    position: absolute;
    top: 0;
    height: 100%;
    object-fit: cover;
    opacity: 0.85;
  }
</style>
