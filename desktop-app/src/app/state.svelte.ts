import { execute, isNative, readSnapshot } from '../bridge/client';
import type { Command, Page, Preferences, Scenario, Snapshot, SetupState } from '../bridge/contracts';
import { applyNativeTheme } from '../bridge/client';
import { deliveriesFor, snapshotFor } from '../preview/fixtures';

export const defaultPreferences: Preferences = {
  version: 1, theme: 'light', transparency: true, motion: true, quality: 'auto',
  startupView: 'overview', checkUpdates: true, automaticUpdates: false,
};
const storageKey = 'codex-qq.ui.preferences.v1';
function readPreferences(): Preferences {
  try {
    const raw = JSON.parse(localStorage.getItem(storageKey) || '{}');
    const p = { ...defaultPreferences };
    if (['light', 'dark', 'system'].includes(raw.theme)) p.theme = raw.theme;
    if (['auto', 'fine', 'light'].includes(raw.quality)) p.quality = raw.quality;
    if (['overview', 'tray'].includes(raw.startupView)) p.startupView = raw.startupView;
    for (const key of ['transparency', 'motion', 'checkUpdates', 'automaticUpdates'] as const) {
      if (typeof raw[key] === 'boolean') p[key] = raw[key];
    }
    return p;
  } catch { return { ...defaultPreferences }; }
}
const scenarios: Scenario[] = ['ready', 'offline', 'stopped', 'unknown', 'empty', 'unconfigured'];
const candidate = new URLSearchParams(location.search).get('scenario') as Scenario;
const initialScenario = scenarios.includes(candidate) ? candidate : 'ready';
const nativeInitial: Snapshot = { revision: 0, observedAt: '', runtime: 'unknown', codex: 'unknown',
  qq: 'unknown', configured: false, appId: '', sandbox: false, identity: '', hooks: 'missing',
  startup: false, startupVerified: false, receivedByUser: false };

class AppState {
  page = $state<Page>('overview');
  subroute = $state('');
  preferences = $state<Preferences>(isNative ? { ...defaultPreferences } : readPreferences());
  scenario = $state<Scenario>(initialScenario);
  snapshot = $state<Snapshot>(isNative ? { ...nativeInitial } : snapshotFor(initialScenario));
  deliveries = $state(isNative ? [] : deliveriesFor(initialScenario));
  loaded = $state(!isNative);
  setup = $state<SetupState>({ phase: 'new', step: 0, ready: false });
  checks = $state<Record<string, boolean>>({});
  deliveryTotal = $state(0);
  deliveriesLoaded = $state(!isNative);
  logs = $state<{ time: string; level: string; component: string; text: string }[]>([]);
  logsLoaded = $state(!isNative);
  pending = $state<string[]>([]);
  toast = $state('');
  toastWarning = $state(false);
  error = $state('');
  update = $state({ status: 'unchecked', version: '', notes: '', lastCheck: '尚未检查', operationId: '', progress: 0, errorCode: '', unknownFailure: true });
  private updateBusy = false;
  private nextUpdateCheck = 0;
  private nextIdleCheck = 0;
  helpSection = $state('');
  preview = !isNative;
  preferencesRevision = 0;
  private preferenceWrite = Promise.resolve();
  private toastTimer: ReturnType<typeof setTimeout> | undefined;

  setScenario(scenario: Scenario) {
    if (isNative) return;
    this.scenario = scenario;
    this.snapshot = snapshotFor(scenario);
    this.deliveries = deliveriesFor(scenario);
    if (scenario === 'unconfigured') this.navigate('setup');
  }
  navigate(page: Page, sub = '') {
    if (isNative && page === 'gallery') page = 'overview';
    this.page = page; this.subroute = sub;
    location.hash = `/${page}${sub ? '/' + sub : ''}`;
  }
  notify(text: string, warning = false) {
    this.toast = text;
    this.toastWarning = warning;
    clearTimeout(this.toastTimer);
    this.toastTimer = setTimeout(() => { this.toast = ''; }, 4200);
  }
  applySnapshot(snapshot: Snapshot) {
    this.snapshot = snapshot;
    if (!isNative) return;
    this.scenario = snapshot.configured ? 'ready' : 'unconfigured';
  }
  private applyPreferences(value: Partial<Preferences> & { revision?: number }) {
    for (const key of Object.keys(defaultPreferences) as (keyof Preferences)[]) {
      if (key in value) (this.preferences as unknown as Record<string, unknown>)[key] = value[key];
    }
    this.preferencesRevision = value.revision ?? this.preferencesRevision;
  }
  private invalidateConnections(command: Command) {
    if (isNative && command === 'check_connections') {
      this.snapshot = { ...this.snapshot, runtime: 'unknown', codex: 'unknown', qq: 'unknown' };
    }
  }
  async initialize() {
    if (!isNative) return;
    try {
      const value = await readSnapshot();
      this.applySnapshot(value.snapshot);
      this.applyPreferences(value.preferences);
      void applyNativeTheme(this.preferences.theme).catch(() => {});
      this.deliveries = [];
      const setup = await execute('get_setup_state');
      if (setup.data?.setup) this.setup = setup.data.setup;
      this.loaded = true;
      await this.refreshDeliveries();
    } catch (error) {
      this.error = typeof error === 'string' ? error : '本机服务无法读取，请退出助手后重新打开；必要时修复安装。';
      this.snapshot = { ...nativeInitial };
      this.deliveries = [];
      this.notify('无法读取本机助手状态；页面当前只显示待核对状态。', true);
    }
  }
  preference<K extends keyof Preferences>(key: K, value: Preferences[K]) {
    this.preferences[key] = value;
    if (!isNative) {
      try { localStorage.setItem(storageKey, JSON.stringify(this.preferences)); }
      catch { this.notify('外观已应用，本次未能保存偏好。'); }
      return;
    }
    this.preferenceWrite = this.preferenceWrite.then(async () => {
      const result = await execute('patch_preferences', { [key]: value, expectedRevision: this.preferencesRevision });
      if (result.status === 'confirmed' && result.data?.preferences) this.applyPreferences(result.data.preferences);
      else {
        await this.initialize();
        this.notify('偏好保存尚未确认，请重新核对。', true);
      }
      if (key === 'theme') void applyNativeTheme(this.preferences.theme).catch(() => this.notify('外观已保存，原生标题栏暂未更新。', true));
    }).catch(() => {
      this.notify('偏好未能保存到本机。', true);
      return this.initialize();
    });
  }
  async run(command: Command, apply: () => void = () => {}, params: Record<string, unknown> = {}) {
    if (this.pending.includes(command)) return false;
    this.pending = [...this.pending, command];
    this.error = '';
    try {
      const result = await execute(command, params);
      if (result.status !== 'confirmed') {
        this.invalidateConnections(command);
        this.error = result.errorCode === 'update_wait_for_idle'
          ? '暂未开始安装：有活动任务、待发通知或尚未核实的状态。请稍后再试。'
          : `操作结果尚未确认（${result.operationId}），请先核对。`;
        return false;
      }
      // The native service remains authoritative. Only preview commands mutate fixtures.
      if (result.preview) {
        apply();
        this.snapshot.revision++;
        this.snapshot.observedAt = new Date().toISOString();
      } else if (result.data?.snapshot) {
        this.applySnapshot(result.data.snapshot);
      }
      if (result.data?.setup) this.setup = result.data.setup;
      if (result.data?.checks) this.checks = result.data.checks;
      if (result.data?.exportPath) {
        this.notify('诊断包已保存，可在诊断目录查看。');
        try { await execute('open_known_folder', { folder: 'diagnostics' }); }
        catch { this.notify('诊断包已保存，目录未能自动打开。', true); }
      }
      if (!result.preview && result.data?.preferences) {
        this.applyPreferences(result.data.preferences);
      }
      return true;
    } catch {
      this.invalidateConnections(command);
      this.error = '当前无法连接本机服务，操作结果尚未确认。请先查看诊断。';
      return false;
    } finally { this.pending = this.pending.filter(item => item !== command); }
  }
  async refresh(quiet = false) {
    const ok = await this.run('check_connections');
    if (ok && isNative) { this.loaded = true; await this.refreshDeliveries(); }
    if (ok && !quiet) this.notify(this.preview ? '示例状态已重新核对' : '连接检查完成');
  }
  async refreshDeliveries(more = false) {
    if (!isNative) return;
    try {
    const result = await execute('list_notifications', { offset: more ? this.deliveries.length : 0, limit: 200 });
    if (result.status === 'confirmed' && result.data?.deliveries) {
      this.deliveries = more ? [...this.deliveries, ...result.data.deliveries] : result.data.deliveries;
      this.deliveryTotal = result.data.total ?? this.deliveries.length;
      this.deliveriesLoaded = true;
    }
    } catch { this.deliveriesLoaded = false; }
  }
  async refreshLogs() {
    if (!isNative) return;
    try {
      const result = await execute('list_logs');
      this.logsLoaded = result.status === 'confirmed' && Array.isArray(result.data?.logs);
      if (this.logsLoaded) this.logs = result.data!.logs!;
    } catch { this.logsLoaded = false; }
  }
  async checkAppUpdate() {
    if (this.updateBusy || ['downloading', 'installing'].includes(this.update.status)) return;
    this.updateBusy = true;
    this.update.status = 'checking';
    this.update.operationId = '';
    this.update.errorCode = '';
    this.update.unknownFailure = true;
    this.update.lastCheck = new Date().toLocaleTimeString('zh-CN');
    this.nextUpdateCheck = Date.now() + 6*60*60*1000;
    try {
      const result = await execute('check_update');
      this.update.operationId = result.operationId;
      this.update.errorCode = result.errorCode || '';
      const value = result.data?.update;
      if (result.status !== 'confirmed' || !value) { this.update.status = 'failure'; return; }
      this.update.unknownFailure = false;
      this.update.status = value.status;
      this.update.version = value.version || '';
      this.update.notes = value.notes || '';
    } catch { this.update.status = 'failure'; }
    finally { this.updateBusy = false; }
  }
  async downloadAppUpdate() {
    if (this.updateBusy || this.update.status !== 'available') return;
    this.updateBusy = true; this.update.status = 'downloading'; this.update.progress = 0;
    this.update.errorCode = ''; this.update.unknownFailure = true;
    try {
      const result = await execute('download_update');
      this.update.operationId = result.operationId;
      this.update.status = result.status === 'confirmed' ? 'verified' : result.errorCode === 'update_download_cancelled' ? 'available' : 'failure';
      if (this.update.status === 'verified') this.update.progress = 100;
    } catch { this.update.status = 'failure'; }
    finally { this.updateBusy = false; }
  }
  async automaticUpdateTick() {
    if (!isNative || !this.loaded || !this.snapshot.configured || this.snapshot.migrationRequired
        || this.page === 'setup' || this.pending.length || this.updateBusy) return;
    if ((this.preferences.checkUpdates || this.preferences.automaticUpdates)
        && this.update.status === 'unchecked' && Date.now() >= this.nextUpdateCheck) await this.checkAppUpdate();
    if (!this.preferences.automaticUpdates) return;
    if (this.update.status === 'available') await this.downloadAppUpdate();
    if (this.update.status !== 'verified' || Date.now() < this.nextIdleCheck) return;
    this.nextIdleCheck = Date.now() + 60000;
    try {
      const result = await execute('get_update_guard');
      if (result.status !== 'confirmed' || result.data?.guard?.allowed !== true) return;
      this.update.status = 'installing';
      if (!await this.run('apply_update')) this.update.status = 'failure';
    } catch { this.update.status = 'failure'; }
  }
  async cancelAppUpdate() {
    if (this.update.status !== 'downloading') return;
    try {
      const result = await execute('cancel_update');
      if (result.status !== 'confirmed') this.notify('取消结果未确认，请等待下载结果');
    } catch { this.notify('取消结果未确认，请等待下载结果'); }
  }
  resetAppearance() {
    for (const key of ['theme', 'transparency', 'motion', 'quality'] as const) this.preference(key, defaultPreferences[key] as never);
    this.notify('已恢复默认外观');
  }
}
export const app = new AppState();
