<script lang="ts">
  import type { Snippet } from 'svelte';
  import type { Tone } from '../bridge/contracts';
  let { tone = 'neutral', children }: { tone?: Tone; children: Snippet } = $props();
</script>
<span class="pill {tone}">{@render children()}</span>
