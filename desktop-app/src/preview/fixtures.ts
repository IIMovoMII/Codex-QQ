import type { Delivery, Scenario, Snapshot } from '../bridge/contracts';

// Synthetic data only. This module never reads the installed QQ assistant.
export function snapshotFor(scenario: Scenario): Snapshot {
  return {
    revision: 1, observedAt: new Date().toISOString(),
    runtime: scenario === 'stopped' ? 'stopped' : scenario === 'unknown' ? 'unknown' : 'ready',
    codex: scenario === 'unknown' ? 'unknown' : 'ready',
    qq: scenario === 'offline' ? 'offline' : scenario === 'unknown' ? 'unknown' : 'ready',
    configured: scenario !== 'unconfigured', appId: '102345678', sandbox: false,
    identity: '•••• A7C9', hooks: 'ready', startup: false, startupVerified: false, receivedByUser: false,
  };
}

export function deliveriesFor(scenario: Scenario): Delivery[] {
  if (scenario === 'empty' || scenario === 'unconfigured') return [];
  const rows: [string, Delivery['event'], Delivery['state'], number][] = [
    ['10:42', 'complete', 'unknown', 0], ['10:18', 'attention', 'sent', 0],
    ['09:56', 'complete', 'sent', 0], ['09:31', 'error', 'sent', 0],
    ['09:05', 'complete', 'pending', 0], ['08:42', 'complete', 'sent', 0],
    ['18:26', 'attention', 'sent', 1], ['17:08', 'complete', 'pending', 1],
    ['15:30', 'complete', 'sent', 1], ['12:06', 'error', 'terminated', 2],
    ['11:24', 'complete', 'sent', 3], ['10:08', 'complete', 'sent', 4],
  ];
  return rows.map(([time, event, state, ageDays], i) => ({
    id: `preview-delivery-${i + 1}`, time, event, state, ageDays, target: '本人私聊',
    title: event === 'complete' ? '任务完成' : event === 'attention' ? '需要确认' : '任务出错',
    timeline: [{ time: '2026-11-14 ' + time + ':03', label: '通知已生成', icon: 'file' },
               { time: '2026-11-14 ' + time + ':05', label: state === 'sent' ? '平台确认发送' : '结果待核对', icon: 'send' }],
  }));
}

export const helpArticles = [
  { id: 'start', title: '第一次使用', icon: 'sparkles', summary: '从电脑到手机，完成第一次连接。', paragraphs: ['在 QQ 中私聊机器人，发送“帮助”查看支持的操作。', '发送“对话”，引用列表选择一个对话；之后每次操作都引用对应回执或任务提醒。', '连接就绪表示本机检查通过。请在手机聊天中确认实际收到正文。'] },
  { id: 'reference', title: '引用与发送', icon: 'link', summary: '每一条引用，都指向一个明确的对话。', paragraphs: ['每次发送正文或控制命令时，引用带“引用标识”的对应卡片。', '助手根据本条消息的引用确定目标，不沿用上次选择，也不根据标题猜测。', '空闲对话开始新一轮；运行中的正文进入 Codex 原生可见队列。'] },
  { id: 'queue', title: '运行中排队', icon: 'list', summary: '了解排队、暂停与引导的区别。', paragraphs: ['运行中发送普通正文会追加到 Codex 原生队列，只有精确匹配的队列回执才能确认受理。', '排队受理不代表任务已经开始。“暂停”停止当前轮次，保留此前排队消息。', '结果未确认时先检查电脑端可见队列，不自动重发相同内容。'] },
  { id: 'questions', title: '审批与问题', icon: 'circle-help', summary: '需要你参与时，及时回到电脑。', paragraphs: ['异步问题会向 QQ 推送提醒，答案需要在电脑端 Codex 中处理。', '登录验证、额外授权和未知请求也在电脑完成。普通正文不能代替审批。', '长对话的摘要可能不包含全部请求；缺少信息不代表没有待办。'] },
  { id: 'groups', title: '群聊与身份', icon: 'users', summary: '只授权你信任的群和成员。', paragraphs: ['群聊默认关闭，私聊绑定不会自动授权群聊成员。', '需要时在可信群 @机器人发送“群聊身份”，按文档配置精确的群 OpenID/成员 OpenID。', '身份查询本身不会授予权限；群内回复对其他成员可见。'] },
  { id: 'startup', title: '自启与更新', icon: 'refresh', summary: '让助手随登录就绪，保持更新。', paragraphs: ['在“设置 → 通用”开启登录 Windows 后启动。系统配置成功后，实际登录效果仍需在下次正常登录核对。', '关闭窗口后助手继续在托盘运行，退出应用才停止助手。', '更新仅替换助手自身程序；有活动任务、待发通知或无法核实状态时等待安全时机。'] },
];
