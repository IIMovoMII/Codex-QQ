<script lang="ts">
  import { app } from '../app/state.svelte';
  import Button from '../components/Button.svelte';
  import Icon from '../components/Icon.svelte';
  import PageHeader from '../components/PageHeader.svelte';
  import Pill from '../components/Pill.svelte';
  import Toggle from '../components/Toggle.svelte';
  let { onconfirm, onopen }: { onconfirm: (kind: string) => void; onopen: (page: 'help' | 'overview') => void } = $props();
  import { onMount } from 'svelte';
  import packageInfo from '../../package.json';
  const status = $derived(app.update.status);
  const progress = $derived(app.update.progress);
  const version = $derived(app.update.version);
  const notes = $derived(app.update.notes);
  const lastCheck = $derived(app.update.lastCheck);
  const requestId = $derived(app.update.operationId);
  const currentVersion = packageInfo.version;
  const canApply = $derived(status === 'verified');
  const knownCheckFailure = $derived(!app.update.unknownFailure && status === 'failure'
    && ['update_feed_unavailable','update_configuration_invalid','update_source_invalid'].includes(app.update.errorCode));
  const failureMessage = $derived(({ update_feed_unavailable:'暂时无法获取更新信息，请检查网络后重新检查。',
    update_configuration_invalid:'更新配置无效，请查看诊断或修复安装。',
    update_source_invalid:'更新来源未通过校验，请核对官方发行页面与诊断。' } as Record<string,string>)[app.update.errorCode] || '本次更新检查未完成，请稍后重新检查。');
  const statusLabel = $derived(({ unchecked:'尚未检查更新',latest:'已是最新版本',checking:'正在检查更新',available:'发现新版本',downloading:'正在下载更新',verified:'签名已验证',installing:'正在安装',failure:knownCheckFailure ? '检查更新失败' : '更新结果未确认' } as Record<string,string>)[status]);
  async function check() {
    await app.checkAppUpdate();
  }
  async function download() {
    await app.downloadAppUpdate();
  }
  async function copyVersion() {
    try {
    await navigator.clipboard.writeText('Codex QQ 助手 v' + currentVersion + (app.preview ? ' · 设计预览' : ' · Windows x64'));
    app.notify('版本信息已复制');
    } catch { app.notify('无法访问剪贴板，请重试'); }
  }
  onMount(() => {
    let dispose: (() => void) | undefined;
    let ended = false;
    if (!app.preview) {
      void import('@tauri-apps/api/event').then(({ listen }) => listen<{received:number;total?:number}>('update-progress', e => {
        app.update.progress = e.payload.total ? Math.round(e.payload.received / e.payload.total * 100) : 0;
      })).then(fn => { if (ended) fn(); else dispose = fn; });
    }
    return () => { ended = true; dispose?.(); };
  });
</script>

<div class="page">
  <PageHeader title="更新" subtitle="应用与 QQ 网关版本分开管理">
    <Button variant="primary" icon="refresh" loading={status === 'checking'} onclick={() => void check()}>{status === 'checking' ? '正在检查' : '检查更新'}</Button>
  </PageHeader>
  {#if app.error}<div class="error-banner" role="alert"><Icon name="error" size={17}/>{app.error}</div>{/if}
  <section class="update-hero surface">
    <span class="update-art"><Icon name="package" size={34}/></span>
    <div class="update-meta"><div class="row"><Pill tone={status === 'latest' ? 'success' : status === 'failure' ? 'danger' : 'info'}>{statusLabel}</Pill><span class="muted small">稳定版渠道</span></div><h2>Codex QQ 助手 <span class="version">v{currentVersion}{app.preview ? " · 设计预览" : ""}</span></h2><p class="muted small">最近检查：{lastCheck}</p></div>
    <div class="update-versions"><span>配套网关</span><strong>随应用固定版本发布</strong><button class="text-button" onclick={() => onopen('help')}>组件详情 <Icon name="right" size={15}/></button></div>
  </section>

  {#if status === 'available' || status === 'downloading' || status === 'verified' || status === 'installing' || status === 'failure'}
    <section class="page-section update-panel surface">
      <div class="spread"><div><h2>{status === 'failure' ? (knownCheckFailure ? '检查更新失败' : '更新没有完成') : 'Codex QQ 助手 v' + version}</h2><p class="muted small" style="margin-top:5px">{status === 'failure' ? (knownCheckFailure ? failureMessage : '请核对网络与诊断，当前安装保持原状。') : notes || '该版本未提供发布说明。'}</p></div><Pill tone={status === 'failure' ? 'danger' : status === 'verified' ? 'success' : 'info'}>{status === 'verified' ? '签名与文件已校验' : status === 'failure' ? (knownCheckFailure ? '未开始安装' : '结果未确认') : '应用更新'}</Pill></div>
      {#if status === 'downloading'}<div class="download-state"><div class="spread"><span>下载应用更新</span><span class="muted">{progress}%</span></div><div class="progress-track"><div class="progress-fill" style="width:{progress}%"></div></div></div>{/if}
      {#if status === 'verified'}<div class="note-banner"><Icon name="info" size={17}/><span>{app.preview ? '设计预览不安装更新。' : '文件已通过固定公钥验签。安装前会再次核对活动任务、待发通知和配置事务；状态不明时等待。'}</span></div>{/if}
      {#if status === 'failure'}<div class="error-banner"><Icon name="info" size={17}/><span>{knownCheckFailure ? '本次没有下载或安装更新，助手可以继续使用。' : '结果未知时先核对操作状态，不会自动重新下载。'}{requestId ? ` 操作编号：${requestId}` : ''}</span></div>{/if}
      <div class="update-actions">
        {#if status === 'available'}<Button variant="primary" icon="download" onclick={() => void download()}>下载更新</Button>
        {:else if status === 'verified'}<Button variant="primary" icon="refresh" disabled={!canApply} onclick={() => onconfirm('apply-update')}>{canApply ? '重启助手并安装…' : '等待可安全安装'}</Button>
        {:else if status === 'failure'}<Button variant="secondary" onclick={() => onopen('help')}>查看诊断</Button>
        {:else if status === 'downloading'}<Button variant="secondary" onclick={() => void app.cancelAppUpdate()}>取消下载</Button>
        {:else}<Button variant="secondary" disabled>正在安装</Button>{/if}
        {#if status === 'available' || status === 'verified'}<Button variant="ghost" onclick={() => onopen('overview')}>稍后处理</Button>{/if}
      </div>
    </section>
  {/if}

  <section class="page-section">
    <div class="section-heading"><h2>发布说明</h2><button class="text-button" onclick={() => void app.run('open_allowed_url', () => {}, { url: 'https://github.com/IIMovoMII/Codex-QQ/releases' })}>在浏览器查看 <Icon name="external" size={15}/></button></div>
    <div class="surface release-notes"><article><span class="release-date">{app.preview ? '设计预览' : '当前安装'}</span><h3>Codex QQ 助手 v{currentVersion}</h3><p class="muted small">{notes || '检查更新后会在这里显示远端版本说明。'}</p></article><div class="divider"></div><div class="spread"><span class="muted small">{app.preview ? '预览版本不代表已发布资产。' : 'Windows x64 · Tauri / WebView2'}</span><Button variant="ghost" icon="copy" onclick={() => void copyVersion()}>复制版本信息</Button></div></div>
  </section>

  <section class="page-section">
    <div class="section-heading"><h2>更新偏好</h2></div>
    <div class="surface settings-list update-settings">
      <div class="settings-row"><span class="settings-icon"><Icon name="refresh" size={17}/></span><div class="settings-copy"><strong>启动时检查更新</strong><span>检查失败不会影响助手启动</span></div><Toggle checked={app.preferences.checkUpdates} label="启动时检查更新" onchange={(v) => app.preference('checkUpdates', v)}/></div>
      <div class="settings-row"><span class="settings-icon"><Icon name="download" size={17}/></span><div class="settings-copy"><strong>自动下载并在空闲时安装</strong><span>默认关闭；有活动任务、待发通知或状态未知时等待</span></div><Toggle checked={app.preferences.automaticUpdates} label="自动下载并在空闲时安装" onchange={(v) => app.preference('automaticUpdates', v)}/></div>
    </div>
  </section>
</div>

<style>
  .update-hero { display:grid;grid-template-columns:66px minmax(0,1fr) minmax(190px,.55fr);align-items:center;gap:17px;min-height:143px;padding:22px;border-radius:19px; }.update-art { display:grid;width:58px;height:58px;place-items:center;border:1px solid var(--line);border-radius:17px;background:var(--blue-wash);color:var(--blue-dark); }.update-meta { display:grid;gap:8px; }.update-meta h2 { margin-top:2px;font-size:18px; }.version { color:var(--muted);font-size:12px;font-weight:540; }.update-versions { display:grid;justify-items:start;gap:4px;padding-left:20px;border-left:1px solid var(--line);color:var(--muted);font-size:10px; }.update-versions strong { color:var(--ink-soft);font-size:11px;font-weight:600; }.update-versions .text-button { min-height:27px;padding-left:0;font-size:11px; }
  .update-panel { display:grid;gap:15px;padding:20px;border-radius:17px; }.download-state { display:grid;gap:9px;font-size:12px; }.update-actions { display:flex;gap:8px;justify-content:flex-end; }.release-notes { display:grid;gap:16px;padding:19px 21px;border-radius:16px; }.release-notes h3 { margin-top:7px; }.release-date { color:var(--muted);font-size:10px; }.update-settings { padding-top:3px;padding-bottom:3px; }
  @media(max-width:640px) { .update-hero { grid-template-columns:48px minmax(0,1fr);gap:11px;padding:15px; }.update-art { width:44px;height:44px; }.update-art :global(svg) { width:24px; }.update-meta h2 { font-size:14px; }.update-versions { grid-column:2;padding:8px 0 0;border-left:0;border-top:1px solid var(--line); }.update-panel { padding:15px; }.update-actions { justify-content:stretch;flex-wrap:wrap; }.update-actions :global(.button) { flex:1; }.update-settings .settings-row { grid-template-columns:29px minmax(0,1fr) auto; } }
</style>
