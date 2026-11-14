export function observedTime(value: string, seconds = false): string {
  const date = new Date(value);
  if (!value || !Number.isFinite(date.getTime())) return '尚未核实';
  return date.toLocaleTimeString('zh-CN', {
    hour: '2-digit', minute: '2-digit', ...(seconds ? { second: '2-digit' } : {}),
  });
}
