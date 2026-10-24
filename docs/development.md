# 开发与维护

## 目录

```text
src/codex_desktop_weixin/   现有运行包名，保留以兼容旧安装
  resident_app.py          独立应用生命周期与单实例
  win_tray.py              Windows 原生托盘
  monitor.py / tailer.py   事件观察与增量游标
  state_machine.py        状态判断；Stop 不单独等同于完成
  notifier.py             持久通知队列和投递
  control_router.py       逐条引用、身份、去重和指令路由
  control_backend.py      指定对话的原生操作
  native_app_tools.py     当前 Desktop 公布的本地工具
  control_contract.py     按实际能力验证，不绑定安装版本
  mobile_copy.py          手机帮助与通知标题文案
tests/                    隔离回归测试
tools/                    本机构建、首次配置、诊断工具
vendor/cc-connect/        固定基线的适配与精简构建补丁
```

Python 运行部分只使用标准库。桌面可执行入口是小型 C# 启动器，通过现有 Explorer 启动 Python，避免继承 Codex 的作业组而随 Desktop 一起退出。它不是单文件打包器。

## 消息路径

```text
Codex 本地事件/增量日志 → 状态判断 → 本地通知队列 → CC Connect → QQ
QQ 引用 + 正文 → CC Connect 元信息 → 引用路由 → 当前 Desktop 的原生入口
```

原生接口按实际进程身份和所需方法核对，不根据文件名或安装目录中的版本号猜测。普通发送区分空闲/暂停与忙碌状态；空闲/暂停时通过 Desktop 所有者开始新一轮。运行中消息暂时拒绝，不能调用独立 `codex queue` 假装进入当前 Desktop 的可见队列；写操作结果未知时不自动换路径重试。

原生工具连接接受 `codex-app-tools-*` 和旧 `codex-browser-use-*` 名称。缓存只绑定当前 Desktop 进程身份，钩子提示和继承的环境变量都可能过期；失效时按需读取本地管道名称清单，限制候选数量、总时间，并在发送 `tools/list` 前核对实际服务进程。发现只发生在明确调用时，不增加常驻扫描。确认目录后校验所用工具参数，提交前再次核对身份；`tools/call` 一旦开始，任何失败都不能触发换端点重发。显式指定的非法端点不启用自动发现。

引用和入站处理状态保存在本项目自己的数据库；不改写 Codex 历史、认证和账号配置。钩子安装是例外的显式配置动作，只合并自身定义和启用 hooks。

## 测试

```powershell
py -3 -X utf8 -m unittest discover -s tests -v
py -3 -X utf8 tools/check_publication.py
```

隔离测试涵盖状态归并、退出不提醒、错误去重、编码、引用、排队、接口契约、权限/待办、创建、分支、生命周期和首次配置。真实 QQ 平台和 Codex 模型请求不在单元测试里调用。

Windows 原生测试中有隔离进程、托盘和注册表场景；不要把测试通过当作所有机器、所有 Desktop 版本或 QQ 实收均已验证。CI 检查 Python 3.11/3.13，并在干净环境重新构建固定基线的 QQ 网关。新人的首次绑定流程仍需端到端使用反馈。

## CC Connect 适配版

基于 v1.5.0 的固定提交，按 `metadata.patch`、`delivery.patch`、`minimal-build.patch` 顺序应用：

1. 原始消息 ID、时间、重连边界和引用信息透传给 ACP。
2. 有路由尾部的引用保存小型持久索引，不只依赖短期内存缓存。
3. 独立提醒走主动发送，不复用旧消息回复上下文。
4. QQ 业务错误、缺少消息 ID 不再当作成功。
5. 精简构建开关同样覆盖上游命令入口，避免未选用的平台/代理被间接导入。

构建只保留 ACP 和 QQ，明确排除其他 Agent；必须检查依赖及二进制编译标记，不能只看 `--version`。基线及第三方许可见[声明](../THIRD_PARTY_NOTICES.md)。不固定 Codex 安装版本与固定第三方构建依赖是两回事。

普通用户不需要自己准备 Go：`tools/prepare_gateway.py` 默认下载本项目 GitHub Releases 的 Windows x64 配套网关，校验发布包 SHA-256 和 `BUILD_INFO.json` 中的固定上游提交后，才写入不存在的目标。发布包还附带第三方声明和适配说明；因此 GitHub 并不是“不用 CC Connect”，而是把已经验证过的 CC Connect 适配版作为运行依赖交付。

### 重新构建与更新

首次构建脚本不会覆盖已有文件。已有安装更新网关时：

1. 退出 QQ 助手，保留现用程序和运行数据；不要关闭或改写 Codex。
2. 在新的临时目录取得固定基线，顺序应用补丁；可参考 `tools/prepare_gateway.py --source-build`，不要直接重新使用已打过补丁的源码。
3. 用 `tools/build_cc_connect.py --go <go.exe> --source <源码目录> --output <新的.staged.exe>` 生成候选文件，输出路径必须尚不存在。
4. 检查编译标签、测试和候选版本；验证后再替换项目中的网关文件，保留一份可恢复的前版即可。
5. 启动助手，检查 doctor、本地队列、QQ 入站和聊天正文实收。不能用退出码成功替代手机验收。

源码构建并打包发布时：

```powershell
py -3 -X utf8 tools/prepare_gateway.py --source-build --version 1.5.0-desktop-control.5
py -3 -X utf8 tools/package_gateway_release.py `
  --binary vendor/cc-connect/v1.5.0-desktop-control/bin/cc-connect-desktop-control.exe `
  --output-dir dist --release-tag v1.5.0-desktop-control.5 `
  --gateway-version 1.5.0-desktop-control.5
```

源码版首次构建需要 Go，运行已构建的应用不需要 Go。发布工作流只允许在 GitHub Actions 通过网关专项测试后创建新 Release；不要把本机运行目录、配置、密钥或未核实的 exe 手工上传。

只改 Python 源码通常只需退出助手、更新并测试、重新启动；不用重装钩子。修改入口路径/Python 位置后须重新构建启动入口，更新应用配置的绝对路径，重新安装并信任变化的钩子。不要运行首次配置来覆盖现有绑定。

## 改文案时

帮助用短段落、换行和少量图标，不依赖 QQ 的 Markdown 渲染。每条分类有独立入口，避免一屏列满内部术语。表情只修饰标题，不改模型正文。

`对话编号：`、`引用标识：`、异步问题尾部是路由契约，不在这些行前后随意插入图标或标点。新增操作要保留逐条引用、原始时间、身份核对和未知结果不重试，补充测试再写进帮助。

## 发布检查

Git 只跟踪源码、测试、公开文档和补丁。排除个人交接文档、运行配置、日志、历史、二进制及 `app-runtime.json`。发布脚本只能做基础检查，不替代人工检查差异、第三方许可证和新引入依赖。截图请使用虚构示例或获得授权并脱敏。
