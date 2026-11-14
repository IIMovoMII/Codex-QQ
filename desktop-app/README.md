# Windows 桌面应用

Tauri 2 原生宿主、Svelte 5 界面、随包 Python 与固定 QQ 网关。原生版不需要 Node、Python 开发环境或浏览器开发服务器。所有密钥、QQ 绑定与运行数据留在本机。

普通用户下载 [1.0.2 中文安装包](https://github.com/IIMovoMII/Codex-QQ/releases/tag/app-v1.0.2)，按[首次安装](../docs/install.md)配置。以下为开发与发行说明。

## 开发

在 Windows 安装 Node.js 24、Python 3.13、Rust GNU 与 MinGW UCRT GCC，使用系统 Edge / WebView2。`npm ci` 安装固定前端依赖，`npm run check`、`npm test`、`npm run build` 验证界面；测试不下载 Chrome。Rust 使用 `src-tauri/rust-toolchain.toml` 与 Cargo.lock。

从仓库根运行 `tools/build_desktop.ps1 -NoBundle` 检查 release 原生构建，完整安装包构建省略该参数。构建只下载固定哈希 Python 和固定网关，并收集许可证；不能绕过完整性验证。签名私钥通过 `TAURI_SIGNING_PRIVATE_KEY` 或构建机专用存储提供，永不提交。中文 NSIS 为当前用户安装，WebView2 缺失时由微软安装器处理。

## 验收和发行

界面、QQ 实际往返、安装回退、干净 Windows、原生 DPI 与真实登录自启分别保留验收证据。`--test-profile <绝对目录>` 隔离应用与 Codex 数据，仅用于验收；该参数错误时退出，不回退使用真实数据。`--health-check` 只验证安装资源，不证明 QQ 已连接。

原生更新只接受固定公钥签名且版本与签名绑定的应用安装包，拒绝降级；网关标签与应用 `app-v…` 标签分开。安装前等待任务、投递与配置事务安全，程序回退保留最新运行数据。窗口关闭进入托盘，“退出助手”才关闭所属服务。

`tools/package_desktop_release.py` 从通过验收的同一安装包准备签名、哈希、许可证和中文说明；`tools/publish_desktop_release.py` 检查本机 G1–G6 证据、提交与资产哈希后发布并回下载验证。它不重建、不覆盖已有 tag/release，不上传本机证据。先将准备出的 `latest.json` 同步为本目录 `update-channel.json` 并提交，再发布；渠道在资产上传期间可能暂时不可下载，客户端保持当前安装。

源码清理后不重打已发布安装包；有运行代码变化就另发新版本。开发预览和 `src/preview/fixtures.ts` 继续用于 Edge 状态测试，原生模式禁止样例回退；源码安装兼容入口与桌面宿主复用 Python 服务。

桌面 CI 只检查源码与原生构建；没有人工和真机验收时不会自动制作或发布成品。源码提交中的更新公钥不是 Windows Authenticode 证书，两者独立。
