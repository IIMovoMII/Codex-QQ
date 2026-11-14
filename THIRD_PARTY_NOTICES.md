# 第三方组件

## CC Connect

- 项目：[chenhg5/cc-connect](https://github.com/chenhg5/cc-connect)。感谢上游提供 QQ 官方机器人通道、ACP 和本地投递服务。
- 本项目适配基线：`v1.5.0`，提交 `17c61062c2f9ce9bcdd45a2082e491f9743a2770`。
- 许可证依据：[该提交 README 的 License 声明](https://github.com/chenhg5/cc-connect/blob/17c61062c2f9ce9bcdd45a2082e491f9743a2770/README.md#-license) 为 MIT。此基线未提供单独的 LICENSE 文件；这里保留其原始声明和来源，不替上游补写版权归属。
- 原有代码归上游作者及贡献者所有，本仓库的 MIT 声明不替代第三方权利声明。
- `vendor/cc-connect/v1.5.0-desktop-control/` 保存本项目的适配和精简构建补丁（含元数据、投递、安全和可选群聊白名单适配）；上游完整源码由发布流水线按固定提交获取。Git 仓库不提交运行二进制；GitHub Releases 另行分发从该源码与补丁生成的配套网关，并附构建来源、校验值、本声明和适配说明。
- 网关的间接依赖以固定上游提交的依赖清单及校验文件为准，各自遵守其许可证。再分发时应审查并保留相应声明。

## 运行环境与产品名称

桌面安装包包含固定版本 Python（许可全文在 `python/LICENSE.txt`）、WebView2 Loader、Tauri/Rust 和 Svelte/Lucide 依赖。构建从 Cargo.lock、package-lock.json 与原始许可证收集 `licenses/DEPENDENCIES.json`、`licenses/THIRD_PARTY_LICENSES.txt`，随包分发；构建工具也保守列入。缺少许可证时构建停止。WebView2 运行时由微软安装与维护，不附带 Chrome。

MPL-2.0 组件保持原样，源码可从依赖清单中的精确 crates.io 版本链接获取；本项目对这些组件没有修改。缺失于 crate 的根许可证从对应源码提交保留在 [desktop-app/licenses](desktop-app/licenses)，其来源与哈希一并记录。微软 Loader 的许可来源和版本也在该目录保留。

Python、Windows/.NET、QQ 和 Codex 各自属于其提供方。Codex-QQ 是独立社区项目，不隶属于 OpenAI 或腾讯，也不分发 Codex 客户端或任何账号凭据。
