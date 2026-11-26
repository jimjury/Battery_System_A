# 安全与权限边界

USB A 仅服务 192.168.7.1 与直连 192.168.7.2，校验 Host/Origin；首次短时配对在 USB 进行，设备凭据写入 NVS，不编译进公共固件。会话采用 HttpOnly/SameSite、写请求 CSRF；开发部署使用一次性 HMAC challenge。API key 仅保存哈希，可按 scope 撤销。

C 逐次检查角色、设备凭据、配对关系、任务所有权、attempt 与实例 epoch。设备发现不代表授权；撤销配对后旧任务不因再次配对恢复权限。文件私有，分块上传计算分块和整文件 SHA-256，下载逐块复核权限。B 不开放远程代码安装，科研权重不上传 C/GitHub。

公共网络要求有效 TLS 证书，禁止关闭验证。B/C 回环 HTTP 仅用于明确的开发测试；C 对外监听还要求 TLS 和明确的 Origin。MQTT 通知链路尚未实现/验收，当前以 HTTPS 轮询为权威，不能据此声称完成 MQTT ACL 测试。

USB 与 Wi-Fi 不做桥接、NAT 或共享；编译配置禁用 lwIP forwarding/NAPT。Windows IPv4 forwarding 只读检查为关闭，ICS COM 读取权限不足，不能据此宣称 ICS 已完整验收。没有更改主机网络、重装环境或写 eFuse。

签名私钥与每机凭据保存在忽略目录 `.local`；运行状态在 `.runtime`。发布前扫描源码、轻量包和固件，公共发布禁止包含设备密钥。发布者私钥需要离线备份，丢失后只能按明确密钥迁移方案更新固件，不能静默换钥。
