# 开发与维护

先从[维护入口](../MAINTENANCE.md)定位相关模块，不为小改动通读历史。Python 运行部分使用标准库；旧包名 `codex_desktop_weixin` 保留兼容。桌面应用为 `desktop-app/` 的 Tauri/Svelte 与 Python 服务；发布成熟度看实测，不看目录存在。

## 实现约束与源码导航

下表 Python 模块位于 `src/codex_desktop_weixin/`；限额、超时、协议字段以源码常量和对应测试为准，文档不复制数值快照。

| 范围 | 必须保留的行为 | 入口 |
| --- | --- | --- |
| 引用与身份 | 每条消息独立解析可信引用；长期卡片按稳定 thread ID 核对 owner，目录暂缺不能按标题猜。新引用带 QQ scope；纯旧 ID 只兼容已核实的同一用户私聊，群聊/未知拒绝。分卷编号仅凭本机证据映射；保留原始消息时间与去重 | `control_router.py`、`qq_desktop_bridge.py` |
| 普通发送与队列 | 旧连接空闲/暂停经 owner 开始轮次，忙时走原生队列。新版用本机工具校验引用的对话，由配套 `codex queue` 受理正文，Codex 在对话空闲时取出并开始一轮。暂停续接和队列管理在电脑完成。按 thread/queue ID 核对回执，提交状态不明时禁止补发 | `control_backend.py`、`submission_receipts.py` |
| 超大状态 | 有界排空 follower 广播，按需 `read_thread` 读最近一轮；仅 idle、已结束、resumed、无活动标记且 owner/连接复核一致才允许开始。明确忙时可追加队列。答复/权限/分支/压缩缺少必需字段就拒绝，指出缺项与是否已调用，不伪造空列表/默认值 | `desktop_control.py`、`control_backend.py` |
| 原生能力 | 接受当前 `codex-app-tools-*` 与旧工具名；按实际进程与参数核验，缓存绑定进程身份。发现限候选与总时长，仅明确调用时进行；先验服务进程再列工具，写前再验。非法显式端点不回退；调用开始后不换端点重发 | `native_app_tools.py`、`control_contract.py` |
| 状态与通知 | 原生事件先于迟到钩子，Stop 不单独等于完成；只跳过目标轮次前历史，成功归并后提交游标。坏时间戳跳过并标记不完整，不能用当前时间补。通知事实进账本，索引有界且损坏可隔离恢复；dead-letter 终态不复活。清理不碰 pending、活动任务和 Codex 文件 | `monitor.py`、`tailer.py`、`state_machine.py`、`notifier.py` |
| 安装与恢复 | 配置先写校验进度再原子发布，兼容旧进度；持锁才能恢复，`--check` 只读。未知归属文件保留。配置网关在 CreateProcess 时原子加入私有 kill-on-close Job，句柄不继承，仅传本次必要输入/日志句柄；禁止先启动再补挂 Job | `tools/configure.py`、`tools/setup_process.py` |
| 进程与就绪 | PID、程序与创建时间在同一已核验句柄上复核/终止，探测未知则保留，不按名字清进程。配置/正式网关 READY 分开，从本次日志起点核验；ACP 首条接收边界基于已验证就绪，不依赖预热。常驻/monitor 共用实例锁，独立入口不能随 Codex 作业组退出 | `resident_app.py`、`tools/ResidentLauncher.cs`、`tools/pairing_agent.py` |
| 钩子 | 只合并自身处理项与明确启用 hooks，语义回读保证其他 TOML 不变；不可安全解析则停止。输入有界，异常安全继续并脱敏诊断；安装、启用和用户信任分别核对 | `installer.py`、`hook_bridge.py` |
| 桌面桥接 | UI 不直接拼命令/写配置；状态来自有时效的服务证据，开发样例不能冒充真实数据。操作有编号、阶段和查询，超时不重放；秘密不进 argv、URL、日志、界面持久存储或诊断包 | `desktop_bridge.py`、`desktop_migration.py`、`desktop-app/` |

异步问题只提示去电脑回答；分支仅最近完成位置，新建对话先明确项目与首条任务。改手机文案时保留 `对话编号：`、`引用标识：` 和异步问题尾部路由契约；用短段落，不依赖 QQ Markdown，不改模型正文。

## 测试

先复现，改动后跑对应回归；新测试应验证实际行为边界，不镜像实现。核心流程：

```powershell
py -3 -X utf8 -m unittest discover -s tests -p "test_smoke.py" -v
```

发布或修改底层状态、安装/Windows 生命周期时跑全量 `py -3 -X utf8 -m unittest discover -s tests -v`。提交前运行 `py -3 -X utf8 tools/check_publication.py` 和 `git diff --check`。纯文档改动检查链接、公开边界和差异即可；通过后不无故重复全量。

前端按 `desktop-app/package.json` 的 check/build/test 执行，Rust 检查匹配实际目标与锁文件；浏览器测试使用已安装 Edge，不能下载 Chrome。原生窗口的 DPI、托盘和生命周期需 Windows 实测，浏览器结果不能替代。

安装专项：`test_setup_process.py` 检查真实 Job/控制台关闭，`test_setup_recovery.py` 检查故障恢复，`test_setup_diagnostics.py` 检查构建诊断。启动器单测输出到隔离目录，不覆盖现用 bin/网关；其他终端宿主的关闭、READY/OpenID 中断、网络和平台失败另做隔离实测。

CI 覆盖 Python 3.11/3.13 和固定网关构建；单元测试不用真实 QQ 或模型。首次安装无需开发全量测试。分别记录自动测试、正式服务就绪、QQ 聊天正文实收、Windows 实际登录启动；不把其中一种当作全部成功。

## 网关与发布

固定基线依次应用 metadata、delivery、minimal-build、security、group 补丁；保留原消息/引用/重连边界、持久引用索引、主动通知发送及真实投递错误。仅构建 ACP/QQ，校验来源、哈希、BUILD_INFO 和能力，不能只看版本号。群聊精确限定群/成员，身份查询不授权，权限按钮绑定操作者。基线与许可见[第三方声明](../THIRD_PARTY_NOTICES.md)。

现有网关流程由 `ci_build_gateway.py` 与 `.github/workflows/` 管理。网关改变才发布新标签并同步下载器校验值；不覆盖旧标签，不为仅 Python/文档修改重发同一网关。桌面安装包及更新须单独验证。

Git 只收源码、测试、公开文档及合法资源；排除本机计划、配置、凭据、日志、历史、构建二进制与运行元数据。发布检查器不能替代暂存差异、资产内容与依赖许可证检查；截图用虚构数据或经授权脱敏。

## 代码与文档清理

先查运行路径、模块引用、测试、构建和文档入口，确认废弃后再移出源码；无引用的探索脚本和重复包装器可归档移除。源码安装仍使用 `install.cmd`、`tools/install_app.py`、C# 启动器与共享 Python 服务；预览样例用于前端测试，这些不按“旧代码”删除。

本机旧程序、候选产物和历史任务文档集中归档并附原路径与哈希；不把个人归档提交到 GitHub。保留当前版本、必要回退、配置和绑定。仓库清理用正常提交同步本地与远端，不改写历史，也不删除仍被下载器或升级使用的 release/tag。用户入口以 [README](../README.md)、[安装](install.md)、[操作](usage.md)和[排错](troubleshooting.md)为准，变更记入 CHANGELOG。

## 更新已有安装

1. 检查当前改动、安装归属与数据位置；先在候选目录构建/校验，旧入口可回退。保留实例锁、维护锁与绑定，不用首次配置覆盖已有安装。
2. 需要替换时退出已核实的 QQ 助手及其网关，保留 Codex Desktop。仅网关哈希变化才替换网关，并暂留一份已验证旧程序供失败恢复。
3. 移动源码须完整保留项目与 Git；数据留原目录。备份后同步启动器、运行元数据、settings 中网关路径、网关桥接脚本/work_dir、目录历史、快捷方式及已启用的登录自启。
4. 只更新本程序钩子路径并语义回读；信任变化由用户在 Codex 处理。不改模型、认证、密钥或 OpenID，不重做绑定。自启需独立系统视角回读，不能只信当前进程看到的注册表。
5. 在新位置验证配置、单实例和正式就绪，再按本会话授权验收 QQ；普通路径迁移本身不授权发消息。实际登录验证与静态自启检查分开，不能擅自注销/重启电脑。
6. 通过后只清理确认不再引用的本应用旧文件，保留必要回退期限；不删除运行数据、其他项目或归属不明文件。更新公开用法，本机结果仅更新本地状态。
