# 域名接入计划与实测

## 2026-11-27 当前结果

用户已添加 `c` 的 A 记录，PC、云服务器和公共 DNS 均确认 `c.jueyjim.loc.cc -> 60.204.129.7`。服务器已部署 `3.1.1-domain-20261127` 和域名 SNI 叶证书，保留原 IP 证书；域名证书由现有项目 CA 签发，CA 私钥没有上传。服务器本地及服务器到自身公网 IP 的域名 TLS 校验通过，不能将其当作 PC 公网通过。

PC 实测：IP TLSv1.3 校验通过；同一 IP 带目标域名 SNI 时被 ConnectionReset 重置；错误名称对照能进入证书主机名校验并被拒绝。有限 TCP 头诊断看到带目标 SNI 的 ClientHello 及后续入站 RST，没有捕获业务数据。单凭这些现象不能精确确定拦截设备。

用户明确确认尚未完成备案/接入。该现象与华为云关于大陆地域域名备案接入的说明一致，但云平台最终拦截原因仍待控制台/客服核实。参考：[华为云域名访问排查](https://support.huaweicloud.com/domain_faq/domain_faq_040112.html)。不通过更换端口或关闭 TLS 检查绕过拦截。

公有证书本次重试仍因共享根域 `loc.cc` 的每周配额拒绝，返回下次时间 `2026-11-27 02:17:13 UTC`（北京时间 10:17:13）。没有获得公有证书；项目 CA 不等同浏览器公有信任。未配置定时重试，也不重复消耗申请限额。

因此没有迁移 A/B 的 C 地址或更换身份。现有 IP + 项目 CA 入口保留；后续需要核实域名可备案接入性，或在用户授权的其他地域服务器继续验证。此域名为平台分发子域名，能否办理备案需平台/华为云确认，不能保证。

新 SNI 与 Host 配置测试通过；完整回归 **80 项，37.879 秒通过**。记录见 `validation_domain_20261127.md`。下面保留原接入准备记录，当前状态以上述结果为准。

用户提供 `jueyjim.loc.cc`。2026-11-26 本机实际解析根域名/`www` 到 GitHub Pages 地址；两者现有 HTTPS 均报主机名不匹配。`c.jueyjim.loc.cc` 和 `api.jueyjim.loc.cc` 暂无解析。截图显示 NS 未同步，最终以实际权威 DNS 生效为准。

保留根域名与 `www` 的原有用途。计划增加独立 A 记录：名称 `c`，值 `60.204.129.7`，仅 DNS，TTL 默认。当前没有用户 DNS 管理连接，已请用户添加，尚未擅自修改任何 DNS。

DNS 正确后：核对公网记录及端口 80 → 使用 ACME HTTP-01 验证控制权并签发公有证书 → 为 C 加入该域名的 SNI 证书和明确 Host 许可 → 检查域名 HTTPS 和 API → 再受控迁移 A/B 的 C 地址，保留 UUID/配对/数据。原 IP + 专用 CA 继续提供过渡访问，不先破坏当前可用链路。

SNI 支持已加入源码，尚未部署/验收真实域名证书。默认 IP 证书和额外域名证书可共存，限制为明确的至多八个名称，不接受通配符/URL/重复名称。域名证书不会默认信任或放宽 Host/证书检查。

服务器当前未发现 certbot/nginx/caddy，80 未占用，现有 C 的 443 正常。DNS 尚未就绪前不发起证书申请，避免失败申请和无意义的限额消耗；没有安装或重装本机环境。

复查：`python tools/check_domain.py --host c.jueyjim.loc.cc --ip 60.204.129.7`。只有 `dns_ready` 和实际证书验证通过后才切换客户端；不能用 hosts 文件或关闭 TLS 冒充公网 DNS 成功。

依据：[Let's Encrypt 验证方式](https://letsencrypt.org/docs/challenge-types/) 与 [Certbot standalone](https://eff-certbot.readthedocs.io/en/stable/using.html#standalone)。HTTP-01 要求公网 80 可访问；若 DNS/安全组/平台限制无法满足，再选择明确授权的 DNS-01 方式。
