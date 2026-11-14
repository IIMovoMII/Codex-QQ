<script lang="ts">
  import { onMount } from 'svelte';
  import { app } from '../app/state.svelte';
  import { observedTime } from '../app/time';
  import { execute } from '../bridge/client';
  import Button from '../components/Button.svelte';
  import Icon from '../components/Icon.svelte';
  import Overlay from '../components/Overlay.svelte';
  import PageHeader from '../components/PageHeader.svelte';
  import Pill from '../components/Pill.svelte';
  import Toggle from '../components/Toggle.svelte';
  let { onconfirm, onopen }: { onconfirm: (kind: string) => void; onopen: (page: 'setup' | 'help') => void } = $props();
  let editing = $state(false);
  let expanded = $state(false);
  let identity = $state(false);
  let appId = $state(app.snapshot.appId);
  let secret = $state('');
  let replaceSecret = $state(false);
  let sandbox = $state(app.snapshot.sandbox);
  let appIdError = $state('');
  let saving = $state(false);
  const changingRobot = $derived(appId.trim() !== app.snapshot.appId);
  onMount(() => {
    const clear = () => { secret = ''; replaceSecret = false; if (!saving) editing = false; };
    window.addEventListener('app-window-hidden', clear);
    return () => window.removeEventListener('app-window-hidden', clear);
  });

  async function save() {
    appIdError = '';
    if (!/^\d{5,24}$/.test(appId.trim())) { appIdError = '请输入有效的机器人 AppID。'; return; }
    if ((replaceSecret || changingRobot) && secret.trim().length < 8) { appIdError = '请填写新机器人的 AppSecret，至少需要 8 个字符。'; return; }
    saving = true;
    try {
      const result = await execute(changingRobot ? 'begin_rebind' : 'commit_config_change', { appId: appId.trim(), sandbox, replaceSecret, ...(replaceSecret || changingRobot ? { appSecret: secret } : {}) });
      if (result.status !== 'confirmed') { app.error = result.errorCode === 'different_robot_requires_explicit_rebinding' ? '更换机器人需要重新核验身份。请使用“更换绑定”，现有配置已保留。' : `保存结果尚未确认（${result.operationId}），请先核对。`; return; }
      if (result.preview) { app.snapshot.appId = appId.trim(); app.snapshot.sandbox = sandbox; app.snapshot.revision++; }
      else if (result.data?.snapshot) app.applySnapshot(result.data.snapshot);
      if (result.data?.setup) app.setup = result.data.setup;
      editing = false; secret = ''; replaceSecret = false;
      if (changingRobot) { onopen('setup'); return; }
      app.notify(result.preview ? '设计预览已更新示例配置；没有连接 QQ。' : '机器人配置已保存');
    } catch {
      app.error = '本机配置服务当前不可用，保存结果尚未确认。';
    } finally { saving = false; }
  }
  async function copy(text: string) {
    try { await navigator.clipboard.writeText(text); app.notify('已复制到剪贴板'); }
    catch { app.notify(text); }
  }
</script>

<div class="page">
  <PageHeader title="QQ 机器人" subtitle={app.snapshot.configured ? '已配置 · 连接状态按最近一次检查显示' : '尚未完成 QQ 机器人配置'}>
    <Button variant="primary" icon="refresh" onclick={() => app.refresh()} loading={app.pending.includes('check_connections')}>{app.pending.includes('check_connections') ? '正在检查' : '检查连接'}</Button>
  </PageHeader>

  {#if !app.snapshot.configured}
    <section class="surface"><div class="empty-state"><span class="empty-icon"><Icon name="bot" size={30}/></span><h3>尚未配置 QQ 机器人</h3><p>已有配置会继续使用。开始前请准备好 AppID、AppSecret 和测试 QQ。</p><Button variant="primary" icon="arrow" onclick={() => onopen('setup')}>开始配置</Button></div></section>
  {:else}
    <section class="surface qq-bot-card">
      <div class="section-heading"><h2>机器人</h2><Button variant="secondary" onclick={() => { appId = app.snapshot.appId; sandbox = app.snapshot.sandbox; editing = true; }}>编辑配置</Button></div>
      <div class="bot-info">
        <div class="bot-mark"><Icon name="bot" size={34} stroke={1.55}/></div><div class="bot-divider"></div>
        <div class="bot-summary"><strong>QQ 机器人</strong><span>AppID：<span class="mono">{app.snapshot.appId}</span></span><span>运行环境：{app.snapshot.sandbox ? '平台沙箱' : '正式环境'}</span><span>AppSecret：已配置</span></div>
      </div>
    </section>

    <section class="surface qq-section">
      <h2>连接状态</h2>
      <dl class="connection-lines">
        <div class="stat-line"><dt>QQ 连接</dt><dd class="dot-row"><span class="status-dot {app.snapshot.qq === 'ready' ? 'success' : app.snapshot.qq === 'offline' ? 'danger' : 'warning'}"></span>{app.snapshot.qq === 'ready' ? '已连接' : app.snapshot.qq === 'offline' ? '连接中断' : '待核对'}</dd></div>
        <div class="stat-line"><dt>最近检查</dt><dd>{observedTime(app.snapshot.observedAt)}</dd></div>
      </dl>
      {#if expanded}
        <dl class="connection-lines expanded-lines">
          <div class="stat-line"><dt>本地服务 API</dt><dd>{app.snapshot.qqApi === 'ready' ? '可用' : '待核对'}</dd></div>
          <div class="stat-line"><dt>配套网关</dt><dd>{app.snapshot.qq === 'ready' ? '运行中' : '待检查'}</dd></div>
          <div class="stat-line"><dt>最近 READY</dt><dd>{app.snapshot.qq === 'ready' ? '本次连接已确认' : '尚未确认'}</dd></div>
        </dl>
      {/if}
      <button class="text-button details-toggle" aria-expanded={expanded} onclick={() => expanded = !expanded}>{expanded ? '收起连接详情' : '查看连接详情'} <Icon name={expanded ? 'down' : 'right'} size={17}/></button>
    </section>

    <section class="surface qq-section">
      <div class="section-heading"><h2>授权身份</h2><Button variant="secondary" onclick={() => onconfirm('rebind')}>更换绑定…</Button></div>
      <div class="identity-lines">
        <div class="identity-line"><span class="settings-icon"><Icon name="user" size={17}/></span><div><strong>本人私聊</strong><p class="muted small">OpenID：{app.snapshot.identity || '尚未核实'}</p></div><button class="text-button identity-view" onclick={() => void copy(app.snapshot.identity)}>复制脱敏身份 <Icon name="copy" size={16}/></button></div>
        <div class="identity-line"><span class="settings-icon"><Icon name="users" size={17}/></span><div><strong>群聊授权</strong><p class="muted small">默认关闭 · 仅接受精确配置的群与成员</p></div><span class="muted small">{app.snapshot.groupsEnabled === true ? '已有精确授权' : app.snapshot.groupsEnabled === false ? '未开启' : '待核对'}</span></div>
      </div>
    </section>

    <section class="surface qq-section test-section">
      <div><h2>实际收发测试</h2><p class="muted small">连接检查不会发送 QQ 消息。</p></div>
      <div class="test-status"><span class="settings-icon"><Icon name="phone" size={18}/></span><span>{app.snapshot.receivedByUser ? '你已确认手机收到测试消息' : '手机实收尚未验证'}</span><Pill tone={app.snapshot.receivedByUser ? 'success' : 'neutral'}>{app.snapshot.receivedByUser ? '用户已确认' : '未验证'}</Pill><span class="spacer"></span>{#if app.snapshot.testSubmitted && !app.snapshot.receivedByUser}<Button variant="secondary" onclick={() => void app.run('record_user_confirmation', () => {}, { received: true })}>我已在手机收到</Button>{/if}<Button variant="secondary" onclick={() => onconfirm('test-message')}>发送测试消息…</Button></div>
    </section>

    <div class="qq-links"><a href="https://q.qq.com/" target="_blank" rel="noreferrer">打开 QQ 开放平台 <Icon name="external" size={15}/></a><span></span><button class="text-button" onclick={() => onopen('help')}>查看配置指南 <Icon name="right" size={16}/></button></div>
  {/if}

  {#if editing}
    <Overlay title="编辑机器人配置" onclose={() => { if (!saving) { editing = false; secret = ''; } }} busy={saving}>
      <form class="form-grid" onsubmit={(event) => { event.preventDefault(); void save(); }}>
        <div class="field"><label for="qq-app-id">机器人 AppID</label><input id="qq-app-id" bind:value={appId} inputmode="numeric" autocomplete="off" aria-invalid={!!appIdError}/>{#if appIdError}<span class="field-error">{appIdError}</span>{:else}<small>用于连接已配置的 QQ 机器人。</small>{/if}</div>
        <div class="settings-row editor-setting"><span class="settings-icon"><Icon name="globe" size={17}/></span><div class="settings-copy"><strong>使用沙箱环境</strong><span>切换后需要重新检查连接。</span></div><Toggle checked={sandbox} label="使用沙箱环境" onchange={(value) => sandbox = value}/></div>
        <div class="credential-line"><div class="settings-icon"><Icon name="lock" size={17}/></div><div><strong>AppSecret</strong><p class="muted small">已配置，不会回填或显示现有密钥。</p></div><button type="button" class="text-button" onclick={() => { replaceSecret = !replaceSecret; secret = ''; }}>{replaceSecret ? '取消更换' : '替换密钥'}</button></div>
        {#if replaceSecret || changingRobot}<div class="field"><label for="qq-secret">新的 AppSecret</label><input id="qq-secret" type="password" bind:value={secret} autocomplete="new-password"/><small>仅在本机内存和配置事务中短暂使用，不写入页面存储。</small></div>{/if}
        <div class="note-banner"><Icon name="info" size={18}/><span>{app.preview ? '当前窗口是设计预览；保存只更新示例字段。' : '保存时会短暂重新连接机器人。验证通过后提交，失败则保留原配置；更换机器人请使用“更换绑定”。'}</span></div>
      </form>
        {#if changingRobot}<div class="note-banner"><Icon name="info" size={18}/><span>将暂时断开旧机器人并连接新机器人。下一步必须明确确认本人 OpenID 后才替换绑定；取消会恢复旧服务。</span></div>{/if}
      {#snippet footer()}<Button variant="secondary" disabled={saving} onclick={() => { editing = false; secret = ''; }}>取消</Button><Button variant="primary" loading={saving} onclick={() => void save()}>{changingRobot ? '连接新机器人并重新绑定…' : '验证并保存'}</Button>{/snippet}
    </Overlay>
  {/if}
</div>

<style>
  .qq-bot-card { padding:20px 22px; border-radius:19px; }.qq-bot-card .section-heading { margin-bottom:12px; }
  .bot-info { display:grid; grid-template-columns:72px 1px minmax(0,1fr); align-items:center; gap:16px; min-height:94px; }
  .bot-mark { display:grid; width:62px;height:62px;place-items:center;border-radius:19px;background:var(--blue-wash);color:var(--blue-dark); }
  .bot-divider { width:1px;height:80px;background:var(--line-strong); }.bot-summary { display:grid;gap:1px;color:var(--muted);font-size:12px; }.bot-summary strong { margin-bottom:2px;color:var(--ink);font-size:14px; }
  .qq-section { margin-top:14px;padding:19px 22px;border-radius:19px; }.qq-section > h2 { margin-bottom:6px; }
  .connection-lines { margin:0; }.expanded-lines { margin-top:2px;padding:3px 0;border-top:1px solid var(--line); }
  .connection-lines dt { font-size:12px; }.connection-lines dd { font-size:12px; }
  .details-toggle { min-height:34px;padding-left:0; }.identity-lines { display:grid; }.identity-line { display:flex;align-items:center;gap:12px;min-height:62px;border-top:1px solid var(--line); }.identity-line > div { min-width:0; }.identity-line strong { font-size:13px; }.identity-line p { margin-top:2px; }.identity-view { margin-left:auto; }
  .test-section { display:grid;grid-template-columns:minmax(0,1fr);gap:8px; }.test-section > div:first-child p { margin-top:3px; }.test-status { display:flex;align-items:center;gap:10px;min-height:49px;flex-wrap:wrap;font-size:12px; }.test-status :global(.button) { margin-left:auto; }
  .qq-links { display:flex;align-items:center;gap:17px;margin-top:11px;color:var(--blue);font-size:12px; }.qq-links a { display:flex;align-items:center;gap:5px; }.qq-links > span { height:16px;width:1px;background:var(--line-strong); }.qq-links .text-button { min-height:30px; }
  .form-grid { display:grid;gap:18px; }.editor-setting { border-top:1px solid var(--line); }.credential-line { display:flex;align-items:center;gap:11px;padding:12px 0;border-top:1px solid var(--line); }.credential-line > div:nth-child(2) { flex:1; }.credential-line strong { font-size:12px; }.credential-line p { margin-top:2px; }
  .spacer { flex:1; }
</style>
