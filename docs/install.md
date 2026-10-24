# 首次安装

目前是 Windows 源码版。想交给智能体完成环境检查和部署，请复制 [README 的一句话安装](../README.md#一句话安装)，执行说明见[智能体安装指南](agent-install.md)。下面是手动流程，预留构建和机器人注册的时间；不要在两份目录里同时安装同一个助手。

## 1. 准备环境

- Windows 10/11 x64，使用自己的普通登录用户，不需要管理员权限。当前主要在 Windows 11 验证。
- 已安装、已登录且能够正常对话的 Codex Desktop，至少有一个本地对话。官方账号和第三方 API 均可。
- [Python 3.11 或更新版本](https://www.python.org/downloads/windows/)，包含 `py` 启动器和 `pythonw.exe`。测试矩阵为 3.11、3.13。
- [Git](https://git-scm.com/downloads/win)。普通安装会下载 GitHub Releases 中的配套网关，不需要 Go；只有网络受限、需要审查源码或开发适配补丁时，才按[开发说明](development.md#重新构建与更新)安装 [Go 1.25 或更新版本](https://go.dev/dl/)。
- Windows 自带 .NET Framework 编译器，用于生成小型托盘启动入口。缺失时工具会说明，不自动下载安装其他程序。
- 自己的 QQ 官方机器人 AppID 和 AppSecret。到 [QQ 开放平台](https://q.qq.com/) 创建机器人，资格、私聊权限、测试成员和上线要求以平台当时规则为准。[CC Connect 的 QQ 接入说明](https://github.com/chenhg5/cc-connect/blob/v1.5.0/docs/qqbot.md)可辅助参考。

这是官方机器人私聊，不需要登录个人 QQ 协议、安装 NapCat 或开放公网端口。不要把 AppSecret 发进 QQ、GitHub Issue 或终端命令参数。

## 2. 下载并构建

在 PowerShell 中执行：

```powershell
git clone https://github.com/IIMovoMII/Codex-QQ.git
cd Codex-QQ
py -3 -X utf8 tools/prepare_gateway.py
py -3 -X utf8 tools/build_resident_app.py
```

`prepare_gateway.py` 默认从本项目 GitHub Releases 下载并校验已经构建好的 CC Connect 适配版，运行时不需要 Go。网关发布包来自固定的 CC Connect 提交，应用了本仓库的三份补丁，只保留 ACP 和 QQ 通道；包内有来源信息、补丁说明和 SHA-256。首次下载只会写入不存在的目标，不会覆盖正在使用的程序。

生成的 `bin/CodexQQ助手.exe` 仍依赖当前目录的源码和本机 Python，**不是可复制到另一台电脑的单文件程序**。安装后不要随意搬动目录或删除 Python。更新和路径迁移见[开发说明](development.md)。

## 3. 获取自己的 QQ 身份

```powershell
py -3 -X utf8 tools/configure.py
```

按本机提示输入 AppID、AppSecret；密钥输入不显示。使用 QQ 沙箱时加 `--sandbox`，并按平台要求加入测试成员。已有正式配置时工具拒绝覆盖，老用户不用重复运行。

工具会打印配置文件的位置，默认为 `%LOCALAPPDATA%\CodexDesktopWeixin\cc-connect.toml`。这是沿用的历史目录名，不表示安装了微信组件。

在当前 PowerShell 前台启动**配置专用网关**：

```powershell
& .\vendor\cc-connect\v1.5.0-desktop-control\bin\cc-connect-desktop-control.exe --config "$env:LOCALAPPDATA\CodexDesktopWeixin\cc-connect.toml"
```

到 QQ 平台提供的机器人入口打开私聊，发送「绑定」。它会返回你的 **OpenID**，不是 QQ 号码。

此阶段连接的是独立的“身份查询器”，只能回传发送者身份，不连接 Codex、不执行任务；临时接收所有身份以便取得自己的 OpenID。不要长期保持这个配置模式。

## 4. 绑定并启用正式连接

回到前台网关窗口按 `Ctrl+C` 停止它，然后执行：

```powershell
py -3 -X utf8 tools/configure.py --bind
```

粘贴你自己在 QQ 私聊收到的 OpenID，再输入 `yes` 确认。工具把接收范围收紧为这个身份，并配置通知发送到同一私聊；不自动启动应用或开启自启。

默认运行目录是 `%LOCALAPPDATA%\CodexDesktopWeixin`，QQ 数据放在较短的 `%LOCALAPPDATA%\CodexCC`，避免 Windows 本地套接字路径过长。如果目录已有旧安装的数据，工具不接管或清空，请先核对现有安装。

## 5. 先开启独立应用，再安装钩子

双击 `bin/CodexQQ助手.exe`，等通知区域出现助手图标。这一步先建立独立运行标记，再启用钩子，避免首次安装期间触发旧的跟随启动模式。此时尚未安装钩子，部分状态/控制不可用是正常的，不要用旧的 `start` 命令替代本步骤。

先结束其他正在进行的任务。确认 `%USERPROFILE%\.codex\config.toml` 已存在；自定义 `CODEX_HOME` 时遵从自己的配置位置。

```powershell
.\codex-watch.cmd install-hooks
```

安装器合并本程序的钩子，启用 `[features] hooks = true`，保留其他钩子和配置内容；修改前在本地留下恢复副本，不上传。**不会改登录凭据、模型设置或历史数据库。**

需要重新打开 Codex 时，先保存进度，在信任提示中核对 Python 路径和本项目的 `cli.py hook`，再允许。如果安装智能体正运行在 Codex 里，先让它写好接续步骤，不要让它把自己所在的窗口强制关闭。当前钩子命令按 Windows PowerShell 调用方式生成；若 Codex 使用其他命令解释器，请先适配并验证。

钩子只记录本地事件，不在钩子里联网或调用模型。新的对话活动也会让助手取得当前 Desktop 的本地控制端点；刚装好但还没有活动时，部分功能可能提示尚未连接。

## 6. 启动与验收

助手应已在通知区域运行；若已退出，再双击 `bin/CodexQQ助手.exe`。右键可看状态、打开日志、开启/关闭登录自启、退出。不要用管理员身份启动。

Codex 启动后，在 QQ 发送「帮助」，再依次测试：

1. 发「对话」，引用列表回复序号，确认项目和标题正确。
2. 引用选定回执发「设置」，核对模型、档位和权限。
3. 引用回执发无害测试任务，在电脑确认正文进入正确对话。
4. 等任务完成，在 QQ 聊天中确认能看到正文，而不只是一条系统推送。

```powershell
.\codex-watch.cmd doctor
```

`cc_connect_api_status = ready` 说明本地投递服务就绪，不等于手机已实收。完整用法见[操作手册](usage.md)。

## 关闭、更新和卸载

- 关闭：托盘右键「退出」，不会关闭 Codex。关闭 Codex 也不会退出助手。
- 自启：托盘菜单按需开启，默认不会自动打开。
- 更新：先退出助手，再更新源码、跑测试；有网关补丁变更再重建网关。不要运行上游 `cc-connect update` 替换适配版。
- 卸载：先在托盘关闭自启并退出，再执行 `.\codex-watch.cmd uninstall-hooks`，只移除本程序钩子。源码和上述应用数据由你确认后手动删除；应用数据内含密钥及消息状态，请勿分享。Codex 本身的配置与历史不应删除。
