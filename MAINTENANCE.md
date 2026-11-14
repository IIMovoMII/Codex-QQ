# 产品与维护入口

Codex-QQ 是 Windows 常驻助手：观察本机 Codex Desktop 任务，向指定 QQ 会话提醒，并按每条 QQ 消息的明确引用控制对应对话。状态来自程序证据，不由模型猜测。

## 硬边界

- 不按标题、最近活动或上次选择猜目标；空闲/暂停时开始新轮次，运行中正文追加原生可见队列，匹配回执后才报告已排队。
- 写入结果未知先核对，不换通道重发；异步问题只提醒回电脑处理。连接不可用的新指令作废，不恢复后补执行。
- 不改 Codex 认证、默认模型或历史，不覆盖 QQ 绑定；群聊默认关闭，启用须精确限定群和成员。
- 同一数据目录只有一个 worker；关闭界面与退出助手不同。迁移、升级和清理只操作已确认归属的本应用资源。

## 按需导航

Python 模块默认位于 `src/codex_desktop_weixin/`。

| 任务 | 入口 |
| --- | --- |
| 安装、续接、本人绑定 | [安装指南](docs/agent-install.md)；`tools/install_app.py`、`tools/configure.py`、`tools/setup_process.py` |
| 托盘、自启、生命周期 | `resident_app.py`、`win_tray.py`、`installer.py` |
| 监听、状态、投递 | `monitor.py`、`tailer.py`、`state_machine.py`、`notifier.py`、`delivery_ledger.py` |
| 引用、原生控制与文案 | `control_router.py`、`qq_desktop_bridge.py`、`control_backend.py`、`native_app_tools.py`；[动作参考](docs/reply-action-reference.md) |
| 桌面应用与接管 | `desktop-app/`、`desktop_bridge.py`、`desktop_migration.py`；本机状态查 `PROJECT_STATUS.md` |
| 网关、构建与发布 | `vendor/cc-connect/`、`tools/`、`.github/workflows/`；[开发说明](docs/development.md) |
| 用户操作与故障 | [使用说明](docs/usage.md)、[常见问题](docs/troubleshooting.md) |

验证方法与实现约束只在[开发说明](docs/development.md)维护。本页不保存本机路径、待办流水或历史测试成绩；未完成功能不能写成已支持。
