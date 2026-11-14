import { expect, test } from '@playwright/test';

test('desktop navigation and notification detail work', async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 1440, height: 960 });
  await page.goto('/');
  await expect(page.getByRole('heading', { name: '概览' })).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath('overview-desktop.png') });

  await page.getByRole('button', { name: 'QQ 机器人', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'QQ 机器人', exact: true })).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath('qq-desktop.png') });
  await page.getByRole('button', { name: '发送测试消息…' }).click();
  const testMessageDialog = page.getByRole('dialog', { name: '发送 QQ 测试消息' });
  await expect(testMessageDialog).toBeVisible();
  await testMessageDialog.getByRole('button', { name: '发送测试消息' }).click();
  await expect(testMessageDialog).toBeHidden();
  await expect(page.getByRole('status')).toContainText('预览未发送 QQ 消息');

  for (const [navigation, heading] of [
    ['Codex 连接', 'Codex 连接'],
    ['通知记录', '通知记录'],
  ]) {
    await page.getByRole('button', { name: navigation, exact: true }).click();
    await expect(page.getByRole('heading', { name: heading, exact: true })).toBeVisible();
  }

  await page.locator('.delivery-table tbody tr').first().click();
  const dialog = page.getByRole('dialog', { name: '通知详情' });
  await expect(dialog).toBeVisible();
  await expect(dialog.getByText('通知已生成')).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath('notification-detail-desktop.png') });
  for (const width of [1440, 720, 390]) {
    await page.setViewportSize({ width, height: 960 });
    const phases = await dialog.locator('.timeline-row').evaluateAll(rows => rows.map(row => {
      const time = row.querySelector('.timeline-time')!.getBoundingClientRect();
      const label = row.querySelector('.timeline-label')!.getBoundingClientRect();
      return { timeRight: time.right, labelLeft: label.left, labelRight: label.right,
        rowRight: row.getBoundingClientRect().right };
    }));
    for (const phase of phases) {
      expect(phase.timeRight, 'full native date must not overlap its event').toBeLessThan(phase.labelLeft);
      expect(phase.labelRight).toBeLessThanOrEqual(phase.rowRight + 1);
    }
  }
  await page.keyboard.press('Escape');
  await expect(dialog).toBeHidden();
});

test('appearance controls apply the selected theme', async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 1440, height: 960 });
  await page.goto('/');
  await page.getByRole('button', { name: '设置', exact: true }).click();
  await page.getByRole('group', { name: '设置分类' }).getByRole('button', { name: '外观' }).click();
  await expect(page.locator('.page')).toHaveCSS('opacity', '1');
  await expect.poll(() => page.locator('html').evaluate(el => getComputedStyle(el).getPropertyValue('--page').trim())).toBe('#f4f5f7');
  await page.screenshot({ path: testInfo.outputPath('appearance-light.png') });
  await page.locator('.theme-choices').getByRole('button', { name: '深色' }).click();
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
  await expect.poll(() => page.locator('html').evaluate(el => getComputedStyle(el).getPropertyValue('--page').trim())).toBe('#15171b');
  await expect(page.locator('.page')).toHaveCSS('opacity', '1');
  await expect(page.locator('.page h1')).toHaveCSS('color', 'rgb(242, 244, 247)');
  await expect(page.locator('.glass-light')).toHaveCSS('opacity', '0.04');
  await page.screenshot({ path: testInfo.outputPath('appearance-dark.png') });
  await expect(page.getByRole('heading', { name: '材质与动效' })).toBeVisible();
});

test('mobile layouts stay within the viewport across primary pages', async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/');
  await page.screenshot({ path: testInfo.outputPath('overview-mobile.png') });

  for (const [navigation, heading] of [
    ['概览', '概览'],
    ['QQ 机器人', 'QQ 机器人'],
    ['Codex 连接', 'Codex 连接'],
    ['通知记录', '通知记录'],
    ['设置', '设置'],
    ['更新', '更新'],
    ['帮助与诊断', '帮助与诊断'],
  ]) {
    await page.getByRole('button', { name: navigation, exact: true }).click();
    await expect(page.getByRole('heading', { name: heading, exact: true })).toBeVisible();
    const widths = await page.evaluate(() => ({ viewport: innerWidth, root: document.documentElement.scrollWidth }));
    expect(widths.root, `${navigation} should not create horizontal scrolling`).toBeLessThanOrEqual(widths.viewport);
  }
});

test('compact desktop layouts keep navigation and content reachable', async ({ page }) => {
  await page.setViewportSize({ width: 1075, height: 552 });
  await page.goto('/');
  const labels = await page.locator('.nav-item > span').evaluateAll(items => items.map(item => {
    const range = document.createRange();
    range.selectNodeContents(item);
    return { lines: range.getClientRects().length, right: item.getBoundingClientRect().right,
      buttonRight: item.parentElement!.getBoundingClientRect().right };
  }));
  for (const label of labels) {
    expect(label.lines, 'compact sidebar labels should remain on one line').toBe(1);
    expect(label.right).toBeLessThanOrEqual(label.buttonRight);
  }
  await page.setViewportSize({ width: 820, height: 600 });
  await expect(page.locator('.sidebar')).toHaveCSS('flex-direction', 'row');

  await page.setViewportSize({ width: 720, height: 440 });
  const compactLayout = await page.evaluate(() => {
    const main = document.querySelector('.main-scroll');
    return {
      viewportWidth: innerWidth,
      documentWidth: document.documentElement.scrollWidth,
      mainClientHeight: main?.clientHeight ?? 0,
      mainScrollHeight: main?.scrollHeight ?? 0,
    };
  });
  expect(compactLayout.documentWidth).toBeLessThanOrEqual(compactLayout.viewportWidth);
  expect(compactLayout.mainScrollHeight).toBeGreaterThan(compactLayout.mainClientHeight);

  await page.locator('.app-titlebar').evaluate(element => element.remove());
  await page.locator('.app-window').evaluate(element => element.classList.add('native-window'));
  await expect(page.locator('.app-body')).toHaveJSProperty('offsetTop', 0);
});

test('unconfigured preview opens the binding wizard', async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 1518, height: 1032 });
  await page.goto('/?scenario=unconfigured');
  await page.screenshot({ path: testInfo.outputPath('setup-desktop.png') });
  await expect(page.getByRole('heading', { name: '让 Codex 与你保持连接' })).toBeVisible();
  await expect(page.locator('.progress-steps')).toContainText('准备');
  await expect(page.getByText('Windows 桌面应用', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: '检查并准备' }).click();
  await expect(page.getByRole('heading', { name: '准备你的 QQ 机器人' })).toBeVisible();
  await expect(page.getByText('AppSecret 是机器人密钥', { exact: false })).toBeVisible();
  await page.getByRole('button', { name: '我已准备好' }).click();
  await expect(page.getByLabel('AppSecret', { exact: true })).toHaveAttribute('type', 'password');
  await expect(page.getByRole('button', { name: '设计预览' })).toBeVisible();
});
