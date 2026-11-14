import type { Command, CommandResult, Preferences, Snapshot } from './contracts';

export const isNative = typeof window !== 'undefined' && '__TAURI_INTERNALS__' in window;

/** The browser adapter only drives synthetic preview state. No local HTTP control port. */
export async function execute(command: Command, params: Record<string, unknown> = {}): Promise<CommandResult> {
  const requestId = crypto.randomUUID();
  if (isNative) {
    const { invoke } = await import('@tauri-apps/api/core');
    const endpoint = ['check_update','download_update','cancel_update','apply_update'].includes(command) ? 'app_update' : 'app_request';
    const reply = await invoke<CommandResult>(endpoint, { request: { version: 1, requestId, command, params } });
    if (!reply || !reply.operationId || !['confirmed', 'unknown'].includes(reply.status) || reply.preview !== false) {
      throw new Error('后台回执无法核对，请查看诊断。');
    }
    return reply;
  }
  await new Promise(resolve => setTimeout(resolve, 520));
  return { operationId: requestId, status: 'confirmed', preview: true };
}

export async function readSnapshot(): Promise<{ snapshot: Snapshot; preferences: Preferences }> {
  if (!isNative) throw new Error('本机快照只在桌面应用中可用。');
  const { invoke } = await import('@tauri-apps/api/core');
  const value = await invoke<{ snapshot: Snapshot; preferences: Preferences }>('app_snapshot');
  if (!value?.snapshot || !value?.preferences) throw new Error('本机状态快照格式无效。');
  return value;
}

export async function windowAction(action: 'minimize' | 'toggleMaximize' | 'close') {
  if (!isNative) return false;
  const { getCurrentWindow } = await import('@tauri-apps/api/window');
  await getCurrentWindow()[action]();
  return true;
}

export async function exitApplication() {
  if (!isNative) return false;
  const { invoke } = await import('@tauri-apps/api/core');
  await invoke('app_exit');
  return true;
}

export async function resetWindow() {
  if (!isNative) return;
  const { invoke } = await import('@tauri-apps/api/core');
  await invoke('app_reset_window');
}

export async function applyNativeTheme(theme: string) {
  if (!isNative) return;
  const { invoke } = await import('@tauri-apps/api/core');
  await invoke('app_appearance', { theme });
}
