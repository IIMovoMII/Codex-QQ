# 第三方组件

## CC Connect

- 项目：[chenhg5/cc-connect](https://github.com/chenhg5/cc-connect)。感谢上游提供 QQ 官方机器人通道、ACP 和本地投递服务。
- 本项目适配基线：`v1.5.0`，提交 `17c61062c2f9ce9bcdd45a2082e491f9743a2770`。
- 许可证依据：[该提交 README 的 License 声明](https://github.com/chenhg5/cc-connect/blob/17c61062c2f9ce9bcdd45a2082e491f9743a2770/README.md#-license) 为 MIT。此基线未提供单独的 LICENSE 文件；这里保留其原始声明和来源，不替上游补写版权归属。
- 原有代码归上游作者及贡献者所有，本仓库的 MIT 声明不替代第三方权利声明。
- `vendor/cc-connect/v1.5.0-desktop-control/` 保存本项目的适配和精简构建补丁；上游完整源码由发布流水线按固定提交获取。Git 仓库不提交运行二进制；GitHub Releases 另行分发从该源码与补丁生成的配套网关，并附构建来源、校验值、本声明和适配说明。
- Go 传递依赖由固定上游源码的 `go.mod` / `go.sum` 解析，各自遵守其许可证。若再分发编译产物，请同时审查并保留相应声明。

## 运行环境与产品名称

Python、Go、Windows/.NET、QQ 和 Codex 各自属于其提供方。Codex-QQ 是独立社区项目，不隶属于 OpenAI 或腾讯，也不分发 Codex 客户端或任何账号凭据。
