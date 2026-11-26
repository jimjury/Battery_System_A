# PC A 与云 C 实际测试：2026-11-26

新工程 `F:\ESP32_Projects\AI_Platform_PC` 基于原工程提交 `ef97a76` 复制，原 ESP32 工程和板上固件保留。原 V3 总纲先备份，再加入 PC A 修订；完整规范在 `SPEC_V3.md`。没有重装本机 Python、Conda、IDF 或工具。

C：用户授权的 `60.204.129.7`，Ubuntu 24.04.4，复用 Python 3.12.3。保留已有 Docker 服务，新增 `ab-center` 独立账户/systemd 服务与 HTTPS 443。证书含 IP SAN，由项目专用 CA 签发；客户端显式验证，不关闭 TLS，不修改系统证书存储。

| 编号 | 实际操作 | 结果/边界 |
|---|---|---|
| PC-L01 | PC A HTTP→本地 C HTTP→实际 B 子进程，CSV [2,5]/[3,8] | scale-two 实际返回 `[15,23]`，附件 SHA 一致 |
| PC-L02 | A API scope、撤销 key、任意代理 | 越权与撤销后请求拒绝 |
| PC-L03 | 跨站登录、缺 CSRF 写请求、查询状态 | 拒绝跨站/缺 CSRF；状态不返回 C 凭据 |
| PC-L04 | A 重新创建、复用运行目录 | UUID/登记凭据持久化 |
| PC-B01 | 现有 Edge 操作 PC A 上传并经本地 C/B 推理 | 3 个词，0 捕获 JS 异常 |
| PC-B02 | 已持久化接单后丢弃一次回执再重试 | 一个任务，同一逻辑幂等键 |
| CLOUD-01 | SSH 只读检查、固定首次观察的 host key、首次安装 | 现有 Python 安装 C 后健康检查成功 |
| CLOUD-02 | PC 公网 443、项目 CA 验证 HTTPS | center/ok；未关闭防火墙/安全组 |
| CLOUD-03 | PC A→公网 C→本机 B→C→A，真实执行/下载 | `[15,23]`，输入与附件 SHA 一致 |
| CLOUD-04 | 同幂等键重复提交、未配对 A 读取 task/file | 一个 group，越权拒绝 |
| CLOUD-05 | 实际重启云 C，再读历史；撤销配对 | 历史哈希保留，撤销后结果拒绝，重新配对不恢复旧权限 |
| CLOUD-06 | 正常升级为带管理界面的 C | 一致性备份完成，新版本健康，旧版本保留 |
| CLOUD-07 | 停服务备份、下载、实际恢复 | 10 文件，29,252 B，逐文件哈希通过，恢复后公网健康 |
| CLOUD-08 | 安装明确用于测试的启动失败候选 | 自动恢复 `/opt/ab-center/releases/3.1.0-pc-ui-20261126`，公网 HTTPS 健康；失败候选未发布 |
| CLOUD-BROWSER | 现有 Edge→日常 PC A→公网 C→日常 B | 实际文本结果 3 个词，demo 标识，0 JS 异常，未绕过 A 或关闭 TLS |
| PORTABLE-01 | 41 文件 A+B 包复制到新目录，使用现有 IDF Python 3.11 | A 页面载入，B 实际结果 4 个词，新 UUID，无身份/私钥；不是第二实体电脑 |
| REGRESSION | Windows 完整 unittest | **78 项，37.215 秒，全部通过**；含历史模块，不等于 40 项全部验收 |

公网 CSV 任务 `8d677aaa-a54f-46e2-8e82-a232f4eab47a`；输入 SHA `0ae6f833d683cba0e5faff3efc98f14686f096f99b1e1c0854c6a05141f9e4b6`，附件 SHA `d5b7d4bcdf66f4cf78fc196f95e9984372055b0ce8ea8ec48d21c299f57390a2`。真实浏览器公网任务 `318dad44-2d71-46e4-8af8-90d0c0e2cb2f`。这些都是实际执行的 demo 插件，不能代表 SOC/SOH。

私有备份 SHA `e2e49a03aeb886b2f46b65de4fc7b595486e34c6b887d04f7badd180f9ad70b0`。备份含凭据、私有文件及 TLS 配置，仅保留本机忽略目录/服务器私有目录，不提交 GitHub。

修复记录：Windows 导入 Linux `pwd` 导致部署包测试失败，改为 Linux 安装时才要求该模块；Edge 闭环后 Crashpad 占用临时文件导致退出失败，改为 CDP 正常关闭并重跑成功。这些失败没有掩盖为最初即成功。

自动审批拒绝下载/保存 C 管理员令牌，理由是提取高权限凭据超出本地实例启动所需；该操作未执行。日常 A/B 改用此前授权测试已在本机创建的普通身份，不保存 C 管理员令牌。

待验：真实云 MQTT broker/ACL 未部署，当前使用实际 HTTPS 轮询；ESP32 USB/Wi-Fi/OTA/断电未验；真实 SOC/SOH 权重/预处理/参考样本未提供；第二服务器、其他电脑/OS、长期资源增长未验。本轮未改变 Windows 网络共享、云 SSH 认证方式或已有服务。

详细原始记录在 `.local`；脱敏 JSON/测试输出在 `docs/test_records/pc_20261126`。密码、设备凭据、私钥和私有备份不进入公开记录。
