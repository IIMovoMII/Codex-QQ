export type Page = 'overview' | 'qq' | 'codex' | 'notifications' | 'settings' | 'updates' | 'help' | 'setup' | 'recovery' | 'gallery';
export type Scenario = 'ready' | 'offline' | 'stopped' | 'unknown' | 'empty' | 'unconfigured';
export type Theme = 'light' | 'dark' | 'system';
export type Tone = 'success' | 'warning' | 'danger' | 'info' | 'neutral';
export type DeliveryState = 'sent' | 'pending' | 'unknown' | 'failed' | 'terminated';
export interface Preferences {
  version: 1;
  theme: Theme;
  transparency: boolean;
  motion: boolean;
  quality: 'auto' | 'fine' | 'light';
  startupView: 'overview' | 'tray';
  checkUpdates: boolean;
  automaticUpdates: boolean;
}
export interface Snapshot {
  revision: number;
  observedAt: string;
  runtime: 'ready' | 'stopped' | 'unknown';
  codex: 'ready' | 'offline' | 'unknown';
  qq: 'ready' | 'offline' | 'unknown';
  qqApi?: string;
  configured: boolean;
  appId: string;
  sandbox: boolean;
  identity: string;
  groupsEnabled?: boolean | null;
  hooks: 'ready' | 'untrusted' | 'missing' | 'unknown';
  hooksInstalled?: boolean | null;
  hooksEnabled?: boolean | null;
  hooksTrust?: 'confirmed' | 'untrusted' | 'unknown';
  startup: boolean;
  startupEnabled?: boolean;
  startupVerified: boolean;
  startupConfiguredVerified?: boolean;
  receivedByUser: boolean;
  testSubmitted?: boolean;
  appDataPath?: string;
  qqDataPath?: string;
  installPath?: string;
  migrationRequired?: boolean;
}
export interface Delivery {
  id: string;
  time: string;
  ageDays: number;
  event: 'complete' | 'attention' | 'error';
  title: string;
  target: string;
  state: DeliveryState;
  timeline?: { time: string; label: string; icon: string }[];
}
export interface CommandResult {
  operationId: string;
  status: 'confirmed' | 'unknown';
  preview: boolean;
  errorCode?: string;
  data?: {
    snapshot?: Snapshot;
    preferences?: Partial<Preferences> & { revision?: number };
    checks?: Record<string, boolean>;
    setup?: SetupState;
    deliveries?: Delivery[];
    total?: number;
    hasMore?: boolean;
    logs?: { time: string; level: string; component: string; text: string }[];
    exportPath?: string;
    update?: { status: string; version?: string; currentVersion?: string; notes?: string; bytes?: number };
    guard?: { allowed: boolean; reason?: string };
  };
}
export interface SetupState { phase: 'new' | 'pairing' | 'configured' | 'recover'; step: number; ready: boolean; rebind?: boolean }
export type Command =
  | 'get_snapshot' | 'check_connections' | 'start_runtime' | 'stop_runtime' | 'restart_runtime'
  | 'set_startup' | 'repair_startup' | 'patch_preferences' | 'install_hooks' | 'commit_config_change'
  | 'complete_binding' | 'send_test_message' | 'record_user_confirmation'
  | 'prepare_files' | 'begin_rebind' | 'begin_pairing' | 'edit_pairing' | 'cancel_pairing' | 'recover_setup'
  | 'run_checks' | 'check_update' | 'download_update' | 'cancel_update' | 'apply_update' | 'get_update_guard' | 'get_setup_state' | 'robot_ready' | 'get_operation' | 'advance_setup'
  | 'list_notifications' | 'list_logs' | 'export_diagnostics' | 'open_codex' | 'open_known_folder' | 'open_allowed_url';

export const deliveryLabels: Record<DeliveryState, string> = {
  sent: '已送出', pending: '待投递', unknown: '结果未确认', failed: '发送失败', terminated: '已终止',
};
export const deliveryTones: Record<DeliveryState, Tone> = {
  sent: 'success', pending: 'info', unknown: 'warning', failed: 'danger', terminated: 'neutral',
};
