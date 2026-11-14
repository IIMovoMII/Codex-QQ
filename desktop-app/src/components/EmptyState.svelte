<script lang="ts">
  import type { Snippet } from 'svelte';
  import Icon from './Icon.svelte';
  let { icon = 'bell', title, description = '', children }: { icon?: string; title: string; description?: string; children?: Snippet } = $props();
</script>
<div class="empty-state"><span class="empty-icon"><Icon name={icon} size={30} /></span><h3>{title}</h3>{#if description}<p>{description}</p>{/if}{#if children}{@render children()}{/if}</div>
