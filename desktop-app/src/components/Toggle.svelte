<script lang="ts">
  let { checked, label, onchange, disabled = false }: { checked: boolean; label: string; onchange: (checked: boolean) => void; disabled?: boolean } = $props();
</script>
<button class="toggle" class:checked role="switch" aria-label={label} aria-checked={checked} {disabled} onclick={() => onchange(!checked)}><span></span></button>
