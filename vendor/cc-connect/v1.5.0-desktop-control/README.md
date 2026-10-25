# CC Connect 本机控制适配版

基于上游 v1.5.0，公开构建固定提交为 `17c61062c2f9ce9bcdd45a2082e491f9743a2770`。本文件描述应用依赖，不是另一个项目。

- 当前发布版本：1.5.0-desktop-control.6；安装器固定校验该版本的网关二进制哈希。
- 配套网关包含 ACP 和官方 QQ 通道。
- 改动：原始消息身份、发送时间、重连边界及引用结构透传到 ACP；`SendWithMetadata` 会把它们写入实际 `session/prompt` 请求的 `_meta.cc_connect`，不会混入提示词；跨 CC Connect 内部队列保留元信息。群聊使用独立的精确“群 OpenID/成员 OpenID”白名单，默认关闭。
- 改动：有路由尾部的消息保存小型长期引用索引，按哈希分桶，只存对话编号/引用标识，不存完整历史正文。
- 独立任务提醒走 QQ 主动消息，不复用旧入站消息的被动回复编号/序号；普通即时回复保留被动回复。QQ 返回业务错误或缺少消息编号时不再当成成功。
- 配套源码补丁：[metadata.patch](metadata.patch)、[delivery.patch](delivery.patch)、[minimal-build.patch](minimal-build.patch)、[security.patch](security.patch)、[group.patch](group.patch)，按此顺序应用。最小构建补丁使上游命令入口也遵守排除开关，安全补丁使权限按钮增加白名单、操作者绑定、去重和一次性请求绑定；群聊补丁恢复官方群事件入口，但只接受明确的群/成员对，并保留异步问题引用尾部。上游原来的代理和其他模型逻辑不参与本机控制。
- 本仓库不跟踪作者本机运行目录里的二进制。普通用户使用 GitHub Releases 中由 Actions 从该固定提交构建的 Windows x64 网关；发布包附有 `BUILD_INFO.json`、SHA-256 校验、第三方声明和本说明。构建工具只输出本地候选文件的哈希，不将作者本机产物哈希冒充公开发布结果。

发布检查覆盖 ACP/QQ 包测试、核心元数据回归、组件标记和来源校验。上游完整测试套件的 Windows 兼容性不属于本适配版的验证承诺；手机实收需单独验收。

安装运行根目录的 `install.cmd`，自动获取并校验配套发布包。相同网关直接复用；其他版本按[更新说明](../../../docs/development.md#更新已有安装)处理。

发布入口为 `tools/ci_build_gateway.py`；组件标记必须保留 ACP 和 QQ。普通上游程序不包含本目录的元数据及投递适配，不能直接替换配套网关。

机器人认证、绑定和运行数据沿用原位置；本目录不保存凭据。
