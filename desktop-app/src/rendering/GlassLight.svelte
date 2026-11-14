<script lang="ts">
  import { app } from '../app/state.svelte';
  import { glassRenderer } from './glass';
  let canvas: HTMLCanvasElement;
  $effect(() => {
    const preferences = app.preferences;
    if (!canvas || !preferences.transparency || !preferences.motion || preferences.quality === 'light') return;
    if (matchMedia('(prefers-reduced-motion: reduce)').matches || matchMedia('(forced-colors: active)').matches) return;
    const dark = preferences.theme === 'dark' || preferences.theme === 'system' && matchMedia('(prefers-color-scheme: dark)').matches;
    return glassRenderer(canvas, dark);
  });
</script>
<canvas bind:this={canvas} class="glass-light" aria-hidden="true"></canvas>
