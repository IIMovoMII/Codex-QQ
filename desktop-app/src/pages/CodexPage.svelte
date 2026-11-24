<script lang="ts">
  import { app } from '../app/state.svelte';
  import { observedTime } from '../app/time';
  import Button from '../components/Button.svelte';
  import Icon from '../components/Icon.svelte';
  import PageHeader from '../components/PageHeader.svelte';
  import Pill from '../components/Pill.svelte';
  let { onconfirm, onopen }: { onconfirm: (kind: string) => void; onopen: (page: 'help' | 'settings') => void } = $props();
  let details = $state(false);
  const controlReady = $derived(app.snapshot.codex === 'ready');
  const limited = $derived(app.snapshot.codex === 'limited');
  const running = $derived(controlReady || limited);
  const unknown = $derived(app.snapshot.codex === 'unknown');
  const controlUnavailable = $derived(limited ? '暂不可用' : unknown ? '待检查' : '等待 Codex');
</script>

<div class="page">
  <PageHeader title="Codex 连接" subtitle="连接状态仅反映最近一次本机核对">
    {#if running}<Button variant="secondary" icon="refresh" onclick={() => app.refresh()} loading={app.pending.includes('check_connections')}>重新检查</Button>{:else}<Button variant="primary" icon="monitor" onclick={() => void app.run('open_codex')}>打开 Codex</Button>{/if}
  </PageHeader>

  <section class="flow-panel surface" aria-label="Codex 与助手连接路径">
    <div class="flow-node"><span class="flow-icon"><Icon name="monitor" size={21}/></span><div><strong>Codex Desktop</strong><Pill tone={running ? 'success' : unknown ? 'warning' : 'neutral'}>{running ? '运行中' : unknown ? '待核对' : '未运行'}</Pill></div></div>
    <Icon name="right" class="flow-link" size={20}/>
    <div class="flow-node"><span class="flow-icon"><Icon name="code" size={21}/></span><div><strong>本机连接</strong><Pill tone={controlReady ? 'success' : 'warning'}>{controlReady ? '可用' : limited ? '已连接 · 控制受限' : unknown ? '待检查' : '未连接'}</Pill></div></div>
    <Icon name="right" class="flow-link" size={20}/>
    <div class="flow-node"><span class="flow-icon"><Icon name="bell" size={21}/></span><div><strong>QQ 助手</strong><Pill tone={app.snapshot.runtime === 'ready' ? 'success' : 'warning'}>{app.snapshot.runtime === 'ready' ? '已连接' : '待处理'}</Pill></div></div>
  </section>

  <section class="page-section">
    <div class="section-heading"><h2>连接能力</h2><button class="text-button" aria-expanded={details} onclick={() => details = !details}>{details ? '收起详情' : '查看能力详情'} <Icon name={details ? 'down' : 'right'} size={16}/></button></div>
    <div class="surface capability-list">
      <div class="capability"><span class="settings-icon"><Icon name="bell" size={17}/></span><div><strong>任务提醒</strong><p class="muted small">Codex 的本地任务状态经助手转为 QQ 通知。</p></div><Pill tone={app.snapshot.runtime === 'ready' ? 'success' : 'warning'}>{app.snapshot.runtime === 'ready' ? '运行中' : '待核对'}</Pill></div>
      <div class="capability"><span class="settings-icon"><Icon name="link" size={17}/></span><div><strong>引用控制</strong><p class="muted small">每次操作按当前 QQ 消息的引用确定对话。</p></div><Pill tone={controlReady ? 'success' : limited ? 'warning' : 'neutral'}>{controlReady ? '可用' : controlUnavailable}</Pill></div>
      <div class="capability"><span class="settings-icon"><Icon name="list" size={17}/></span><div><strong>运行中排队</strong><p class="muted small">正文进入 Codex 原生可见队列。</p></div><Pill tone={controlReady ? 'success' : limited ? 'warning' : 'neutral'}>{controlReady ? '已支持' : controlUnavailable}</Pill></div>
      {#if details}<div class="capability-detail"><span><strong>状态来源</strong><small>本机 Desktop 连接检查 · {observedTime(app.snapshot.observedAt, true)}</small></span><span><strong>检查范围</strong><small>Desktop 进程、本机工具和控制接口</small></span><span><strong>最近状态</strong><small>{controlReady ? '连接已核对，异步问题仍在电脑处理。' : limited ? '本机工具已连接，引用控制所需的接口当前不可用。' : '当前未发现可用的 Desktop 连接。'}</small></span></div>{/if}
    </div>
  </section>

  {#if app.snapshot.hooks !== 'unknown'}
  <section class="page-section">
    <div class="section-heading"><h2>提醒钩子</h2><Pill tone={app.snapshot.hooks === 'ready' ? 'success' : app.snapshot.hooks === 'untrusted' ? 'warning' : 'neutral'}>{app.snapshot.hooks === 'ready' ? '已安装并启用' : app.snapshot.hooks === 'untrusted' ? '待信任' : '尚未安装或启用'}</Pill></div>
    <div class="surface hook-panel">
      <div class="hook-copy"><span class="settings-icon"><Icon name="shield" size={18}/></span><div><strong>Codex 任务通知钩子</strong><p class="muted small">用于补充任务完成和需要参与时的提醒。</p>{#if app.snapshot.hooks === 'ready'}<div class="hook-badges"><Pill tone="success">配置已写入并启用</Pill></div>{:else if app.snapshot.hooks === 'untrusted' || app.snapshot.hooksTrust === 'untrusted'}<div class="hook-badges"><Pill tone="warning">需在 Codex 信任</Pill></div>{/if}</div></div>
      <div class="hook-actions">{#if app.snapshot.hooks === 'missing'}<Button variant="primary" onclick={() => onconfirm('install-hooks')}>安装提醒钩子…</Button>{:else if app.snapshot.hooks === 'untrusted' || app.snapshot.hooksTrust === 'untrusted'}<Button variant="primary" onclick={() => onconfirm('trust-hooks')}>打开 Codex 处理</Button>{:else}<Button variant="secondary" onclick={() => onopen('help')}>查看说明</Button>{/if}</div>
    </div>
  </section>
  {/if}

  <section class="page-section small-note"><Icon name="info" size={17}/><p>异步问题、登录验证和额外审批需要在电脑上的 Codex 中处理。长对话的摘要可能不包含完整请求信息。</p><button class="text-button" onclick={() => onopen('help')}>查看操作说明 <Icon name="right" size={15}/></button></section>
  {#if app.error}<div class="error-banner" role="alert"><Icon name="error" size={18}/>{app.error}</div>{/if}
</div>

<style>
  .flow-panel { display:flex;align-items:center;justify-content:space-between;gap:12px;padding:22px 24px;border-radius:18px; }
  .flow-node { display:flex;align-items:center;gap:12px;min-width:0; }.flow-icon { display:grid;width:42px;height:42px;flex:0 0 42px;place-items:center;border:1px solid var(--line);border-radius:13px;background:var(--blue-wash);color:var(--blue-dark); }.flow-node > div { display:grid;align-items:start;gap:6px; }.flow-node strong { white-space:nowrap;font-size:12px; }.flow-panel :global(.flow-link) { flex:0 0 auto;color:var(--faint); }
  .capability-list { padding:5px 18px;border-radius:17px; }.capability { display:grid;grid-template-columns:34px minmax(0,1fr) auto;align-items:center;gap:12px;min-height:72px;border-bottom:1px solid var(--line); }.capability:last-of-type { border-bottom:0; }.capability strong { font-size:12px; }.capability p { margin-top:3px; }.capability-detail { display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:16px;padding:15px 4px;border-top:1px solid var(--line); }.capability-detail span { display:grid;gap:4px; }.capability-detail strong { font-size:11px; }.capability-detail small { color:var(--muted);font-size:10px;line-height:1.65; }
  .hook-panel { display:flex;align-items:center;justify-content:space-between;gap:18px;padding:18px 20px;border-radius:17px; }.hook-copy { display:flex;align-items:flex-start;gap:12px; }.hook-copy strong { font-size:13px; }.hook-copy p { margin-top:4px; }.hook-badges { display:flex;gap:7px;margin-top:9px;flex-wrap:wrap; }.hook-actions { display:grid;justify-items:end;gap:5px; }.small-note { display:flex;align-items:center;gap:10px;color:var(--muted);font-size:11px; }.small-note p { flex:1;line-height:1.7; }
  @media(max-width:640px) { .flow-panel { padding:13px 10px;gap:5px; }.flow-node { display:grid;justify-items:center;gap:5px; }.flow-icon { width:34px;height:34px;flex-basis:34px; }.flow-icon :global(svg) { width:17px; }.flow-node > div { justify-items:center; }.flow-node strong { font-size:9px; }.flow-node :global(.pill) { font-size:9px;min-height:20px;padding:2px 6px; }.flow-panel :global(.flow-link) { width:14px; }.hook-panel { align-items:flex-start;flex-direction:column; }.hook-actions { width:100%;justify-items:stretch; }.small-note { align-items:flex-start;flex-wrap:wrap; }.small-note p { flex-basis:calc(100% - 32px); }.capability-detail { grid-template-columns:1fr; } }
</style>
