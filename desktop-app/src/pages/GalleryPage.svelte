<script lang="ts">
  import { app } from '../app/state.svelte';
  import type { Page, Scenario } from '../bridge/contracts';
  import Button from '../components/Button.svelte';
  import Icon from '../components/Icon.svelte';
  import PageHeader from '../components/PageHeader.svelte';
  import Pill from '../components/Pill.svelte';
  import Segmented from '../components/Segmented.svelte';
  let { onopen }: { onopen: (page: Page) => void } = $props();
  const pages: { title: string; page: Page; icon: string; note: string }[] = [
    {title:'概览',page:'overview',icon:'home',note:'主状态、通知和手机实收说明'},
    {title:'QQ 机器人',page:'qq',icon:'bot',note:'凭据状态、连接、身份和配置抽屉'},
    {title:'Codex 连接',page:'codex',icon:'link',note:'连接拓扑、能力与提醒钩子'},
    {title:'通知记录',page:'notifications',icon:'bell',note:'投递筛选与结果详情抽屉'},
    {title:'外观设置',page:'settings',icon:'palette',note:'浅深主题、玻璃和动效控件'},
    {title:'更新',page:'updates',icon:'refresh',note:'版本、发布状态与更新偏好'},
    {title:'帮助与诊断',page:'help',icon:'help',note:'使用说明、只读检查与脱敏记录'},
    {title:'首次绑定',page:'setup',icon:'users',note:'步骤导航、OpenID 表单与确认'},
    {title:'迁移与恢复',page:'recovery',icon:'package',note:'安装路径、组件与自启核对'},
  ];
</script>

<div class="page">
  <PageHeader title="组件展厅" subtitle="设计预览工具 · 切换合成状态并查看页面交互">
    <Pill tone="info">仅浏览器预览</Pill>
  </PageHeader>
  <section class="gallery-tool surface">
    <div><h2>示例状态</h2><p class="muted small">更改只影响当前页面的合成样本，不读本机服务。</p></div>
    <Segmented label="预览状态" value={app.scenario} onchange={(value) => app.setScenario(value as Scenario)} options={[{value:'ready',label:'就绪'},{value:'offline',label:'QQ 离线'},{value:'stopped',label:'已停止'},{value:'unknown',label:'未知'},{value:'empty',label:'空通知'},{value:'unconfigured',label:'未配置'}]}/>
    <div class="row gallery-state"><Pill tone={app.scenario === 'ready' ? 'success' : app.scenario === 'unknown' ? 'warning' : app.scenario === 'offline' ? 'danger' : 'neutral'}>助手：{app.snapshot.runtime === 'ready' ? '运行中（示例）' : app.snapshot.runtime === 'stopped' ? '已停止（示例）' : '未知（示例）'}</Pill><Pill tone={app.snapshot.qq === 'ready' ? 'success' : app.snapshot.qq === 'offline' ? 'danger' : 'warning'}>QQ：{app.snapshot.qq === 'ready' ? '已连接（示例）' : app.snapshot.qq === 'offline' ? '离线（示例）' : '待核对（示例）'}</Pill><Pill tone="neutral">{app.deliveries.length} 条合成通知</Pill></div>
  </section>
  <div class="gallery-nav-grid">{#each pages as item}<button class="gallery-page-card surface" onclick={() => onopen(item.page)}><span class="settings-icon"><Icon name={item.icon} size={20}/></span><span><strong>{item.title}</strong><small>{item.note}</small></span><Icon name="right" size={17}/></button>{/each}</div>
  <section class="page-section">
    <div class="section-heading"><h2>动效与降级</h2></div>
    <div class="gallery-grid"><div class="gallery-card"><span class="eyebrow">材质状态</span><div class="glass gallery-glass">局部 WebGL 折射</div><small class="muted">自绘光场 · 静止时按需渲染</small></div><div class="gallery-card"><span class="eyebrow">系统辅助</span><div class="row"><Icon name="eye" size={17}/><span>减少透明度</span></div><div class="row"><Icon name="activity" size={17}/><span>减少动态效果</span></div><small class="muted">设置页可切换主题和效果质量</small></div><div class="gallery-card"><span class="eyebrow">UI 状态</span><div class="row"><Pill tone="success">已确认</Pill><Pill tone="warning">结果未知</Pill></div><small class="muted">状态包含文字与图标，不仅依赖颜色</small></div></div>
  </section>
</div>

<style>
  .gallery-tool { display:grid;gap:13px;padding:17px 20px;border-radius:16px; }.gallery-tool h2 { font-size:14px; }.gallery-tool p { margin-top:3px; }.gallery-state { flex-wrap:wrap; }.gallery-nav-grid { display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px;margin-top:16px; }.gallery-page-card { display:grid;grid-template-columns:35px minmax(0,1fr) 17px;align-items:center;gap:11px;min-height:65px;padding:10px 13px;border-radius:13px;color:var(--ink-soft);text-align:left;cursor:pointer;transition:transform 160ms,border-color 160ms; }.gallery-page-card:hover { transform:translateY(-1px);border-color:color-mix(in srgb,var(--blue) 36%,var(--line)); }.gallery-page-card > span:nth-child(2) { display:grid;gap:2px; }.gallery-page-card strong { color:var(--ink);font-size:12px; }.gallery-page-card small { color:var(--muted);font-size:10px; }.gallery-glass { min-height:42px;padding:9px 12px;border-radius:13px;font-size:11px; }.gallery-card { min-height:140px; }
  @media(max-width:640px) { .gallery-tool :global(.segmented) { flex-wrap:wrap; }.gallery-tool :global(.segmented > button) { flex-basis:28%; }.gallery-nav-grid { grid-template-columns:minmax(0,1fr); } }
</style>
