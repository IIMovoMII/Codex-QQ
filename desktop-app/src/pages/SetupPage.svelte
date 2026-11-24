<script lang="ts">
  import { app } from '../app/state.svelte';
  import { execute } from '../bridge/client';
  import type { Command } from '../bridge/contracts';
  import Button from '../components/Button.svelte';
  import Icon from '../components/Icon.svelte';
  import PageHeader from '../components/PageHeader.svelte';
  import Pill from '../components/Pill.svelte';
  import Toggle from '../components/Toggle.svelte';
  import QuoteGuide from '../components/QuoteGuide.svelte';
  let { onconfirm, onopen }: { onconfirm: (kind: string) => void; onopen: (page: 'overview' | 'help') => void } = $props();
  let step = $state(app.preview ? 0 : Math.min(app.setup.step, 7));
  let appId = $state('');
  let secret = $state('');
  let revealSecret = $state(false);
  let openId = $state('');
  let permission = $state(false);
  let sandbox = $state(false);
  let busy = $state(false);
  let error = $state('');
  let revealHelp = $state(false);
  let autoStart = $state(app.snapshot.startup);
  const groups = ['准备', '机器人', '连接', '绑定', '完成'];
  const currentGroup = $derived(step === 0 ? 0 : step === 1 ? 1 : step <= 3 ? 2 : step === 4 ? 3 : 4);
  const labels = ['检查并准备', '我已准备好', '保存并连接', '重新连接', '确认绑定', '启动助手', '安装提醒钩子', '进入概览'];

  async function perform(command: Command, params: Record<string, unknown> = {}) {
    if (busy) return false;
    busy = true; error = '';
    try {
      const result = await execute(command, params);
      if (result.status !== 'confirmed') {
        error = result.errorCode === 'formal_qq_connection_pending' ? '正式 QQ 连接尚未就绪。请检查网络后重试，绑定已保留。' : result.errorCode === 'setup_step_failed_check_diagnostics'
          ? '当前步骤未完成。请核对机器人凭据、沙箱成员和网络；已提交进度保留，可稍后继续。'
          : '操作尚未确认（' + result.operationId + '）。请先核对结果，不要重复提交。';
        return false;
      }
      if (result.data?.snapshot) app.applySnapshot(result.data.snapshot);
      if (result.data?.setup) { app.setup = result.data.setup; step = Math.min(result.data.setup.step, 7); }
      return true;
    } catch {
      error = '无法确认本机服务结果。重新打开后将读取已保存进度；不要重复提交。';
      return false;
    } finally { busy = false; }
  }
  async function forward() {
    if (busy) return;
    if (step === 0) { if (await perform('prepare_files') && app.preview) step = 1; }
    else if (step === 1) { if (await perform('robot_ready') && app.preview) step = 2; }
    else if (step === 2 || step === 3) {
      if (step === 2 && (!/^\d{5,30}$/.test(appId.trim()) || secret.trim().length < 8)) {
        error = '请输入有效的机器人 AppID 和 AppSecret。'; return;
      }
      const params = step === 2 ? { appId: appId.trim(), appSecret: secret, sandbox } : {};
      step = 3;
      const ok = await perform('begin_pairing', params);
      secret = '';
      if (ok && app.preview) step = 4;
    } else if (step === 4) {
      if (!/^[A-Za-z0-9_-]{8,128}$/.test(openId.trim()) || /^\d+$/.test(openId.trim()) || !permission) {
        error = '请填机器人返回的 OpenID（不是 QQ 号），并确认本人授权。'; return;
      }
      if (await perform('complete_binding', { openId: openId.trim(), confirmed: permission })) {
        openId = '';
        if (app.preview) step = 5;
        else if (app.setup.step === 8) onopen('overview');
      }
    } else if (step === 5) {
      if (await perform('start_runtime')) {
        if (app.preview) { app.snapshot.runtime = 'ready'; step = 6; }
        else await perform('advance_setup', { step: 6 });
      }
    } else if (step === 6) {
      if (await perform('install_hooks')) {
        if (app.preview) step = 7;
        else await perform('advance_setup', { step: 7 });
      }
    } else {
      if (autoStart !== app.snapshot.startup && !(await perform('set_startup', { enabled: autoStart }))) return;
      if (app.preview || await perform('advance_setup', { step: 8 })) onopen('overview');
    }
  }
  async function later() {
    if (busy) return;
    secret = ''; openId = '';
    if (await perform('cancel_pairing')) onopen('overview');
  }
  async function skipHooks() {
    if (app.preview) step = 7;
    else await perform('advance_setup', { step: 7 });
  }
  function back() { if (!busy && step <= 2 && step > 0) { step--; error = ''; } }
  async function editCredentials() {
    secret = ''; openId = ''; revealSecret = false;
    if (app.preview) step = 2;
    else await perform('edit_pairing');
  }
  async function copy() {
    try { await navigator.clipboard.writeText('绑定'); app.notify('已复制绑定指令'); }
    catch { app.notify('请在 QQ 私聊机器人发送：绑定'); }
  }
</script>

<div class="page setup-page">
  <PageHeader title="首次配置" subtitle="配置进度保存在本机，可稍后继续。">
    <Button variant="ghost" icon="close" disabled={busy} aria-label="稍后继续" onclick={() => void later()}></Button>
  </PageHeader>
  <div class="progress-steps" aria-label="配置进度">
    {#each groups as group, i}<div class="step" class:current={i === currentGroup} class:done={i < currentGroup}><span class="step-count">{#if i < currentGroup}<Icon name="check" size={13}/>{:else}{i + 1}{/if}</span>{group}</div>{/each}
  </div>
  <div class="setup-content">
    <div class="setup-icon"><Icon name={step === 4 ? 'user' : step === 7 ? 'success' : 'bot'} size={32}/></div>
    {#if step === 0}
      <h1 class="setup-title">让 Codex 与你保持连接</h1>
      <p class="setup-subtitle">用 QQ 接收任务提醒，引用一条消息，就能继续对应对话。</p>
      <div class="surface phase-list">
        <div class="phase-row"><Icon name="monitor"/><span>Windows 桌面应用</span><Pill tone="info">独立运行</Pill></div>
        <div class="phase-row"><Icon name="shield"/><span>内置运行环境与已校验网关</span><Pill tone="info">随安装提供</Pill></div>
        <div class="phase-row"><Icon name="lock"/><span>保留 Codex 账号、模型与历史</span></div>
        <div class="phase-row"><Icon name="code"/><span>Codex 本机连接</span><Pill tone={app.snapshot.codex === 'ready' ? 'success' : 'warning'}>{app.snapshot.codex === 'ready' ? '已连接' : app.snapshot.codex === 'limited' ? '已连接 · 控制受限' : '尚未就绪，可先配置 QQ'}</Pill></div>
      </div>
    {:else if step === 1}
      <h1 class="setup-title">准备你的 QQ 机器人</h1>
      <p class="setup-subtitle">还没有机器人？跟着下面三步完成。</p>
      <div class="surface credential-form">
        <h3>1 · 创建机器人</h3><p class="muted">打开 QQ 开放平台，登录控制台并创建机器人，按平台要求完成认证。</p>
        <a class="text-link" href="https://q.qq.com/" target="_blank" rel="noreferrer">打开 QQ 开放平台 ↗</a>
        <h3>2 · 找到 AppID 与 AppSecret</h3><p class="muted">在“开发设置 / 开发管理”中查看机器人 AppID，生成或查看 AppSecret。</p>
        <h3>3 · 添加自己的测试 QQ</h3><p class="muted">在沙箱或服务范围中加入本人 QQ，开启私信测试。平台登录、实名与验证码需要你本人完成。</p>
        <div class="note-banner"><Icon name="lock" size={17}/><span>AppSecret 是机器人密钥，不是 QQ 密码。仅在下一页本机填写，不要发到聊天或 GitHub。</span></div>
      </div>
    {:else if step === 2}
      <h1 class="setup-title">连接你的机器人</h1><p class="setup-subtitle">凭据只通过本机配置服务保存。</p>
      <div class="surface credential-form">
        <label for="setup-app-id">机器人 AppID</label><input class="text-input" id="setup-app-id" bind:value={appId} inputmode="numeric" autocomplete="off" placeholder="输入机器人 AppID"/>
        <label for="setup-secret">AppSecret</label><input class="text-input" id="setup-secret" type={revealSecret ? 'text' : 'password'} bind:value={secret} autocomplete="new-password" placeholder="仅在本机输入密钥"/><button class="text-button" onclick={() => revealSecret = !revealSecret}>{revealSecret ? '隐藏本次输入' : '显示本次输入'}</button>
        <label class="checkbox-row"><input type="checkbox" bind:checked={sandbox}/><span>使用沙箱环境</span></label>
        <small>AppID 不是普通 QQ 号。密钥提交后清除页面字段；已有有效配置不会被覆盖。</small>
      </div>
    {:else if step === 3}
      <h1 class="setup-title">{busy ? '正在连接 QQ' : '继续 QQ 连接'}</h1>
      <p class="setup-subtitle">等待本次连接完成认证。旧日志和本地 API 均不能代替 QQ 就绪。</p>
      <div class="surface phase-list"><div class="phase-row"><Icon name={busy ? 'loader' : 'link'} class={busy ? 'spin' : ''}/><span>{busy ? '认证连接，等待本次 QQ READY（最多 45 秒）' : '已保存的配置可以继续连接，无需再次填写密钥'}</span><Pill tone="warning">待核对</Pill></div></div>
      <Button variant="ghost" disabled={busy} onclick={() => void editCredentials()}>返回修改凭据</Button>
    {:else if step === 4}
      <h1 class="setup-title">绑定你的 QQ</h1><p class="setup-subtitle">只允许你指定的 QQ 身份控制这台电脑。</p>
      <div class="surface binding-status"><span class="status-dot success"></span><span>本次 QQ 连接已就绪</span></div>
      <section class="surface bind-card"><span class="bind-number">1</span><div><h3>用自己的 QQ 私聊机器人</h3><p>发送下面的指令，获取你的身份标识。</p><div class="command-field"><strong>绑定</strong><Button variant="ghost" icon="copy" onclick={() => void copy()}>复制指令</Button></div></div></section>
      <section class="surface bind-card"><span class="bind-number">2</span><div><h3>填写机器人返回的 OpenID</h3><label class="visually-hidden" for="openid">机器人返回的 OpenID</label><input id="openid" bind:value={openId} autocomplete="off" spellcheck="false" placeholder="粘贴机器人返回的 OpenID"/><small>填写 OpenID，而非 QQ 号码。</small><label class="checkbox-row"><input type="checkbox" bind:checked={permission}/><span>只允许这个 QQ 身份控制本机</span></label><button class="binding-help" aria-expanded={revealHelp} onclick={() => revealHelp = !revealHelp}>找不到 OpenID？</button>{#if revealHelp}<p class="help-inline">确认在本人 QQ 私聊已配置的机器人发送“绑定”，并已添加测试成员。不要填写 QQ 号或引用他人的身份。</p>{/if}</div></section>
    {:else if step === 5}
      <h1 class="setup-title">启动正式助手</h1><p class="setup-subtitle">绑定已保存。接下来启动监听与正式 QQ 连接。</p>
      <div class="surface phase-list"><div class="phase-row"><Icon name="user"/><span>授权身份 · {app.snapshot.identity || '已保存'}</span></div><div class="phase-row"><Icon name="activity"/><span>正式助手</span><Pill tone={app.snapshot.runtime === 'ready' ? 'success' : 'warning'}>{app.snapshot.runtime === 'ready' ? '运行中' : '待启动'}</Pill></div></div>
    {:else if step === 6}
      <h1 class="setup-title">设置任务提醒</h1><p class="setup-subtitle">在 Codex 完成任务或需要你参与时接收提醒。</p>
      <div class="surface hook-preview"><strong>将要变更</strong><div class="preview-line"><Icon name="check" size={16}/>合并本程序的提醒钩子</div><div class="preview-line"><Icon name="check" size={16}/>启用 Codex hooks 功能</div><p class="muted">其他设置保留。安装后如 Codex 提示命令信任，请在电脑上核对。</p></div>
      <Button variant="ghost" disabled={busy} onclick={() => void skipHooks()}>稍后设置提醒</Button>
    {:else}
      <h1 class="setup-title">开始第一次 QQ 操作</h1><p class="setup-subtitle">每一条引用，都指向一个明确的对话。</p>
      <section class="surface credential-form">
        <h3>1 · 私聊机器人发送“帮助”和“对话”</h3><p class="muted">找到机器人返回的对话列表。</p>
        <h3>2 · 右键机器人消息，左键“引用”</h3><p class="muted">确认输入框出现正确的引用预览，再输入列表序号选择对话。</p>
        <h3>3 · 再引用回执，发送任务或“设置”</h3><p class="muted">之后每条操作都需要对应引用。一次选择不会永久锁定目标。</p>
        <div class="note-banner"><Icon name="info" size={17}/><span>本机连接、平台受理与 QQ 聊天实收分别核对。手机推送尚需在手机上确认。</span></div>
        <Button variant="secondary" onclick={() => onconfirm('test-message')}>发送测试消息…</Button>
      </section>
      <QuoteGuide/>
      <div class="row"><Button variant="ghost" onclick={() => void navigator.clipboard.writeText('帮助').then(() => app.notify('已复制帮助指令')).catch(() => app.notify('请在 QQ 私聊发送：帮助'))}>复制帮助指令</Button><Button variant="ghost" onclick={() => onopen('help')}>查看使用说明</Button></div>
      <div class="surface finish-autostart"><Toggle checked={autoStart} label="登录 Windows 后启动" onchange={(v) => autoStart = v}/><span>登录 Windows 后启动</span><small>点击“进入概览”时保存；实际登录效果另行核对。</small></div>
    {/if}
  </div>
  {#if busy || error}<div class:error-banner={!!error} class="setup-feedback" aria-live="polite">{#if busy}<Icon name="loader" class="spin" size={17}/>正在处理，请等待当前步骤的确认…{:else}<Icon name="error" size={17}/>{error}{/if}</div>{/if}
  <footer class="setup-footer"><div class="setup-foot-left"><span class="muted small">已提交进度保存在本机</span><Button variant="ghost" disabled={busy} onclick={() => void later()}>稍后继续</Button></div><div class="row">{#if step > 0 && step < 3}<Button variant="secondary" disabled={busy} onclick={back}>上一步</Button>{/if}<Button variant="primary" loading={busy} onclick={() => void forward()}>{labels[step]}</Button></div></footer>
</div>

<style>
  .setup-page { position:relative;padding-top:22px;display:flex;flex-direction:column;min-height:100%; }
  .setup-page :global(.page-header) { min-height:42px;margin-bottom:8px; }
  .setup-content { flex:1;padding-bottom:16px;width:min(760px,100%);margin:0 auto; }
  .phase-list { padding:5px 18px;border-radius:20px; }
  .phase-row { display:grid;grid-template-columns:28px minmax(0,1fr) auto;align-items:center;gap:12px;min-height:59px;border-bottom:1px solid var(--line); }
  .phase-row:last-child { border:0; }
  .credential-form { display:grid;gap:14px;padding:22px 25px;border-radius:20px; }
  .credential-form h3 { margin-top:6px;font-size:15px; }
  .credential-form p { line-height:1.8; }
  .credential-form small { color:var(--muted); }
  .checkbox-row { display:flex;align-items:center;gap:9px;padding-top:12px;font-size:13px; }
  .checkbox-row input { width:18px;height:18px;accent-color:var(--blue); }
  .binding-status { display:flex;align-items:center;gap:12px;padding:16px 22px;margin-bottom:12px; }
  .bind-card { display:grid;grid-template-columns:38px minmax(0,1fr);gap:16px;padding:20px 24px;margin-top:12px; }
  .bind-card h3 { font-size:17px;margin-bottom:8px; }.bind-card p { color:var(--muted);margin-bottom:10px; }
  .bind-number { display:grid;place-items:center;width:36px;height:36px;border-radius:11px;color:var(--blue-dark);background:var(--blue-wash);font-weight:700; }
  .command-field { display:flex;justify-content:space-between;align-items:center;background:var(--blue-wash);padding:6px 14px;border-radius:12px; }
  .bind-card input:not([type]) { width:100%;margin:8px 0; }
  .binding-help { display:block;margin-top:15px;color:var(--blue); }.help-inline { margin-top:10px; }
  .hook-preview { display:grid;gap:15px;padding:24px; }.preview-line { display:flex;gap:8px;align-items:center; }
  .finish-autostart { display:grid;grid-template-columns:45px minmax(0,1fr);align-items:center;gap:7px;padding:17px;margin-top:14px; }
  .finish-autostart :global(.toggle) { grid-row:span 2; }.finish-autostart small { color:var(--muted); }
  .setup-feedback { display:flex;justify-content:center;gap:10px;padding:14px;font-size:13px; }
  .setup-footer { position:sticky;bottom:0;z-index:2;backdrop-filter:blur(24px);background:var(--surface);border-top:1px solid var(--line);padding:16px 0;display:flex;justify-content:space-between;gap:12px; }
  .setup-foot-left { display:flex;flex-direction:column;gap:3px;align-items:flex-start; }
  @media(max-width:640px) { .credential-form,.bind-card { padding:16px; }.setup-foot-left > span { display:none; }.phase-row { gap:8px; } }
</style>
