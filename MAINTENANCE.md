# 长期维护入口

开发、排错和较大改动前必读。这里是代码导航与当前能力边界的权威入口；实际行为以源码、对应测试和运行证据为准。
架构、入口、关键行为或验证方式改变后及时更新；没有变化不要改。保持简短，不堆故障流水账、机器路径、密钥和运行日志。

## 运行关系

```text
启动器 → 独立助手 → 事件监听 / 通知队列 → CC Connect 配套网关 → QQ
QQ 消息 → ACP 元数据 → 引用路由 → 当前 Codex Desktop 原生入口
```

助手可独立运行与登录自启。Codex 关闭不退出助手；连接不可用时新控制指令作废，不在以后补执行。

## 找问题先看哪里

| 现象 / 变更 | 主要入口 |
| --- | --- |
| 安装、续接、绑定 | `tools/install_app.py`、`tools/configure.py`、`tools/pairing_agent.py` |
| 临时网关遗留、绑定未就绪 | `tools/setup_process.py`；`tests/test_setup_process.py` |
| 下载、启动入口、错误展示 | `tools/fetch_gateway.py`、`tools/build_resident_app.py`、`tools/setup_diagnostics.py` |
| 自启、托盘、退出、资源占用 | `src/codex_desktop_weixin/resident_app.py`、`win_tray.py` |
| 漏提醒、重复提醒、通知积压 | 同包 `monitor.py`、`tailer.py`、`state_machine.py`、`notifier.py` |
| 引用失效、身份、消息去重 | 同包 `control_router.py`、`qq_desktop_bridge.py` |
| 载入旧对话、发送、暂停、引导、队列 | 同包 `control_backend.py`、`control_contract.py`、`submission_receipts.py` |
| 原生工具发现或参数变化 | 同包 `native_app_tools.py`；按实际进程和接口判断，不锁安装版本 |
| 网关元数据或 QQ 投递 | `vendor/cc-connect/v1.5.0-desktop-control/`；发布由 `.github/workflows/` 管理 |
| 手机帮助与文案 | 同包 `mobile_copy.py`；[操作手册](docs/usage.md) |

## 当前边界与修复底线

- 每次控制按当前消息的引用确定对话；不沿用上次选择，不把正文或模型文本当成可信路由。
- 空闲/暂停可发送；忙时普通发送明确拒绝。Desktop 目前只提供整份可见队列替换，没有可核实的 UI CAS/原子追加能力；不能用 ACP 锁声称追加安全，也不改走隐藏队列。已有队列卡片的“引导”先核实并删除唯一的可见排队项，再把相同正文引导给执行时的活动任务；两次原生操作不构成事务，未知结果不重试。
- 异步问题只提醒去电脑回答；分支仅最近完成位置；新建对话先确认项目与首条任务。
- 写操作结果未知不换路径重发；不改 Codex 登录态、历史或默认模型，不覆盖现有 QQ 绑定。
- 安装用私有 Job 管理临时网关，等本次 QQ READY 再提示绑定；配置写前记录可恢复，损坏或归属不明时保留原文件并停止。
- 首次安装在索要凭据前显示 QQ 开放平台的新手步骤；Agent 和安装窗口都说明 AppID/AppSecret 的位置、测试 QQ 配置和密钥边界，不能代替用户完成平台注册或认证。
- 配套 QQ 网关的首次绑定走私聊；群聊默认关闭，只有 `group_allow_from` 中明确写入“群 OpenID/成员 OpenID”才接收该成员。“群聊身份”只回显当前群和发送者自己的标识，不会授权。权限按钮绑定具体操作者、群和请求。
- 修改 Codex 钩子时只删除本程序自己的处理项；启用 `features.hooks` 后，其他 TOML 配置语义必须保持不变。遇到无法安全解析的格式应停止而不是重写整份配置。
- 临时网关停止必须基于同一个已核验进程句柄；身份或套接字探测不确定时保留目标，绝不按进程名或路径猜测清理。
- `monitor.py` 清理只作用于已处理/失败的 spool、已完成/失败/作废/dead-letter 的 outbox 和本项目日志；pending 投递有数量与字节配额，超限的最旧记录进入可审计的 dead-letter。dead-letter 是终态，重复事件和重启都不会把同一记录重新放回 pending，也不会自动重发。缺失转录等待 7 天后转成不含正文/路径的有限期记录；清理频率、保留期、数量和日志大小上限以对应常量及测试为准。
- 常驻助手与公开 `monitor` 入口共用 `state/app.lock`，同一数据目录只允许一个 worker；监听器定期扫描近期会话并重建失效 watcher。通知成功事实进入独立 SQLite 账本，游标采用有界热缓存加磁盘冷索引，坏状态按 fail-closed 处理；冷索引坏行会单行隔离，整库损坏会保留副本后重建。新通知带绑定作用域的引用令牌；旧版只有纯线程号的提醒仅在已核实的 `qqbot:<同一 user_id>` 私聊中兼容，群聊或作用域不明时拒绝写操作。通知/控制引用、日志和历史索引按对应保留上限清理。
- rollout 记录缺失或无法解析时间戳时直接跳过该行，并将上下文标记为不完整；不会用当前时间替代损坏时间，也不会让旧历史记录越过 `not_before` 边界触发新提醒。永久 thread 引用按每个 QQ scope 最多保留最近 256 条，过期旧卡片 fail-closed。

## 验证与交接

1. 先看 `git status` / `git diff`，保护已有改动；先定位和复现，再修对应层。
2. 运行相关测试、`py -3 -X utf8 -m unittest discover -s tests -v`、`py -3 -X utf8 tools/check_publication.py` 和 `git diff --check`。CI 覆盖 Windows / Python 3.11、3.13 及配套网关检查。
3. 区分单元测试、Windows 隔离进程验证、真实 QQ 正文实收；只报告实际完成的验证。旧 CI 失败按对应提交判断，不能当作当前失败或直接忽略。
4. 同步功能文案与本页；个人部署现状留在本机 `PROJECT_STATUS.md`，不上传个人运行数据。安装与更新细节见[开发说明](docs/development.md)。
