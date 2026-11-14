<script lang="ts">
  import { onMount } from 'svelte';
  import { app } from './app/state.svelte';
  import { execute, exitApplication, isNative, windowAction, resetWindow } from './bridge/client';
  import type { Command, Page } from './bridge/contracts';
  import Button from './components/Button.svelte';
  import Icon from './components/Icon.svelte';
  import Overlay from './components/Overlay.svelte';
  import GlassLight from './rendering/GlassLight.svelte';
  import Overview from './pages/Overview.svelte';
  import QQPage from './pages/QQPage.svelte';
  import CodexPage from './pages/CodexPage.svelte';
  import NotificationsPage from './pages/NotificationsPage.svelte';
  import SettingsPage from './pages/SettingsPage.svelte';
  import UpdatesPage from './pages/UpdatesPage.svelte';
  import HelpPage from './pages/HelpPage.svelte';
  import SetupPage from './pages/SetupPage.svelte';
  import RecoveryPage from './pages/RecoveryPage.svelte';
  import GalleryPage from './pages/GalleryPage.svelte';

  let activeOverlay = $state('');
  let working = $state(false);
  let pageElement: HTMLElement;
  const specialLayout = $derived(app.page === 'setup');
  const runtimeTone = $derived(app.snapshot.runtime === 'ready' ? 'success' : app.snapshot.runtime === 'stopped' ? 'warning' : 'warning');
  const confirmation = $derived.by(() => {
    const entries: Record<string, { title: string; body: string; action: string; icon: string; command?: Command }> = {
      start: { title: '启动助手', body: '启动本应用监听器与已配置的 QQ 网关。', action: '启动助手', icon: 'play', command: 'start_runtime' },
      stop: { title: '停止助手', body: '这会停止 Codex QQ 助手监听与其 QQ 网关。Codex Desktop 会继续运行。', action: '停止助手', icon: 'power', command: 'stop_runtime' },
      restart: { title: '重启助手', body: '将重启此应用拥有的监听器和 QQ 网关。未确认的通知状态会继续保留。', action: '重启助手', icon: 'refresh', command: 'restart_runtime' },
      exit: { title: '退出应用', body: '退出后 QQ 助手会停止。只关闭窗口则会继续在托盘运行。', action: '退出应用', icon: 'logout' },
      'install-hooks': { title: '安装提醒钩子', body: '只合并 Codex QQ 助手自己的提醒钩子，并启用 hooks 功能。其他配置内容保持原样。', action: '确认安装', icon: 'shield', command: 'install_hooks' },
      'trust-hooks': { title: '在 Codex 中确认信任', body: '请在 Codex 界面核对当前助手的钩子命令。本机文件存在不代表已经信任。', action: '打开 Codex', icon: 'shield', command: 'open_codex' },
      'test-message': { title: '发送 QQ 测试消息', body: '测试消息会发到已配置的接收会话：“Codex-QQ 连接测试”。请确认发送对象正确。', action: '发送测试消息', icon: 'send', command: 'send_test_message' },
      rebind: { title: '更换 QQ 绑定', body: `当前授权身份：${app.snapshot.identity}。继续会进入身份核验；新绑定完成前保留现有身份。`, action: '进入绑定核验', icon: 'users' },
      'repair-startup': { title: '修复本应用启动项', body: '先核验 Windows 系统视角的启动命令、启用状态和应用路径。不会修改其他程序的启动项。', action: '核验并修复', icon: 'power', command: 'repair_startup' },
      'apply-update': { title: '重启助手并安装更新', body: '更新只替换 Codex QQ 助手及随包网关，不关闭 Codex Desktop。', action: '重启并安装', icon: 'download', command: 'apply_update' },
      'reset-window': { title: '重置窗口布局', body: '窗口位置、尺寸和侧栏状态将恢复默认值。其他偏好不变。', action: '恢复默认布局', icon: 'reset' },
      export: { title: '保存脱敏诊断包', body: '内容范围：组件检查结果与最近最多 100 条已分类的助手生命周期记录。不会包含凭据、QQ 正文、对话历史或个人路径；仅保存到本机。', action: '保存诊断包', icon: 'file', command: 'export_diagnostics' },
    };
    return entries[activeOverlay] || entries.export;
  });

  const primaryNav: { page: Page; label: string; icon: string }[] = [
    { page: 'overview', label: '概览', icon: 'home' },
    { page: 'qq', label: 'QQ 机器人', icon: 'bot' },
    { page: 'codex', label: 'Codex 连接', icon: 'link' },
    { page: 'notifications', label: '通知记录', icon: 'bell' },
  ];
  const bottomNav: { page: Page; label: string; icon: string; sub?: string }[] = [
    { page: 'settings', label: '设置', icon: 'settings', sub: 'general' },
    { page: 'updates', label: '更新', icon: 'refresh' },
    { page: 'help', label: '帮助与诊断', icon: 'help', sub: 'guide' },
  ];

  function parseLocation() {
    const parts = location.hash.replace(/^#\/?/, '').split('/').filter(Boolean);
    const candidate = (parts[0] || 'overview') as Page;
    const allowed: Page[] = ['overview','qq','codex','notifications','settings','updates','help','setup','recovery','gallery'];
    app.page = allowed.includes(candidate) && !(isNative && candidate === 'gallery') ? candidate : 'overview';
    app.subroute = parts[1] || '';
  }
  function navigate(page: Page, sub = '') {
    app.navigate(page, sub);
    activeOverlay = '';
    document.querySelector('.main-scroll')?.scrollTo({ top: 0, behavior: app.preferences.motion ? 'smooth' : 'instant' });
  }
  function go(page: Page, sub = '') { navigate(page, sub); }
  function closeOverlay() { if (!working) activeOverlay = ''; }

  async function act() {
    const kind = activeOverlay;
    const config = confirmation;
    if (working) return;
    if (kind === 'exit' && isNative) {
      working = true;
      try {
        await exitApplication();
        activeOverlay = '';
      } catch {
        app.notify('无法确认桌面应用已退出。', true);
      } finally { working = false; }
      return;
    }
    if (!config.command) {
      activeOverlay = '';
      if (kind === 'rebind') {
        if (app.preview || await app.run('begin_rebind')) navigate('setup');
      }
      else if (kind === 'reset-window') { await resetWindow(); app.notify(app.preview ? '预览没有原生窗口可重置。' : '窗口已恢复默认布局。'); }
      else if (kind === 'export') app.notify('这是设计预览；没有生成诊断包。');
      return;
    }
    working = true;
    try {
      const result = await app.run(config.command, () => {
        if (kind === 'stop' || kind === 'exit') app.snapshot.runtime = 'stopped';
        if (kind === 'start' || kind === 'restart' || kind === 'apply-update') app.snapshot.runtime = 'ready';
        if (kind === 'install-hooks') app.snapshot.hooks = 'ready';
        if (kind === 'test-message') app.snapshot.receivedByUser = false;
        if (kind === 'repair-startup') { app.snapshot.startup = true; app.snapshot.startupVerified = false; }
      });
      if (kind === 'apply-update') app.update.status = result ? 'installing' : 'failure';
      if (result) {
        activeOverlay = '';
        if (kind === 'stop') app.notify(app.preview ? '预览已停止示例助手；真实服务未受影响。' : '助手已停止。');
        else if (kind === 'exit') app.notify(app.preview ? '预览操作完成；没有退出桌面应用或助手进程。' : '正在退出助手。');
        else if (kind === 'start') app.notify(app.preview ? '示例状态已更新；没有启动真实助手服务。' : '助手启动检查完成。');
        else if (kind === 'restart') app.notify(app.preview ? '示例助手状态已更新；没有重启服务。' : '助手重启检查完成。');
        else if (kind === 'install-hooks') app.notify(app.preview ? '预览显示钩子已安装；没有修改 Codex 配置。' : '提醒钩子已写入并回读。');
        else if (kind === 'trust-hooks') app.notify('请在电脑上的 Codex 中核对信任状态。');
        else if (kind === 'test-message') app.notify(app.preview ? '预览未发送 QQ 消息；手机实收仍未验证。' : '测试消息提交状态已返回；手机实收需要单独核对。');
        else if (kind === 'repair-startup') app.notify(app.preview ? '预览完成；Windows 自启项没有被修改。' : '已回读系统启动项；登录启动效果尚待实际核验。');
        else if (kind === 'apply-update') app.notify(app.preview ? '预览已展示更新完成状态；没有安装文件。' : '助手更新操作已返回。');
      } else if (app.error) activeOverlay = '';
    } finally { working = false; }
  }

  async function nativeWindow(action: 'minimize' | 'toggleMaximize' | 'close') {
    if (!isNative) { app.notify('窗口控制按钮仅在 Tauri 桌面应用中生效。'); return; }
    try { await windowAction(action); }
    catch { app.notify('无法执行窗口操作。', true); }
  }

  onMount(() => {
    let cancelled = false;
    let unlistenTray: (() => void) | undefined;
    let unlistenHidden: (() => void) | undefined;
    parseLocation();
    window.addEventListener('hashchange', parseLocation);
    if (isNative) {
      void import('@tauri-apps/api/event').then(({ listen }) => listen('window-hidden', () => {
        window.dispatchEvent(new Event('app-window-hidden'));
        if (app.page === 'setup') {
          void app.run('cancel_pairing').then(() => app.navigate('overview'));
        }
      })).then(unlisten => { if (cancelled) unlisten(); else unlistenHidden = unlisten; });
      void import('@tauri-apps/api/event').then(({ listen }) =>
        listen('tray-refresh', () => { void app.refresh(); })
      ).then((unlisten) => {
        if (cancelled) unlisten();
        else unlistenTray = unlisten;
      }).catch(() => app.notify('无法接收托盘状态检查请求。', true));
    }
    if (!location.hash) app.navigate(app.preview && app.scenario === 'unconfigured' ? 'setup' : 'overview');
    void app.initialize().then(() => {
      if (app.snapshot.migrationRequired) app.navigate('recovery');
      else if (isNative && app.loaded && (!app.snapshot.configured || app.setup.step < 8)) app.navigate('setup');
      void app.automaticUpdateTick();
    });
    const poll = setInterval(() => {
      if (isNative && app.loaded && !document.hidden && !app.pending.length && app.page !== 'setup') void app.refresh(true);
      void app.automaticUpdateTick();
    }, 15000);
    const openExternal = (event: MouseEvent) => {
      const anchor = (event.target as Element).closest('a[href]') as HTMLAnchorElement | null;
      if (isNative && anchor?.href.startsWith('https:')) { event.preventDefault(); void app.run('open_allowed_url', undefined, { url: anchor.href }); }
    };
    document.addEventListener('click', openExternal);
    return () => {
      cancelled = true;
      unlistenTray?.();
      unlistenHidden?.();
      clearInterval(poll);
      document.removeEventListener('click', openExternal);
      window.removeEventListener('hashchange', parseLocation);
    };
  });

  $effect(() => {
    const theme = app.preferences.theme;
    const transparency = app.preferences.transparency;
    document.documentElement.dataset.theme = theme;
    document.documentElement.dataset.transparent = String(transparency);
  });
  $effect(() => {
    const current = app.page;
    if (!pageElement) return;
    const heading = pageElement.querySelector('h1');
    const transition = pageElement.animate(
      [{ opacity: .72, transform: 'translateY(8px)' }, { opacity: 1, transform: 'translateY(0)' }],
      { duration: app.preferences.motion ? 220 : 0, easing: 'cubic-bezier(.215,.61,.355,1)' }
    );
    if (heading) requestAnimationFrame(() => heading.focus({ preventScroll: true }));
    void current;
    return () => transition.cancel();
  });
</script>

<svelte:head>
  <title>Codex QQ 助手</title>
  <meta name="description" content="Codex 与 QQ 机器人助手桌面应用" />
</svelte:head>

<div class="app-window" class:native-window={isNative}>
  <div class="ambient" aria-hidden="true"></div>
  <GlassLight />
  {#if !isNative}<header class="app-titlebar">
    <div class="brand"><span class="brand-mark"><Icon name="bot" size={20}/></span><span>Codex QQ 助手</span></div>
    <span class="window-spacer"></span>
    {#if !isNative}<button class="preview-chip" title="打开前端状态展厅" onclick={() => navigate('gallery')}><Icon name="sparkles" size={13}/>设计预览</button>{/if}
    <div class="window-controls" aria-label="窗口控制">
      <button class="window-control" aria-label="最小化窗口" onclick={() => void nativeWindow('minimize')}><Icon name="minus" size={16}/></button>
      <button class="window-control" aria-label="最大化或还原窗口" onclick={() => void nativeWindow('toggleMaximize')}><Icon name="square" size={14}/></button>
      <button class="window-control close" aria-label="关闭窗口" onclick={() => void nativeWindow('close')}><Icon name="close" size={17}/></button>
    </div>
  </header>{/if}

  <div class:full-layout={specialLayout} class="app-body">
    {#if !specialLayout}
      <aside class="sidebar">
        <nav class="nav-group" aria-label="主导航">
          {#each primaryNav as item (item.page)}<button class="nav-item" class:active={app.page === item.page} aria-current={app.page === item.page ? 'page' : undefined} onclick={() => navigate(item.page)}><Icon name={item.icon} size={19}/><span>{item.label}</span></button>{/each}
        </nav>
        <nav class="nav-group nav-bottom" aria-label="设置和帮助">
          {#each bottomNav as item (item.page)}<button class="nav-item" class:active={app.page === item.page} aria-current={app.page === item.page ? 'page' : undefined} onclick={() => navigate(item.page, item.sub)}><Icon name={item.icon} size={19}/><span>{item.label}</span></button>{/each}
        </nav>
        <div class="sidebar-status"><span class="status-dot {runtimeTone}"></span>{app.snapshot.runtime === 'ready' ? '助手运行中' : app.snapshot.runtime === 'stopped' ? '助手已停止' : '状态待核对'}</div>
      </aside>
    {/if}

    <main class="main-scroll" id="main-content">
      <div bind:this={pageElement}>
        {#if app.page === 'overview'}<Overview onconfirm={(kind) => activeOverlay = kind} onopen={navigate}/>
        {:else if app.page === 'qq'}<QQPage onconfirm={(kind) => activeOverlay = kind} onopen={navigate}/>
        {:else if app.page === 'codex'}<CodexPage onconfirm={(kind) => activeOverlay = kind} onopen={navigate}/>
        {:else if app.page === 'notifications'}<NotificationsPage />
        {:else if app.page === 'settings'}<SettingsPage onconfirm={(kind) => activeOverlay = kind} onopen={navigate}/>
        {:else if app.page === 'updates'}<UpdatesPage onconfirm={(kind) => activeOverlay = kind} onopen={navigate}/>
        {:else if app.page === 'help'}<HelpPage onconfirm={(kind) => activeOverlay = kind} onopen={navigate}/>
        {:else if app.page === 'setup'}<SetupPage onconfirm={(kind) => activeOverlay = kind} onopen={navigate}/>
        {:else if app.page === 'recovery'}<RecoveryPage onopen={navigate}/>
        {:else}<GalleryPage onopen={navigate}/>{/if}
      </div>
    </main>
  </div>

  {#if activeOverlay}
    <Overlay title={confirmation.title} onclose={closeOverlay} busy={working}>
      <div class="confirm-body">
        <span class="confirm-icon"><Icon name={confirmation.icon} size={25}/></span>
        <p>{confirmation.body}</p>
        {#if activeOverlay === 'test-message'}<div class="test-message-preview"><span class="muted small">测试正文</span><strong>Codex-QQ 连接测试</strong><small>通过当前已配置机器人发送，不包含任务正文。</small></div>{/if}
        {#if activeOverlay === 'install-hooks'}<div class="hook-confirm-lines"><span><Icon name="check" size={15}/>合并本程序的提醒钩子</span><span><Icon name="check" size={15}/>仅启用 Codex hooks 功能</span><span><Icon name="info" size={15}/>保留其他 Codex 配置</span></div>{/if}
        {#if activeOverlay === 'apply-update'}<div class="note-banner"><Icon name="info" size={17}/><span>{app.preview ? '浏览器设计预览不会下载或替换文件。' : '仅安装已验证签名的更新；后台未空闲时将等待。'}</span></div>{/if}
      </div>
      {#snippet footer()}<Button variant="secondary" disabled={working} onclick={closeOverlay}>取消</Button><Button variant={activeOverlay === 'stop' || activeOverlay === 'exit' ? 'danger' : 'primary'} loading={working} onclick={() => void act()}>{confirmation.action}</Button>{/snippet}
    </Overlay>
  {/if}

  {#if app.toast}<div class="toast" class:warning={app.toastWarning} role="status" aria-live="polite"><Icon name={app.toastWarning ? 'warning' : 'info'} size={17}/>{app.toast}</div>{/if}
</div>

<style>
  .full-layout { grid-template-columns:minmax(0,1fr); }.confirm-body { display:grid;justify-items:center;gap:13px;text-align:center; }.confirm-icon { display:grid;width:56px;height:56px;place-items:center;border:1px solid var(--line);border-radius:17px;background:var(--blue-wash);color:var(--blue-dark); }.confirm-body > p { max-width:410px;color:var(--ink-soft);font-size:13px;line-height:1.8; }.confirm-body > .note-banner { text-align:left; }.test-message-preview { display:grid;width:100%;gap:6px;padding:13px 15px;border:1px solid var(--line);border-radius:12px;background:var(--surface);text-align:left; }.test-message-preview strong { font-size:13px; }.test-message-preview small { color:var(--muted);font-size:10px; }.hook-confirm-lines { display:grid;width:100%;gap:8px;padding:13px 15px;border:1px solid var(--line);border-radius:12px;background:var(--surface);text-align:left; }.hook-confirm-lines > span { display:flex;align-items:center;gap:8px;color:var(--ink-soft);font-size:11px; }.hook-confirm-lines > span :global(svg) { color:var(--success); }.hook-confirm-lines > span:last-child :global(svg) { color:var(--muted); }
  @media(max-width:640px) { .full-layout .main-scroll { grid-row:2; }.app-window { border-radius:0; } }
</style>
