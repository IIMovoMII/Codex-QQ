<script lang="ts">
  import { app } from '../app/state.svelte';
  import Button from '../components/Button.svelte';
  import Icon from '../components/Icon.svelte';
  import PageHeader from '../components/PageHeader.svelte';
  import Pill from '../components/Pill.svelte';
  let { onopen }: { onopen: (page: 'overview' | 'help') => void } = $props();
  let phase = $state('idle');
  let busy = $state(false);
  async function migrate() {
    if (!app.snapshot.migrationRequired && !app.preview) { phase = 'current'; return; }
    busy = true; phase = 'checking';
    try {
      const ok = await app.run('recover_setup');
      phase = ok ? app.preview ? 'preview' : 'ready' : 'unknown';
    }
    finally { busy = false; }
  }
</script>

<div class="page">
  <PageHeader title="安装迁移与恢复" subtitle="保留现有 QQ 绑定和运行数据，核对新旧路径">
    <Button variant="ghost" icon="back" onclick={() => onopen('overview')}>返回概览</Button>
  </PageHeader>
  <section class="surface recovery-hero"><span class="setup-icon"><Icon name="package" size={30}/></span><div><h2>{phase === 'ready' ? '桌面助手已迁移并启动' : phase === 'current' ? '安装路径已是最新' : phase === 'unknown' ? '迁移结果待核对' : app.snapshot.migrationRequired ? '需要迁移旧版运行路径' : '桌面安装恢复'}</h2><p class="muted small">迁移时会先备份现有设置，只更新 QQ 网关程序和 Codex 桥接命令路径，保留机器人凭据、身份绑定及其他配置。</p></div><Pill tone={phase === 'ready' || phase === 'current' ? 'success' : phase === 'unknown' ? 'warning' : 'neutral'}>{phase === 'ready' ? '迁移完成' : phase === 'current' ? '无需迁移' : phase === 'unknown' ? '结果未知' : app.snapshot.migrationRequired ? '等待迁移' : '待确认'}</Pill></section>
  <section class="page-section">
    <div class="section-heading"><h2>路径与运行组件</h2><Button variant="primary" icon="refresh" loading={busy} disabled={!app.snapshot.migrationRequired && !app.preview} onclick={() => void migrate()}>{busy ? '正在迁移并启动' : app.preview ? '模拟检查' : app.snapshot.migrationRequired ? '迁移并启动助手' : '无需迁移'}</Button></div>
    <div class="surface migration-list">
      {#each [{icon:'package',title:'助手程序',old:'独立 Windows 桌面应用',new:app.snapshot.installPath || '安装路径待读取'},{icon:'bot',title:'QQ 网关',old:'当前机器人配置',new:app.snapshot.migrationRequired ? '确认后切换到随包运行组件' : '使用当前安装组件'},{icon:'code',title:'Codex 桥接命令',old:'仅更新本应用关联的命令',new:'身份、凭据及其他配置保留'},{icon:'power',title:'Windows 登录自启',old:app.snapshot.startup?'启动入口已核对':'启动入口需要更新',new:app.snapshot.startupEnabled?'Windows 标记为启用':'登录启动状态待核验'}] as item}<div class="migration-row"><span class="settings-icon"><Icon name={item.icon} size={18}/></span><strong>{item.title}</strong><span class="muted small">{item.old}</span><Icon name="right" size={16} class="muted"/><span>{item.new}</span>{#if phase === 'preview'}<Pill tone="warning">模拟</Pill>{:else if phase === 'ready' || phase === 'current'}<Pill tone="success">已确认</Pill>{:else}<Pill tone="neutral">待核验</Pill>{/if}</div>{/each}
    </div>
  </section>
  <section class="page-section note-banner"><Icon name="shield" size={18}/><span>{phase === 'preview' ? '浏览器预览没有修改本机设置或启动服务。' : '迁移会短暂停止 QQ 助手以安全切换运行路径；Codex Desktop 与其他配置不变，并在确认回读后启动新助手。'}</span></section>
  {#if phase === 'unknown'}<div class="error-banner" role="alert"><Icon name="warning" size={18}/>{app.error || '当前无法确认迁移状态。请保留现有安装与配置并查看诊断。'}<Button variant="ghost" onclick={() => onopen('help')}>查看诊断</Button></div>{/if}
</div>

<style>
  .recovery-hero { display:grid;grid-template-columns:72px minmax(0,1fr) auto;align-items:center;gap:16px;padding:18px 21px;border-radius:18px; }.recovery-hero .setup-icon { width:58px;height:58px;margin:0;border-radius:17px; }.recovery-hero h2 { font-size:16px; }.recovery-hero p { margin-top:4px;line-height:1.7; }.migration-list { padding:4px 17px;border-radius:16px; }.migration-row { display:grid;grid-template-columns:32px minmax(130px,1.1fr) minmax(105px,.8fr) 16px minmax(150px,1.1fr) auto;align-items:center;gap:11px;min-height:64px;border-bottom:1px solid var(--line);font-size:11px; }.migration-row:last-child { border:0; }.migration-row > strong { font-size:12px; }.migration-row > span:nth-last-child(2) { color:var(--ink-soft); }.page-section.note-banner { align-items:center; }.page-section.note-banner > span { line-height:1.7; }
  @media(max-width:760px) { .migration-row { grid-template-columns:30px minmax(0,1fr) auto;gap:7px;min-height:69px; }.migration-row > span:nth-child(3),.migration-row > :global(svg):nth-child(4) { display:none; }.migration-row > span:nth-child(5) { grid-column:2;grid-row:2; }.migration-row > :last-child { grid-column:3;grid-row:1/3; }.recovery-hero { grid-template-columns:48px minmax(0,1fr);padding:14px; }.recovery-hero .setup-icon { width:43px;height:43px; }.recovery-hero :global(.pill) { grid-column:2; } }
</style>
