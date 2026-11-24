import { expect, test } from '@playwright/test';

test('native state failure cannot display preview identity or success', async ({ page }) => {
  await page.addInitScript(() => {
    (window as unknown as Record<string, unknown>).__TAURI_INTERNALS__ = {
      transformCallback: () => 1,
      invoke: async (command: string) => {
        if (command === 'app_snapshot') throw new Error('service offline');
        if (command.startsWith('plugin:event|')) return 1;
        throw new Error('unavailable');
      },
    };
  });
  await page.goto('/?scenario=ready#/qq');
  await expect(page.getByText('102345678')).toHaveCount(0);
  await expect(page.getByText('已就绪', { exact: true })).toHaveCount(0);
  await expect(page.getByText('demo_openid_A7C9')).toHaveCount(0);
  await page.goto('/?scenario=ready#/gallery');
  await expect(page.getByRole('heading', { name: '组件展厅' })).toHaveCount(0);
});

test('unknown hook trust is informationally silent when its local config is ready', async ({ page }) => {
  await page.addInitScript(() => {
    const snapshot = { revision: 1, observedAt: new Date().toISOString(), runtime: 'ready', codex: 'ready',
      qq: 'ready', configured: true, appId: '987654321', identity: '•••• TST9', sandbox: false,
      hooks: 'ready', hooksInstalled: true, hooksEnabled: true, hooksTrust: 'unknown',
      startup: false, startupVerified: false, receivedByUser: false };
    (window as unknown as Record<string, unknown>).__TAURI_INTERNALS__ = {
      transformCallback: () => 1,
      invoke: async (command: string, args: { request?: { command?: string; requestId?: string } }) => {
        if (command === 'app_snapshot') return { snapshot, preferences: { version: 1, theme: 'light' } };
        if (command.startsWith('plugin:event|')) return 1;
        if (args?.request?.command === 'get_setup_state') return { status: 'confirmed', preview: false,
          operationId: args.request.requestId, data: { setup: { phase: 'configured', step: 8, ready: true } } };
        if (args?.request?.command === 'list_notifications') return { status: 'confirmed', preview: false,
          operationId: args.request.requestId, data: { deliveries: [], total: 0 } };
        throw new Error('unavailable');
      },
    };
  });
  await page.goto('/');
  await expect(page.getByText('提醒钩子待处理')).toHaveCount(0);
  await page.getByRole('button', { name: 'Codex 连接', exact: true }).click();
  await expect(page.getByText('已安装并启用')).toBeVisible();
  await expect(page.getByText(/信任.*(待核对|未确认)|需在 Codex 信任/)).toHaveCount(0);
});

test('unreadable hook status is omitted from Codex and diagnostic pages', async ({ page }) => {
  await page.addInitScript(() => {
    const snapshot = { revision: 1, observedAt: new Date().toISOString(), runtime: 'ready', codex: 'ready',
      qq: 'ready', configured: true, appId: '987654321', identity: '•••• TST9', sandbox: false,
      hooks: 'unknown', hooksInstalled: null, hooksEnabled: null, hooksTrust: 'unknown',
      startup: false, startupVerified: false, receivedByUser: false };
    (window as unknown as Record<string, unknown>).__TAURI_INTERNALS__ = {
      transformCallback: () => 1,
      invoke: async (command: string, args: { request?: { command?: string; requestId?: string } }) => {
        if (command === 'app_snapshot') return { snapshot, preferences: { version: 1, theme: 'light' } };
        if (command.startsWith('plugin:event|')) return 1;
        if (args?.request?.command === 'get_setup_state') return { status: 'confirmed', preview: false,
          operationId: args.request.requestId, data: { setup: { phase: 'configured', step: 8, ready: true } } };
        if (args?.request?.command === 'list_notifications') return { status: 'confirmed', preview: false,
          operationId: args.request.requestId, data: { deliveries: [], total: 0 } };
        if (args?.request?.command === 'get_diagnostics') return { status: 'confirmed', preview: false,
          operationId: args.request.requestId, data: { logs: [] } };
        throw new Error('unavailable');
      },
    };
  });
  await page.goto('/');
  await expect(page.getByText('提醒钩子待处理')).toHaveCount(0);
  await page.getByRole('button', { name: 'Codex 连接', exact: true }).click();
  await expect(page.getByText('提醒钩子')).toHaveCount(0);
  await page.getByRole('button', { name: '帮助与诊断', exact: true }).click();
  await expect(page.getByText('提醒钩子')).toHaveCount(0);
});

test('notification failure preserves the authoritative native snapshot', async ({ page }) => {
  await page.addInitScript(() => {
    const snapshot = { revision: 1, observedAt: new Date().toISOString(), runtime: 'stopped', codex: 'offline',
      qq: 'offline', configured: true, appId: '987654321', identity: '•••• TST9', sandbox: true,
      hooks: 'missing', startup: false, startupVerified: false, receivedByUser: false };
    (window as unknown as Record<string, unknown>).__TAURI_INTERNALS__ = {
      transformCallback: () => 1,
      invoke: async (command: string, args: { request?: { command?: string; requestId?: string } }) => {
        if (command === 'app_snapshot') return { snapshot, preferences: { version: 1, theme: 'light' } };
        if (command.startsWith('plugin:event|')) return 1;
        if (args?.request?.command === 'get_setup_state') return { status: 'confirmed', preview: false,
          operationId: args.request.requestId, data: { setup: { phase: 'configured', step: 8, ready: false } } };
        throw new Error('notification service unavailable');
      },
    };
  });
  await page.goto('/');
  await page.getByRole('button', { name: 'QQ 机器人', exact: true }).click();
  await expect(page.getByText('987654321', { exact: true })).toBeVisible();
  await expect(page.getByText('•••• TST9', { exact: false })).toBeVisible();
  await expect(page.getByText('102345678')).toHaveCount(0);
  await expect(page.getByText('已连接', { exact: true })).toHaveCount(0);
});

test('failed native connection refresh removes stale readiness and retains configured identity', async ({ page }) => {
  await page.addInitScript(() => {
    const snapshot = { revision: 1, observedAt: new Date().toISOString(), runtime: 'ready', codex: 'ready',
      qq: 'ready', configured: true, appId: '987654321', identity: '•••• TST9', sandbox: true,
      hooks: 'missing', startup: false, startupVerified: false, receivedByUser: false };
    (window as unknown as Record<string, unknown>).__TAURI_INTERNALS__ = {
      transformCallback: () => 1,
      invoke: async (command: string, args: { request?: { command?: string; requestId?: string } }) => {
        if (command === 'app_snapshot') return { snapshot, preferences: { version: 1, theme: 'light' } };
        if (command.startsWith('plugin:event|')) return 1;
        if (args?.request?.command === 'get_setup_state') return { status: 'confirmed', preview: false,
          operationId: args.request.requestId, data: { setup: { phase: 'configured', step: 8, ready: true } } };
        if (args?.request?.command === 'list_notifications') return { status: 'confirmed', preview: false,
          operationId: args.request.requestId, data: { deliveries: [], total: 0 } };
        throw new Error('connection service unavailable');
      },
    };
  });
  await page.goto('/');
  await expect(page.getByRole('heading', { name: '已就绪', exact: true })).toBeVisible();
  await page.getByRole('button', { name: '重新检查', exact: true }).click();
  await expect(page.getByRole('heading', { name: '状态待核对', exact: true })).toBeVisible();
  await expect(page.getByRole('heading', { name: '已就绪', exact: true })).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'Codex 已连接', exact: true })).toHaveCount(0);
  await page.getByRole('button', { name: 'QQ 机器人', exact: true }).click();
  await expect(page.getByText('987654321', { exact: true })).toBeVisible();
  await expect(page.getByText('•••• TST9', { exact: false })).toBeVisible();
});

test('current Codex tools show a connection while quote control and queuing remain unavailable', async ({ page }) => {
  await page.addInitScript(() => {
    const snapshot = { revision: 1, observedAt: new Date().toISOString(), runtime: 'ready', codex: 'limited',
      qq: 'ready', configured: true, appId: '987654321', identity: '•••• TST9', sandbox: true,
      hooks: 'ready', startup: false, startupVerified: false, receivedByUser: false };
    (window as unknown as Record<string, unknown>).__TAURI_INTERNALS__ = {
      transformCallback: () => 1,
      invoke: async (command: string, args: { request?: { command?: string; requestId?: string } }) => {
        if (command === 'app_snapshot') return { snapshot, preferences: { version: 1, checkUpdates: false } };
        if (command.startsWith('plugin:event|')) return 1;
        const request = args?.request;
        const data = request?.command === 'get_setup_state' ? { setup: { phase: 'configured', step: 8, ready: true } }
          : request?.command === 'list_notifications' ? { deliveries: [], total: 0 }
          : request?.command === 'check_connections' ? { snapshot: { ...snapshot, revision: 2, codex: 'ready' } }
          : null;
        if (data) return { status: 'confirmed', preview: false, operationId: request?.requestId, data };
        throw new Error('unavailable');
      },
    };
  });
  await page.goto('/');
  await expect(page.getByRole('heading', { name: 'Codex 控制受限', exact: true })).toBeVisible();
  await page.getByRole('button', { name: '查看连接', exact: true }).click();
  const connection = page.getByRole('region', { name: 'Codex 与助手连接路径' });
  await expect(connection.getByText('运行中', { exact: true })).toBeVisible();
  await expect(connection.getByText('已连接 · 控制受限', { exact: true })).toBeVisible();
  await expect(page.getByText('暂不可用', { exact: true })).toHaveCount(2);
  await page.getByRole('button', { name: '重新检查', exact: true }).click();
  await expect(connection.getByText('可用', { exact: true })).toBeVisible();
  await expect(page.getByText('暂不可用', { exact: true })).toHaveCount(0);
});

for (const known of [true, false]) {
  test(`native update check preserves ${known ? 'definite failure' : 'uncertain result'} without automatic retry`, async ({ page }) => {
    await page.addInitScript(({ known }) => {
      const globals = window as unknown as Record<string, unknown>;
      const snapshot = { revision: 1, observedAt: new Date().toISOString(), runtime: 'ready', codex: 'ready',
        qq: 'ready', configured: true, appId: '987654321', identity: '•••• TST9', sandbox: true,
        hooks: 'missing', startup: false, startupVerified: false, receivedByUser: false };
      globals.__updateChecks = 0;
      globals.__TAURI_INTERNALS__ = {
        transformCallback: () => 1,
        invoke: async (command: string, args: { request?: { command?: string; requestId?: string } }) => {
          if (command === 'app_snapshot') return { snapshot, preferences: { version: 1, theme: 'light', checkUpdates: false } };
          if (command.startsWith('plugin:event|')) return 1;
          const request = args?.request;
          if (request?.command === 'get_setup_state') return { status: 'confirmed', preview: false,
            operationId: request.requestId, data: { setup: { phase: 'configured', step: 8, ready: true } } };
          if (request?.command === 'list_notifications') return { status: 'confirmed', preview: false,
            operationId: request.requestId, data: { deliveries: [], total: 0 } };
          if (command === 'app_update' && request?.command === 'check_update') {
            globals.__updateChecks = Number(globals.__updateChecks) + 1;
            return { status: known ? 'confirmed' : 'unknown', preview: false, operationId: request.requestId,
              ...(known ? { errorCode: 'update_feed_unavailable', data: { update: { status: 'failure' } } } : {}) };
          }
          throw new Error('unavailable');
        },
      };
    }, { known });
    await page.goto('/#/updates');
    await page.getByRole('button', { name: '检查更新', exact: true }).click();
    await expect(page.getByText('最近检查：尚未检查', { exact: true })).toHaveCount(0);
    if (known) {
      await expect(page.getByRole('heading', { name: '检查更新失败', exact: true })).toBeVisible();
      await expect(page.getByText('暂时无法获取更新信息，请检查网络后重新检查。')).toBeVisible();
      await expect(page.getByText('本次没有下载或安装更新，助手可以继续使用。', { exact: false })).toBeVisible();
      await expect(page.getByText('结果未知时先核对操作状态', { exact: false })).toHaveCount(0);
    } else {
      await expect(page.getByText('更新结果未确认', { exact: true })).toBeVisible();
      await expect(page.getByText('结果未知时先核对操作状态', { exact: false })).toBeVisible();
      await expect(page.getByText('本次没有下载或安装更新', { exact: false })).toHaveCount(0);
    }
    await expect(page.getByRole('button', { name: '下载更新', exact: true })).toHaveCount(0);
    expect(await page.evaluate(() => (window as unknown as Record<string, unknown>).__updateChecks)).toBe(1);
  });
}
