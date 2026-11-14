<script lang="ts">
  let { options, value, onchange, label = '选择', class: className = '' }: { options: { value: string; label: string; count?: number }[]; value: string; onchange: (value: string) => void; label?: string; class?: string } = $props();
  function move(event: KeyboardEvent) {
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
    event.preventDefault();
    const current = options.findIndex(option => option.value === value);
    const next = event.key === 'Home' ? 0 : event.key === 'End' ? options.length - 1 : (current + (event.key === 'ArrowRight' ? 1 : -1) + options.length) % options.length;
    onchange(options[next].value);
    const buttons = (event.currentTarget as HTMLElement).parentElement?.querySelectorAll<HTMLButtonElement>('button');
    buttons?.[next]?.focus();
  }
</script>
<div class="segmented {className}" role="group" aria-label={label}>
  {#each options as option}
    <button aria-pressed={value === option.value} class:active={value === option.value} onkeydown={move} onclick={() => onchange(option.value)}>
      {option.label}{#if option.count !== undefined}<span class="count">{option.count}</span>{/if}
    </button>
  {/each}
</div>
