# C 安装、升级、迁移与恢复

当前已部署 `https://60.204.129.7`，Ubuntu 24.04 / 现有 Python 3.12，systemd 服务 `ab-center`。使用项目专用 CA；PC A/B 显式加载 `client_config/cloud-ca.pem`，不更改系统证书存储。没有域名不影响客户端实际 TLS 校验。浏览器直接打开 C 时需要用户自行信任此 CA；不要关闭证书验证。

## 在另一台 Linux 服务器快速安装

1. 准备已有 Python 3.10+、systemd、空闲 HTTPS 端口和 SSH 管理权限。安装器不执行 pip、apt、Docker pull 或重装 Python；缺少必要环境会报错停止。
2. 将新服务器 SSH 参数放到本机忽略目录中的私有 JSON，例如 `{"host":"服务器IP","port":22,"username":"root","password":"仅保存在私有文件"}`，也支持 `key_file`。不把密码放命令行、GitHub 或报告。
3. 首次用 SSH 与服务器控制台核对主机密钥，加入私有 known_hosts。当前服务器已固定首次连接观察到的主机密钥；部署工具使用 RejectPolicy 拒绝未知或变化的密钥。
4. 在此工程运行：`python tools/deploy_center.py --credentials .local/new-server.json --known-hosts .local/known_hosts --version 3.1.0-your-release`。复用本机已有 paramiko/cryptography。每个新版本名不可变，不能覆盖不同内容。
5. 脚本生成并检查源码 SHA-256 包，通过 SSH 上传至独立 staging，调用 `deploy/install_center.py`。服务使用独立账户、TLS、最小文件写权限和开机自启；健康检查失败则恢复上一代码版本、配置和一致性数据备份。成功后更新客户端公开地址/CA profile。

源码在 `/opt/ab-center/releases/{version}`，`current` 指向活动版本；私有 DB、附件和管理员凭据在 `/var/lib/ab-center`；服务端证书/密钥在 `/etc/ab-center`。原有 Docker 服务和 SSH 设置保持不变。

## 同一服务器升级

本地测试通过后，用新的不可变 `--version` 再运行部署命令。安装前自动停止 C 并备份一致性状态；校验新源码、切换链接、启动服务、验证证书及 `/health`。升级和测试会有短暂服务中断，应避开活动任务。普通 GitHub 源码提交不触发未验收的自动生产升级。

## 私有备份与迁移

在服务器运行 `sudo python3 maintain_center.py backup --file /安全目录/center-private.zip`，其中脚本来自工程 `deploy/maintain_center.py`。它停止托管服务后流式保存数据/配置并生成逐文件哈希，然后恢复运行。备份包含设备凭据、私有文件和 TLS 私钥，仅限私有存储，禁止加入公共发布包。

新服务器安装同版本代码后，上传私有备份，运行 `sudo python3 maintain_center.py restore --file /安全目录/center-private.zip`。恢复前再备份目标现有状态，只操作两个固定托管根目录。切换 IP 时需要签发匹配新 IP 的 TLS 证书、更新客户端 profile 并执行受控身份迁移；不能把旧 IP 证书直接用于新 IP。保留同一 C 数据库可保留 UUID、配对、任务历史；不要并行运行两个可写的同身份 C。

实际已验证：本服务器首次安装、一次正常升级、私有备份下载/恢复、故意启动失败候选的自动回退、C 重启后历史可查。第二台实体服务器迁移尚未验证。

## 管理登记与 broker 状态

C 网页提供管理员登录、节点列表和一次性 A/B 登记码；也可运行 `python tools/center_admin.py enrollment --role A`，手动输入管理员凭据，不保存。现有 A/B 日常启动不需要 C 管理员凭据。自动审批拒绝了将 C 管理员令牌下载保存到本机，交付中改用已登记的普通 A/B 身份，不提供此自动下载步骤。

当前云 C 已实测 HTTPS 权威通道；没有安装/配置真实 Mosquitto broker，MQTT 为明确的轮询回退，不标记 MQTT 云验收通过。可选 broker 源码/模板见原 `deploy_center.md`，后续可独立启用。
