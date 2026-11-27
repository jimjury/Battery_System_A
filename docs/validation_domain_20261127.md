# 2026-11-27 域名验证记录

目标：`c.jueyjim.loc.cc`，服务器 `60.204.129.7`。用户已添加 A 记录，并明确告知尚未备案或接入备案。

| 验证 | 实际结果 |
|---|---|
| 本机、服务器、公共 DNS | 解析为 60.204.129.7 |
| 公有证书重试 | 被 `loc.cc` 共享根域每周配额拒绝；返回下次 2026-11-27 02:17:13 UTC；未获得公有证书 |
| 项目 CA 域名叶证书 | 生成并部署；仅叶证书/叶私钥上传，CA 私钥保留本机 |
| C 更新 | 不可变版本 3.1.1-domain-20261127；升级前备份并验证原 IP HTTPS 健康 |
| 本机真实 SNI 测试 | 同一监听端口按域名选择不同叶证书，原 IP 仍校验通过；通配符/URL/重复名称拒绝 |
| 完整回归 | **80 tests / 37.879 s / OK** |
| 服务器 loopback 域名 TLS/HTTP | 主机名/项目 CA 校验通过，/health 返回 center/ok |
| 服务器连接自身公网 IP，SNI=域名 | OpenSSL TLSv1.3、证书校验通过；仅服务器侧对照 |
| PC → 公网 IP，不带域名 SNI | TLSv1.3 及项目 CA 校验通过 |
| PC → 同一公网 IP，SNI=目标域名 | ConnectionResetError，不能认定域名公网可用 |
| 错误主机名负例 | 到达证书校验后主机名不匹配拒绝；没有关闭证书验证 |
| 有限 TCP 头诊断 | 捕获目标 SNI ClientHello 及后续入站 RST；没有保存报文内容或业务数据；不足以独立判定拦截设备 |
| A/B 配置迁移 | 未执行，仍使用原 IP 入口，UUID、凭据、配对和数据保持 |

服务器侧成功与公网 PC 失败分别记录，不把服务器内验证冒充外部成功。公有证书配额与公网域名重置是两个独立问题，不能假定等待签发就能解决后者。

用户未备案这一事实与华为云说明相符，具体拦截原因待云平台确认：[华为云官方说明](https://support.huaweicloud.com/domain_faq/domain_faq_040112.html)。不修改 Windows hosts、路由或全局 CA，不尝试改端口规避域名拦截。

源码新增可回退的域名配置器、只读 DNS 检查器、需叶证书指纹/双入口配对状态一致的客户端迁移工具。后者在域名未验证前不会运行或覆盖身份。主机备份恢复逻辑已保留配置目录所需的服务组读取权限。

详细脱敏记录见 `test_records/domain_20261127`；没有保存或发布 SSH 密码、节点凭据、CA/叶私钥和 ACME 账户密钥。

Final original-origin browser regression passed: actual PC A -> public IP C -> B returned 3 words, zero JS exceptions. See test_records/domain_20261127/original-origin-browser.json. This is not domain public acceptance.
