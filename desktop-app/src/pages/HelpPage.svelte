<script lang="ts">
  import { onMount } from 'svelte';
  import { app } from '../app/state.svelte';
  import { helpArticles } from '../preview/fixtures';
  import { execute } from '../bridge/client';
  import Button from '../components/Button.svelte';
  import Icon from '../components/Icon.svelte';
  import PageHeader from '../components/PageHeader.svelte';
  import Pill from '../components/Pill.svelte';
  import Segmented from '../components/Segmented.svelte';
  import QuoteGuide from '../components/QuoteGuide.svelte';
  let { onopen, onconfirm }: { onopen: (page: 'settings' | 'qq' | 'codex') => void; onconfirm: (kind: string) => void } = $props();
  let tab = $state(app.subroute || 'guide');
  let articleId = $state(helpArticles[0].id);
  let search = $state('');
  let checked = $state(false);
  let checking = $state(false);
  let level = $state('all');
  let component = $state('all');
  const activeArticle = $derived(helpArticles.find(article => article.id === articleId)!);
  const visibleArticles = $derived(helpArticles.filter(article => `${article.title} ${article.summary} ${article.paragraphs.join(' ')}`.toLowerCase().includes(search.trim().toLowerCase())));
  const diagnosticRows = $derived([
    { title: '本机环境', detail: '系统、运行时与应用目录', tone: checked && app.checks.codexHome ? 'success' : 'neutral', state: checked ? app.checks.codexHome ? '目录可读' : '需要处理' : '尚未检查' },
    { title: '助手运行', detail: '监听器与助手进程', tone: app.snapshot.runtime === 'ready' ? 'success' : 'warning', state: app.snapshot.runtime === 'ready' ? '运行中' : app.snapshot.runtime === 'stopped' ? '已停止' : '待核对' },
    { title: 'QQ 连接', detail: 'QQ 网关与本地接口', tone: app.snapshot.qq === 'ready' ? 'success' : app.snapshot.qq === 'offline' ? 'danger' : 'warning', state: app.snapshot.qq === 'ready' ? '已连接' : app.snapshot.qq === 'offline' ? '连接中断' : '待核对' },
    { title: 'Codex 连接', detail: 'Desktop 连接与能力检查', tone: app.snapshot.codex === 'ready' ? 'success' : 'warning', state: app.snapshot.codex === 'ready' ? '可用' : app.snapshot.codex === 'limited' ? '控制受限' : '待检查' },
    ...(app.snapshot.hooks === 'unknown' ? [] : [{ title: '提醒钩子', detail: '安装与启用状态', tone: app.snapshot.hooks === 'ready' ? 'success' : 'warning', state: app.snapshot.hooks === 'ready' ? '配置就绪' : '需要处理' }]),
    { title: 'Windows 自启', detail: '实际登录效果另行验证', tone: app.snapshot.startupConfiguredVerified ? 'success' : 'warning', state: app.snapshot.startupConfiguredVerified ? '系统配置已回读' : '系统配置待核对' },
  ]);
  const visibleLogs = $derived((app.preview ? [
    { time: '10:43:02', level: 'INFO', component: 'assistant', text: '状态快照更新完成（示例数据）' },
    { time: '10:42:20', level: 'WARN', component: 'delivery', text: '通知提交结果未确认；未自动重发' },
    { time: '10:42:05', level: 'INFO', component: 'delivery', text: '通知投递阶段已记录（示例数据）' },
    { time: '10:41:48', level: 'INFO', component: 'qq', text: '连接检查完成（未发送 QQ 消息）' },
    { time: '10:40:11', level: 'INFO', component: 'codex', text: '本地能力状态已刷新（示例数据）' },
  ] : app.logs).filter(row => (level === 'all' || row.level.toLowerCase() === level) && (component === 'all' || row.component === component)));
  async function runChecks() {
    checking = true; checked = false;
    try { const result = await execute('run_checks'); if (result.status === 'confirmed') { checked = true; if (result.data?.snapshot) app.applySnapshot(result.data.snapshot); if (result.data?.checks) app.checks = result.data.checks; app.notify(result.preview ? '示例诊断已刷新；没有检查本机系统。' : '诊断检查完成'); } else app.notify(`检查结果待核对（${result.operationId}）`, true); }
    catch { app.notify('无法连接诊断服务，请核对后重新检查。', true); }
    finally { checking = false; }
  }
  async function copySummary() {
    const text = app.preview ? 'Codex QQ 助手 · 设计预览' : JSON.stringify({ observedAt: app.snapshot.observedAt, runtime: app.snapshot.runtime, codex: app.snapshot.codex, qq: app.snapshot.qq, checks: app.checks }, null, 2);
    try { await navigator.clipboard.writeText(text); app.notify('已复制脱敏诊断摘要'); }
    catch { app.notify('无法写入剪贴板，请稍后重试。', true); }
  }
  onMount(() => {
    if (helpArticles.some(article => article.id === app.helpSection)) articleId = app.helpSection;
    app.helpSection = '';
    if (!app.preview) void app.refreshLogs();
  });
  function iconFor(name: string) { return name === 'sparkles' ? 'sparkles' : name === 'link' ? 'link' : name === 'list' ? 'list' : name === 'circle-help' ? 'help' : name === 'users' ? 'users' : 'refresh'; }
</script>

<div class="page">
  <PageHeader title="帮助与诊断" subtitle="本地使用说明与可执行的下一步">
    {#if tab === 'diagnostics'}<Button variant="primary" icon="activity" loading={checking} onclick={() => void runChecks()}>{checking ? '正在检查' : '运行检查'}</Button>
    {:else if tab === 'logs'}<Button variant="primary" icon="file" onclick={() => onconfirm('export')}>{app.preview ? '预览诊断包' : '保存诊断包…'}</Button>{/if}
  </PageHeader>
  <Segmented label="帮助内容" value={tab} onchange={value => tab = value} options={[{value:'guide',label:'使用帮助'},{value:'diagnostics',label:'连接诊断'},{value:'logs',label:'日志与导出'}]}/>

  {#if tab === 'guide'}
    {#if articleId === 'reference' || articleId === 'start'}<QuoteGuide/>{/if}
    <div class="help-search"><label class="visually-hidden" for="help-search">搜索帮助内容</label><div class="search-field"><Icon name="search" size={17}/><input id="help-search" bind:value={search} placeholder="搜索使用说明" /></div></div>
    {#if visibleArticles.length === 0}<section class="surface"><div class="empty-state"><h3>没有找到相关说明</h3><p>试试“引用”“绑定”或“更新”。</p></div></section>
    {:else}
      <div class="help-layout">
        <nav class="help-nav surface" aria-label="帮助目录">
          {#each visibleArticles as article}<button class:active={article.id === articleId} aria-current={article.id === articleId ? 'page' : undefined} onclick={() => articleId = article.id}><Icon name={iconFor(article.icon)} size={17}/>{article.title}</button>{/each}
        </nav>
        <article class="surface panel help-article"><div class="row"><span class="settings-icon"><Icon name={iconFor(activeArticle.icon)} size={18}/></span><span class="eyebrow">使用帮助</span></div><h2 style="margin-top:12px">{activeArticle.title}</h2><p class="muted">{activeArticle.summary}</p>{#each activeArticle.paragraphs as paragraph}<p>{paragraph}</p>{/each}<div class="help-actions">{#if articleId === 'startup'}<Button variant="secondary" onclick={() => onopen('settings')}>打开通用设置</Button>{:else if articleId === 'groups'}<Button variant="secondary" onclick={() => onopen('qq')}>打开 QQ 机器人</Button>{:else if articleId === 'reference' || articleId === 'queue'}<Button variant="secondary" onclick={() => onopen('codex')}>查看 Codex 连接</Button>{/if}</div></article>
      </div>
    {/if}
  {:else if tab === 'diagnostics'}
    <div class="diagnostic-overview surface"><div class="row"><span class="settings-icon"><Icon name="activity" size={18}/></span><div><strong>最近一次检查</strong><p class="muted small">{checked ? app.preview ? '刚刚 · 设计预览' : app.snapshot.observedAt : '未运行本机检查'}</p></div><span class="spacer"></span><Pill tone={app.preview ? "warning" : "info"}>{app.preview ? "前端合成状态" : "本机服务状态"}</Pill></div><p class="muted small diagnostic-note">检查不会发送 QQ 消息；登录自启与 QQ 聊天实收需要单独核对。</p></div>
    <div class="surface diagnostic-list">
      {#each diagnosticRows as row}<div class="diagnostic-row"><span class="status-dot {row.tone}"></span><div><strong>{row.title}</strong><p class="muted small">{row.detail}</p></div><span class="muted small diagnostic-state">{row.state}</span><Icon name="right" size={15} class="muted"/></div>{/each}
    </div>
    <div class="settings-foot"><Button variant="secondary" icon="copy" onclick={() => void copySummary()}>复制脱敏摘要</Button><Button variant="ghost" icon="settings" onclick={() => onopen('settings')}>打开设置</Button></div>
  {:else}
    <div class="log-filters">
      <label class="visually-hidden" for="log-component">日志组件</label><select id="log-component" bind:value={component}><option value="all">全部组件</option><option value="assistant">助手</option><option value="delivery">通知</option><option value="qq">QQ</option><option value="codex">Codex</option></select>
      <label class="visually-hidden" for="log-level">日志级别</label><select id="log-level" bind:value={level}><option value="all">全部级别</option><option value="info">INFO</option><option value="warn">WARN</option><option value="error">ERROR</option></select>
      <span class="muted small">最近记录 · 最多显示 100 条</span><span class="spacer"></span><Button variant="secondary" icon="folder" onclick={() => void app.run('open_known_folder', undefined, { folder: 'logs' })}>打开日志文件夹</Button>
    </div>
    <div class="surface log-list">
      {#each visibleLogs as row}<div class="log-row"><span class="mono muted log-time">{row.time}</span><Pill tone={row.level === 'WARN' ? 'warning' : 'neutral'}>{row.level}</Pill><span class="log-component">{row.component}</span><span class="log-text">{row.text}</span></div>{/each}
      {#if !app.logsLoaded}<div class="empty-state"><h3>日志读取尚未确认</h3><p>重新打开此页核对，或打开日志文件夹查看本机记录。</p></div>
      {:else if visibleLogs.length === 0}<div class="empty-state"><h3>没有匹配的日志</h3><p>调整筛选条件查看其他记录。</p></div>{/if}
    </div>
    <div class="note-banner log-disclaimer"><Icon name="lock" size={17}/><span>仅显示已分类的助手生命周期记录，不含 QQ 正文、凭据或对话历史。</span><Button variant="ghost" onclick={() => onconfirm('export')}>{app.preview ? '预览诊断包' : '保存诊断包…'}</Button></div>
  {/if}
</div>

<script lang="ts" module>
</script>
<style>
  .help-search { max-width:560px;margin:19px 0 17px; }.help-article { min-height:280px; }.help-article > p { max-width:670px; }.help-actions { display:flex;gap:8px;margin-top:20px; }
  .diagnostic-overview { padding:17px 20px;margin-top:19px;border-radius:16px; }.diagnostic-overview strong { font-size:12px; }.diagnostic-note { margin:11px 0 0 45px;line-height:1.65; }.diagnostic-list { padding:3px 18px;margin-top:12px;border-radius:16px; }.diagnostic-row { display:grid;grid-template-columns:11px minmax(0,1fr) minmax(95px,.5fr) 17px;align-items:center;gap:13px;min-height:64px;border-bottom:1px solid var(--line); }.diagnostic-row:last-child { border-bottom:0; }.diagnostic-row strong { font-size:12px; }.diagnostic-row p { margin-top:2px; }.diagnostic-state { text-align:right; }
  .log-filters { display:flex;align-items:center;flex-wrap:wrap;gap:8px;margin:18px 0 12px; }.log-filters select { min-height:36px;padding:0 27px 0 10px;border:1px solid var(--line);border-radius:9px;background:var(--surface); }.log-list { padding:3px 16px;border-radius:15px; }.log-row { display:grid;grid-template-columns:72px 58px 72px minmax(0,1fr);align-items:center;gap:10px;min-height:49px;border-bottom:1px solid var(--line);font-size:11px; }.log-row:last-child { border:0; }.log-time { font-size:10px; }.log-component { color:var(--muted); }.log-text { overflow-wrap:anywhere;color:var(--ink-soft); }.log-disclaimer { margin-top:15px;align-items:center; }.log-disclaimer span { flex:1; }
  .spacer { flex:1; }
  @media(max-width:640px) { .log-row { grid-template-columns:56px 47px 49px minmax(0,1fr);gap:5px;font-size:9px; }.log-time { font-size:9px; }.diagnostic-row { grid-template-columns:8px minmax(0,1fr) auto;gap:8px; }.diagnostic-row :global(svg) { display:none; }.diagnostic-state { max-width:94px;font-size:9px; }.log-filters > span { width:100%; }.log-filters :global(.button) { flex:1; } }
</style>
