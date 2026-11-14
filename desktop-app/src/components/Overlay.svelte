<script lang="ts">
  import { onMount } from 'svelte';
  import type { Snippet } from 'svelte';
  import Icon from './Icon.svelte';
  let { title, children, footer, onclose, drawer = false, busy = false, wide = false }: { title: string; children: Snippet; footer?: Snippet; onclose: () => void; drawer?: boolean; busy?: boolean; wide?: boolean } = $props();
  let dialog: HTMLDialogElement;
  onMount(() => {
    const previous = document.activeElement as HTMLElement;
    dialog.showModal();
    // Dialog close/cancel is the initial action; a destructive primary action
    // must never be triggered by an immediate Enter after opening.
    dialog.querySelector<HTMLButtonElement>('.overlay-footer .button:not(.primary), .overlay-header button')?.focus();
    return () => { if (previous?.isConnected) previous.focus(); };
  });
  function backdrop(event: MouseEvent) {
    if (event.target !== dialog || busy) return;
    const bounds = dialog.getBoundingClientRect();
    if (event.clientX < bounds.left || event.clientX > bounds.right || event.clientY < bounds.top || event.clientY > bounds.bottom) onclose();
  }
</script>
<dialog bind:this={dialog} class="overlay glass" class:drawer class:wide aria-label={title} oncancel={(event) => { event.preventDefault(); if (!busy) onclose(); }} onclick={backdrop}>
  <header class="overlay-header"><h2>{title}</h2><button class="icon-button" aria-label="关闭{title}" disabled={busy} onclick={onclose}><Icon name="close" /></button></header>
  <div class="overlay-body">{@render children()}</div>
  {#if footer}<footer class="overlay-footer">{@render footer()}</footer>{/if}
</dialog>
