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
- 空闲/暂停可发送；忙时普通发送追加到 Desktop 自己的可见待发送列表。追加前必须保留整份本地快照并回读核对；失败或结果未知时不改走隐藏队列。队列卡片的“引导”先核实并删除唯一的可见排队项，再把相同正文引导给执行时的活动任务；两次原生操作不构成事务，未知结果不重试。
- 异步问题只提醒去电脑回答；分支仅最近完成位置；新建对话先确认项目与首条任务。
- 写操作结果未知不换路径重发；不改 Codex 登录态、历史或默认模型，不覆盖现有 QQ 绑定。
- 安装用私有 Job 管理临时网关，等本次 QQ READY 再提示绑定；配置写前记录可恢复，损坏或归属不明时保留原文件并停止。
- 配套 QQ 网关只接受私聊且要求非空身份；权限按钮绑定具体操作者和请求，旧卡、群消息及未授权点击不进入本地控制。

## 验证与交接

1. 先看 `git status` / `git diff`，保护已有改动；先定位和复现，再修对应层。
2. 运行相关测试、`py -3 -X utf8 -m unittest discover -s tests -v`、`py -3 -X utf8 tools/check_publication.py` 和 `git diff --check`。CI 覆盖 Windows / Python 3.11、3.13 及配套网关检查。
3. 区分单元测试、Windows 隔离进程验证、真实 QQ 正文实收；只报告实际完成的验证。旧 CI 失败按对应提交判断，不能当作当前失败或直接忽略。
4. 同步功能文案与本页；个人部署现状留在本机 `PROJECT_STATUS.md`，不上传个人运行数据。安装与更新细节见[开发说明](docs/development.md)。
