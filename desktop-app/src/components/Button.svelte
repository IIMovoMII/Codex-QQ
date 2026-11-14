<script lang="ts">
  import type { Snippet } from 'svelte';
  import type { HTMLButtonAttributes } from 'svelte/elements';
  import Icon from './Icon.svelte';
  let { children, variant = 'secondary', icon, loading = false, class: className = '', ...rest }: HTMLButtonAttributes & { children?: Snippet; variant?: 'primary' | 'secondary' | 'ghost' | 'danger'; icon?: string; loading?: boolean } = $props();
</script>
<button {...rest} class="button {variant} {className}" disabled={rest.disabled || loading} aria-busy={loading}>
  {#if loading}<Icon name="loader" class="spin" size={18} />{:else if icon}<Icon name={icon} size={18} />{/if}
  {#if children}{@render children()}{/if}
</button>
