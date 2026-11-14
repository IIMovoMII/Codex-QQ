<script lang="ts">
  import { app } from '../app/state.svelte';
  import { observedTime } from '../app/time';
  import type { Page } from '../bridge/contracts';
  import Button from '../components/Button.svelte';
  import Icon from '../components/Icon.svelte';
  import PageHeader from '../components/PageHeader.svelte';
  import Pill from '../components/Pill.svelte';
  let { onconfirm, onopen }: { onconfirm: (kind: string) => void; onopen: (page: Page) => void } = $props();
  let menuOpen = $state(false);
  const heroState = $derived.by(() => {
    const s = app.snapshot;
    if (!app.loaded) return { title: '正在读取状态', detail: '正在核对本机助手与连接。', icon: 'loader', tone: 'info', action: 'check', actionText: '重新检查' };
    if (!s.configured) return { title: '开始配置', detail: '完成 QQ 机器人设置后开始使用。', icon: 'settings', tone: 'info', action: 'setup', actionText: '配置机器人' };
    if (s.runtime === 'stopped') return { title: '助手已停止', detail: 'QQ 监听与通知投递当前未运行。', icon: 'play', tone: 'warning', action: 'start', actionText: '启动助手' };
    if (s.runtime === 'unknown' || s.qq === 'unknown' || s.codex === 'unknown') return { title: '状态待核对', detail: '最近一次状态检查未能确认所有连接。', icon: 'warning', tone: 'warning', action: 'check', actionText: '重新检查' };
    if (s.qq === 'offline') return { title: 'QQ 连接需要处理', detail: '助手仍在运行，请检查 QQ 网关连接。', icon: 'offline', tone: 'danger', action: 'qq', actionText: '检查 QQ 连接' };
    if (s.codex === 'offline') return { title: '等待 Codex', detail: '助手和 QQ 已就绪，Codex Desktop 当前未运行。', icon: 'monitor', tone: 'info', action: 'codex', actionText: '查看连接' };
    return { title: '已就绪', detail: '助手正在运行，Codex 与 QQ 已连接。', icon: 'check', tone: 'success', action: 'codex', actionText: '打开 Codex' };
  });
  function primary() {
    if (heroState.action === 'setup') onopen('setup');
    else if (heroState.action === 'codex') void app.run('open_codex');
    else if (heroState.action === 'qq') onopen('qq');
    else if (heroState.action === 'start') onconfirm('start');
    else void app.refresh();
  }
  function statusFor(value: string, label: string) {
    if (value === 'ready') return { label, tone: 'success' };
    if (value === 'offline') return { label: '需要处理', tone: 'danger' };
    if (value === 'stopped') return { label: '已停止', tone: 'warning' };
    return { label: '待核对', tone: 'warning' };
  }
  const runtime = $derived(statusFor(app.snapshot.runtime, '助手运行中'));
  const codex = $derived(statusFor(app.snapshot.codex, 'Codex 已连接'));
  const qq = $derived(statusFor(app.snapshot.qq, 'QQ 已连接'));
</script>

<div class="page overview-page">
  <PageHeader title="概览" subtitle="最近检查 · {observedTime(app.snapshot.observedAt)}">
    <Button variant="secondary" icon="refresh" onclick={() => app.refresh()} loading={app.pending.includes('check_connections')}>重新检查</Button>
    <div class="menu-anchor">
      <Button variant="secondary" icon="more" aria-label="更多应用操作" onclick={() => menuOpen = !menuOpen}></Button>
      {#if menuOpen}
        <div class="menu-popover" role="menu">
          <button role="menuitem" onclick={() => { menuOpen = false; onconfirm('stop'); }}><Icon name="power" size={16} />停止助手…</button>
          <button role="menuitem" onclick={() => { menuOpen = false; onconfirm('restart'); }}><Icon name="refresh" size={16} />重启助手…</button>
          <hr class="divider" />
          <button role="menuitem" class="danger-text" onclick={() => { menuOpen = false; onconfirm('exit'); }}><Icon name="logout" size={16} />退出应用…</button>
        </div>
      {/if}
    </div>
  </PageHeader>

  {#if app.error}<div class="error-banner page-section" role="alert"><Icon name="error" size={18} />{app.error}<button class="text-button" onclick={() => onopen('help')}>查看诊断</button></div>{/if}

  <section class="hero glass" aria-label="助手状态">
    <div class="hero-top">
      <div class="hero-art {heroState.tone}"><Icon name={heroState.icon} size={57} stroke={1.7} /></div>
      <div class="hero-copy"><h2 class="hero-title">{heroState.title}</h2><p class="hero-description">{heroState.detail}</p></div>
      <div class="hero-actions">
        <Button variant="primary" icon={heroState.action === 'codex' && heroState.title === '已就绪' ? 'external' : heroState.action === 'start' ? 'play' : heroState.action === 'check' ? 'refresh' : 'arrow'} onclick={primary} loading={app.pending.length > 0}>{heroState.actionText}</Button>
        {#if app.snapshot.configured}<button class="text-button hero-secondary" onclick={() => onopen('codex')}>查看连接 <Icon name="right" size={17} /></button>{/if}
      </div>
    </div>
    <div class="hero-divider"></div>
    <div class="hero-statuses">
      <button class="hero-status" onclick={() => onopen('overview')}><Icon name="monitor" size={20}/><span class="hero-status-label"><span class="status-dot {runtime.tone}"></span>{runtime.label}</span></button>
      <button class="hero-status" onclick={() => onopen('codex')}><Icon name="code" size={20}/><span class="hero-status-label"><span class="status-dot {codex.tone}"></span>Codex {codex.label === 'Codex 已连接' ? '已连接' : codex.label}</span></button>
      <button class="hero-status" onclick={() => onopen('qq')}><Icon name="bell" size={20}/><span class="hero-status-label"><span class="status-dot {qq.tone}"></span>QQ {qq.label === 'QQ 已连接' ? '已连接' : qq.label}</span></button>
    </div>
  </section>

  {#if app.snapshot.configured && (app.snapshot.hooks === 'missing' || app.snapshot.hooks === 'untrusted' || app.snapshot.hooksTrust === 'untrusted')}
    <section class="attention-line surface"><span class="settings-icon"><Icon name="triangle" size={17}/></span><div><strong>提醒钩子待处理</strong><p class="muted small">{app.snapshot.hooks === 'missing' ? '尚未安装或启用本应用的提醒钩子。' : '需要在 Codex 中确认提醒钩子的信任状态。'}</p></div><span class="spacer"></span><Button variant="ghost" onclick={() => onopen('codex')}>去处理 <Icon name="right" size={17}/></Button></section>
  {/if}

  <section class="overview-lower">
    <div class="surface panel-pad">
      <div class="section-heading"><h2>最近通知</h2><button class="text-button" onclick={() => onopen('notifications')}>查看全部 <Icon name="right" size={17}/></button></div>
      {#if !app.deliveriesLoaded}
        <div class="empty-compact"><Icon name="info" size={21}/><span>通知记录尚未核实，请重新检查。</span></div>
      {:else if app.deliveries.length === 0}
        <div class="empty-compact"><Icon name="bell" size={21}/><span>还没有通知记录</span></div>
      {:else}
        <div class="notice-list">
          {#each app.deliveries.slice(0, 3) as item (item.id)}
            <div class="notice-row">
              <span class="notice-time">{item.time}</span>
              <Icon name={item.event === 'complete' ? 'success' : item.event === 'attention' ? 'info' : 'error'} class="notice-event-icon {item.event === 'error' ? 'danger' : item.event === 'complete' ? 'success' : 'info'}" size={18}/>
              <span class="notice-title">{item.title}</span>
              <Pill tone={item.state === 'sent' ? 'success' : item.state === 'pending' ? 'info' : item.state === 'unknown' ? 'warning' : item.state === 'failed' ? 'danger' : 'neutral'}>{item.state === 'sent' ? '已送出' : item.state === 'pending' ? '待投递' : item.state === 'unknown' ? '结果未确认' : item.state === 'failed' ? '发送失败' : '已终止'}</Pill>
            </div>
          {/each}
        </div>
      {/if}
    </div>
    {#if !app.snapshot.receivedByUser}
      <div class="surface panel-pad onboarding">
        <h2 class="onboarding-title">开始使用</h2>
        <div class="onboarding-state">手机实收尚未验证</div>
        <p class="onboarding-copy">可前往 QQ 对话中发送“帮助”和“对话”，开始体验。</p>
        <button class="text-button" onclick={() => { app.helpSection = 'start'; onopen('help'); }}>查看使用说明 <Icon name="right" size={17}/></button>
      </div>
    {/if}
  </section>
</div>

<style>
  .menu-anchor { position:relative; }
  .menu-popover { position:absolute; z-index:8; top:calc(100% + 7px); right:0; width:190px; padding:6px; border:1px solid var(--line-strong); border-radius:12px; background:var(--glass-raised); box-shadow:var(--shadow-panel); backdrop-filter:blur(24px); }
  .menu-popover button { display:flex; align-items:center; gap:9px; width:100%; min-height:37px; padding:0 9px; border:0; border-radius:8px; background:transparent; color:var(--ink-soft); text-align:left; font-size:12px; }
  .menu-popover button:hover { background:var(--blue-wash); color:var(--ink); }
  .menu-popover .danger-text { color:var(--danger); }
  .menu-popover .divider { margin:5px 4px; }
  .hero-status { padding:0 7px; border:0; background:transparent; cursor:pointer; }
  .hero-status:hover { color:var(--blue-dark); }
  .attention-line { display:flex; align-items:center; gap:13px; padding:12px 16px; margin-top:14px; border-radius:14px; }
  .attention-line strong { font-size:12px; }.attention-line p { margin-top:2px; }
  .spacer { flex:1; }
  .empty-compact { display:flex; align-items:center; gap:10px; min-height:92px; color:var(--muted); font-size:12px; }
  @media(max-width:640px) { .hero-status { gap:3px; padding:0 2px; font-size:8px; } .hero-status-label { display:grid; justify-items:center; gap:3px; } .notice-row { grid-template-columns:44px 18px minmax(50px,1fr) auto; } }
</style>
