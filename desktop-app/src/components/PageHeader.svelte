<script lang="ts">
  import type { Snippet } from 'svelte';
  let { title, subtitle = '', children }: { title: string; subtitle?: string; children?: Snippet } = $props();
</script>
<header class="page-header">
  <div><h1 tabindex="-1">{title}</h1>{#if subtitle}<p>{subtitle}</p>{/if}</div>
  {#if children}<div class="page-actions">{@render children()}</div>{/if}
</header>
