# 独立通信中心 C 部署

代码入口：`python -m control_center.main --help`。本机集成测试使用回环 HTTP；这不是云部署。生产 C 必须运行在独立、持续在线的服务器上，不能依赖任何 B 进程。当前没有获授权云主机，因此公网端口、域名证书、备份存储和 MQTT broker 均尚未实际部署。

现有实现使用 Python 标准库 HTTPS 和 SQLite WAL，适用于第一版受控负载；配置边界和替换数据库的决策见 `architecture.md`。对外监听需要证书、私钥和精确 Origin，管理员凭据保存在私有运行目录。不要把测试 HTTP 暴露公网，不要关闭防火墙或证书校验。

先使用管理员 API 签发绑定 A/B 角色的一次性 enrollment code。节点主动登记后得到独立凭据，B 发布验证目录并签发逐对授权码；A 按 B UUID 配对。任务和文件的授权由 C 检查。没有默认共享万能设备密码。

HTTPS 始终提供权威任务状态。可选 MQTT TLS 通知已接入 A 原生 IDF 客户端、B 的有界客户端以及 C 的事务 outbox；主题为 `ab/v1/nodes/{UUID}/down/events`，仅携带事件标识并唤醒 HTTPS 查询。重复或延迟的通知不会直接执行任务。没有配置 broker 时继续轮询，不伪造 connected。真实 broker/云仍未验收。

`control_center/deploy/` 提供 Mosquitto 配置和无真实秘密的 JSON 模板。C 使用 `--broker-config` 读取私有配置，必须明确 `dedicated: true`：该 broker 为此 C 专用，因为初始化会将默认 ACL 改为拒绝，并创建独立 publisher 角色。管理员和 publisher 必须是不同凭据。节点调用 `/api/v1/mqtt/session` 前必须通过当前 HTTPS 身份、实例 epoch 检查；C 为其配置唯一 client ID 和精确接收/订阅主题，不允许节点发布或跨节点订阅。凭据轮换先禁用旧 broker 身份。模板不自动安装或配置任何主机。

服务器初始 dynsec 管理员需在获授权服务器通过 Mosquitto 管理工具创建，再填写私有配置，禁止把秘密放命令行或提交 GitHub。模板中的插件路径需要按实际服务器确认。协议依据：[MQTT 3.1.1](https://docs.oasis-open.org/mqtt/mqtt/v3.1.1/os/mqtt-v3.1.1-os.html)、[Mosquitto Dynamic Security](https://mosquitto.org/documentation/dynamic-security/)。

备份必须同时包含一致性数据库和私有文件，另行安全备份凭据；恢复后检查权限和 fencing，不自动重发已经终结的任务。上线前还需完成证书负例、数据保留清理、限流负载和长时间资源水位验证。
