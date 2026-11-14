<script lang="ts">
  import { app } from '../app/state.svelte';
  import { execute } from '../bridge/client';
  import type { Preferences } from '../bridge/contracts';
  import Button from '../components/Button.svelte';
  import Icon from '../components/Icon.svelte';
  import PageHeader from '../components/PageHeader.svelte';
  import Segmented from '../components/Segmented.svelte';
  import Toggle from '../components/Toggle.svelte';
  let { onconfirm, onopen }: { onconfirm: (kind: string) => void; onopen: (page: 'updates' | 'help') => void } = $props();
  let tab = $state(app.subroute || 'appearance');
  let startupBusy = $state(false);
  let startupRepairOpen = $state(false);
  const tabs = [{ value: 'general', label: '通用' }, { value: 'appearance', label: '外观' }, { value: 'data', label: '数据与隐私' }];

  async function setStartup(value: boolean) {
    if (startupBusy) return;
    startupBusy = true;
    try {
      const result = await execute('set_startup', { enabled: value });
      if (result.status !== 'confirmed') { app.notify('自启项修改结果尚未确认，请先重新核对。'); return; }
      if (result.preview) app.snapshot.startup = value;
      else if (result.data?.snapshot) app.applySnapshot(result.data.snapshot);
      app.notify(result.preview ? '预览已更新；没有修改 Windows 自启项。' : '自启配置已回读。实际登录效果尚待验证。');
    } catch { app.notify('无法核对 Windows 实际启动项。', true); }
    finally { startupBusy = false; }
  }

  async function refreshStartup() {
    try {
      const result = await execute('get_snapshot');
      if (result.status !== 'confirmed') { app.notify('系统自启状态尚未确认。', true); return; }
      if (result.preview) app.notify('预览不会读取 Windows 自启状态。');
      else if (result.data?.snapshot) {
        app.applySnapshot(result.data.snapshot);
        app.notify('已回读 Windows 登录启动状态。实际登录效果尚待验证。');
      } else app.notify('系统自启状态回执缺少快照。', true);
    } catch { app.notify('无法读取 Windows 登录启动状态。', true); }
  }
</script>

<div class="page">
  <PageHeader title="设置" subtitle="仅管理 Codex-QQ 自身的外观与运行偏好" />
  <Segmented label="设置分类" value={tab} onchange={(value) => tab = value} options={tabs} />

  {#if tab === 'appearance'}
    <section class="page-section">
      <div class="section-heading"><h2>主题</h2></div>
      <div class="choice-grid theme-choices">
        {#each [{ value: 'light', label: '浅色' },{ value: 'dark', label: '深色' },{ value: 'system', label: '跟随系统' }] as theme}
          <button class="theme-choice" class:selected={app.preferences.theme === theme.value} aria-pressed={app.preferences.theme === theme.value} onclick={() => app.preference('theme', theme.value as Preferences['theme'])}>
            <span class="theme-preview {theme.value}">{#if app.preferences.theme === theme.value}<span class="theme-check"><Icon name="check" size={16}/></span>{/if}</span><span>{theme.label}</span>
          </button>
        {/each}
      </div>
      <div class="theme-segment"><Segmented label="选择主题" value={app.preferences.theme} onchange={(value) => app.preference('theme', value as Preferences['theme'])} options={tabsTheme}/></div>
    </section>

    <section class="page-section">
      <div class="section-heading"><h2>材质与动效</h2><span class="muted small">即时预览</span></div>
      <div class="surface settings-list">
        <div class="settings-row"><span class="settings-icon"><Icon name="layers" size={18}/></span><div class="settings-copy"><strong>透明效果</strong><span>为导航与浮层呈现通透的玻璃层次</span></div><Toggle checked={app.preferences.transparency} label="透明效果" onchange={(v) => app.preference('transparency', v)}/></div>
        <div class="settings-row"><span class="settings-icon"><Icon name="sparkles" size={18}/></span><div class="settings-copy"><strong>动态效果</strong><span>使用平滑转场与细腻的交互反馈</span></div><Toggle checked={app.preferences.motion} label="动态效果" onchange={(v) => app.preference('motion', v)}/></div>
        <div class="settings-row quality-row"><span class="settings-icon"><Icon name="monitor" size={18}/></span><div class="settings-copy"><strong>效果质量</strong><span>自动选择适合当前设备的材质复杂度</span></div><Segmented label="效果质量" value={app.preferences.quality} onchange={(v) => app.preference('quality', v as Preferences['quality'])} options={qualityOptions}/></div>
      </div>
      <div class="settings-foot"><Button variant="secondary" icon="reset" onclick={() => app.resetAppearance()}>恢复外观默认值</Button><span class="muted small">系统减少动态效果与高对比度设置优先。</span></div>
    </section>

  {:else if tab === 'general'}
    <section class="page-section">
      <div class="section-heading"><h2>启动与窗口</h2></div>
      <div class="surface settings-list">
        <div class="settings-row"><span class="settings-icon"><Icon name="power" size={18}/></span><div class="settings-copy"><strong>登录 Windows 后启动</strong><span>关闭窗口时助手仍会在托盘运行</span></div><Toggle checked={app.snapshot.startup} label="登录 Windows 后启动" disabled={startupBusy} onchange={(value) => void setStartup(value)}/></div>
        <div class="settings-row"><span class="settings-icon"><Icon name="laptop" size={18}/></span><div class="settings-copy"><strong>手动启动时显示</strong><span>登录自启仍只驻留托盘</span></div><Segmented label="手动启动行为" value={app.preferences.startupView} onchange={(v) => app.preference('startupView', v as Preferences['startupView'])} options={[{value:'overview',label:'打开概览'},{value:'tray',label:'驻留托盘'}]}/></div>
        <div class="settings-row"><span class="settings-icon"><Icon name="eye" size={18}/></span><div class="settings-copy"><strong>关闭窗口的行为</strong><span>关闭窗口后继续监听和投递</span></div><span class="muted small">继续驻留托盘</span></div>
      </div>
    </section>
    <section class="page-section">
      <div class="section-heading"><h2>Windows 自启核对</h2><Button variant="ghost" icon="refresh" onclick={() => void refreshStartup()}>重新核对</Button></div>
      <div class="surface settings-list">
        <div class="settings-row"><span class="settings-icon"><Icon name="file" size={18}/></span><div class="settings-copy"><strong>启动命令</strong><span>应用内设置与 Windows 系统视角分别核对</span></div><span class="muted small">{app.preview ? '预览 · ' : '系统 · '}{app.snapshot.startup ? '已配置，登录效果待验证' : '尚未启用'}</span></div>
        <div class="settings-row"><span class="settings-icon"><Icon name="shield" size={18}/></span><div class="settings-copy"><strong>Windows 启用状态</strong><span>{app.snapshot.startupVerified ? '已观察到正常登录后自动启动，助手与 QQ 均就绪' : '实际登录启动需要下次正常登录后验证'}</span></div><span class="muted small">{app.snapshot.startupEnabled ? 'Windows 已启用' : app.snapshot.startup ? '系统状态待核对' : 'Windows 未启用'}</span></div>
      </div>
      <div class="settings-foot"><Button variant="secondary" onclick={() => startupRepairOpen = true}>修复本应用启动项…</Button><Button variant="ghost" onclick={() => onconfirm('reset-window')}>重置窗口布局</Button></div>
      {#if startupRepairOpen}<div class="inline-warning"><Icon name="info" size={17}/><span>修复前会核验启动项是否属于 Codex-QQ，并与当前可执行文件路径比较。</span><Button variant="secondary" onclick={() => startupRepairOpen = false}>取消</Button><Button variant="primary" onclick={() => { startupRepairOpen = false; onconfirm('repair-startup'); }}>继续核验</Button></div>{/if}
    </section>
    <section class="page-section">
      <div class="section-heading"><h2>更新偏好</h2><Button variant="ghost" onclick={() => onopen('updates')}>前往更新设置 <Icon name="right" size={16}/></Button></div>
      <div class="note-banner"><Icon name="info" size={17}/><span>启动时检查更新：{app.preferences.checkUpdates ? '开启' : '关闭'} · 自动安装：{app.preferences.automaticUpdates ? '开启，空闲时应用' : '关闭'}</span></div>
    </section>

  {:else}
    <section class="page-section">
      <div class="section-heading"><h2>本机数据</h2><span class="muted small">{app.preview ? '示例路径' : '当前本机路径'}</span></div>
      <div class="surface path-list">
        {#each [{icon:'package',title:'应用程序',path:app.snapshot.installPath || '%LOCALAPPDATA%\\Programs\\CodexQQ'},{icon:'folder',title:'助手数据',path:app.snapshot.appDataPath || '%LOCALAPPDATA%\\CodexDesktopWeixin'},{icon:'folder',title:'QQ 网关数据',path:app.snapshot.qqDataPath || '%LOCALAPPDATA%\\CodexCC'}] as item}
          <div class="path-row"><span class="settings-icon"><Icon name={item.icon} size={17}/></span><div><strong>{item.title}</strong><p class="mono muted">{item.path}</p></div><Button variant="ghost" icon="copy" aria-label="复制{item.title}路径" onclick={() => void navigator.clipboard.writeText(item.path).then(() => app.notify('路径已复制')).catch(() => app.notify('无法复制路径', true))}></Button><Button variant="ghost" icon="folder" aria-label="打开{item.title}文件夹" onclick={() => void app.run('open_known_folder', undefined, { folder: item.title === '应用程序' ? 'install' : item.title === '助手数据' ? 'app' : 'qq' })}></Button></div>
        {/each}
      </div>
    </section>
    <section class="page-section">
      <div class="section-heading"><h2>保留与隐私</h2></div>
      <div class="surface settings-list">
        <div class="settings-row"><span class="settings-icon"><Icon name="bell" size={17}/></span><div class="settings-copy"><strong>通知记录</strong><span>按助手现行保留策略维护；不在设置页重试或清空</span></div><span class="muted small">自动管理</span></div>
        <div class="settings-row"><span class="settings-icon"><Icon name="lock" size={17}/></span><div class="settings-copy"><strong>诊断内容</strong><span>导出前预览并脱敏；不会自动上传</span></div><Button variant="secondary" onclick={() => onopen('help')}>查看诊断</Button></div>
      </div>
    </section>
  {/if}
</div>

<script lang="ts" module>
  const tabsTheme = [{ value: 'light', label: '浅色' }, { value: 'dark', label: '深色' }, { value: 'system', label: '跟随系统' }];
  const qualityOptions = [{ value: 'auto', label: '自动' }, { value: 'fine', label: '精致' }, { value: 'light', label: '轻盈' }];
</script>

<style>
  .theme-choices { margin:15px 12px 13px; }.theme-segment { width:min(640px,100%); }.theme-segment :global(.segmented) { width:100%; }
  .settings-foot { display:flex;align-items:center;gap:14px;flex-wrap:wrap;margin-top:15px; }.path-list { display:grid;padding:3px 17px;border-radius:16px; }.path-row { display:grid;grid-template-columns:34px minmax(0,1fr) 36px 36px;align-items:center;gap:10px;min-height:70px;border-bottom:1px solid var(--line); }.path-row:last-child { border-bottom:0; }.path-row strong { font-size:12px; }.path-row p { margin-top:2px;overflow-wrap:anywhere;font-size:10px; }
  .inline-warning { display:flex;align-items:center;gap:10px;flex-wrap:wrap;padding:12px 14px;margin-top:12px;border:1px solid var(--line);border-radius:12px;background:var(--warning-wash);font-size:11px; }.inline-warning span { flex:1;min-width:220px; }
  @media(max-width:640px) { .theme-choices { margin:12px 0; }.theme-segment :global(.segmented) { min-height:37px; }.quality-row { grid-template-columns:29px minmax(0,1fr);padding:10px 0; }.quality-row :global(.segmented) { grid-column:2; }.settings-foot > span { flex-basis:100%; }.path-row { grid-template-columns:30px minmax(0,1fr) 31px 31px;gap:5px; }.path-row :global(.button) { width:31px;min-width:31px;padding:0; }.settings-row > :global(.segmented) { grid-column:2/-1;margin-bottom:10px; } }
</style>
