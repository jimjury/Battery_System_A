# PC A applicability of the V3 forty tests

Original hardware-specific criteria remain required for ESP32 A. This table does not grant hardware acceptance from PC results.

| ID | Requirement | PC applicability and actual evidence |
|---|---|---|
| T01 | 现有环境与构建 | Existing Conda and IDF Python reused; 78 host regressions passed. No ESP32 rebuild needed for PC code. |
| T02 | 原生 USB/Win10 | ESP32-specific criterion; PC A uses local HTTP and its OS network. Hardware/update acceptance unchanged and not granted. |
| T03 | 电脑完全断网 | ESP32-specific criterion; PC A uses local HTTP and its OS network. Hardware/update acceptance unchanged and not granted. |
| T04 | A 配网/重连 | ESP32-specific criterion; PC A uses local HTTP and its OS network. Hardware/update acceptance unchanged and not granted. |
| T05 | DHCP/新电脑 | ESP32-specific criterion; PC A uses local HTTP and its OS network. Hardware/update acceptance unchanged and not granted. |
| T06 | GitHub HTTPS | ESP32-specific criterion; PC A uses local HTTP and its OS network. Hardware/update acceptance unchanged and not granted. |
| T07 | UI 远程更新 | ESP32-specific criterion; PC A uses local HTTP and its OS network. Hardware/update acceptance unchanged and not granted. |
| T08 | UI 错误包及恢复 | ESP32-specific criterion; PC A uses local HTTP and its OS network. Hardware/update acceptance unchanged and not granted. |
| T09 | OTA 与回退 | ESP32-specific criterion; PC A uses local HTTP and its OS network. Hardware/update acceptance unchanged and not granted. |
| T10 | C 独立部署 | Public independent C installed and TLS verified. Real MQTT broker ACL pending. |
| T11 | 设备注册/身份 | Local persistence/forgery/replay tests and actual cloud A/B enrollments verified. |
| T12 | 多对多发现 | Local simulated 2A x 2B tested; actual public current PC A/B verified. |
| T13 | 配对授权 | Local bound/expired/replayed codes denied; actual cloud pairing verified. |
| T14 | 撤销隔离 | Local multi-pair isolation and real cloud revoked access verified. |
| T15 | ACL 与对象越权 | Real cloud unauthorized task/file reads denied; real MQTT broker pending. |
| T16 | B 本地离线推理 | Local B inference without C regression passed; physical network-disconnect UI pending. |
| T17 | 环境自动/手动 | Partially covered by regression; full criterion not accepted yet. |
| T18 | 多环境隔离 | Partially covered by regression; full criterion not accepted yet. |
| T19 | 模型多权重 | Actual independent demo variants passed; not research weights. |
| T20 | 动态目录 | Partially covered by regression; full criterion not accepted yet. |
| T21 | 非电池任务 | Actual local/public CSV and text generic demo passed. |
| T22 | B 便携迁移 | Copied package runs with second existing interpreter; another physical PC pending. |
| T23 | 并行本地/远程 | Partially covered by regression; full criterion not accepted yet. |
| T24 | 单 A 多 B | Local two-B child tasks verified; actual public two-B test pending. |
| T25 | 单 B 多 A | Local concurrent A1/A2 ownership tests; public multi-PC topology pending. |
| T26 | 文件完整性 | Partially covered by regression; full criterion not accepted yet. |
| T27 | 文件续传 | Partially covered by regression; full criterion not accepted yet. |
| T28 | 真实体全链路 | PC variant passed: Edge -> PC A -> public HTTPS C -> local B -> PC A. ESP32 PC-offline/Wi-Fi criterion is not passed. |
| T29 | 双端结果 | Actual public result SHA matched and PC export succeeded. |
| T30 | B 重启/迁移 | Partially covered by regression; full criterion not accepted yet. |
| T31 | MQTT 重复/乱序 | Wire fixture duplicate/ACK tests only; broker not deployed. |
| T32 | C 断线 | Partially covered by regression; full criterion not accepted yet. |
| T33 | A 换电脑/拔线 | Partially covered by regression; full criterion not accepted yet. |
| T34 | 取消和晚到结果 | Partially covered by regression; full criterion not accepted yet. |
| T35 | API/SDK | Real PC A/B/C calls and scoped-key denials passed; full OpenAPI schemas remain partial. |
| T36 | MCP/AI | Partially covered by regression; full criterion not accepted yet. |
| T37 | 真实模型 | Blocked: actual SOC/SOH weights, preprocessing and reference samples not supplied. |
| T38 | 安全数据/秘密 | Secrets excluded from source/package. C admin token download rejected and not done. |
| T39 | 长时/资源 | Prior 180s local B only; no new long-duration acceptance. |
| T40 | 备份与交付 | Actual C install/upgrade/private restore/failed-start rollback verified; second server/hardware recovery pending. |
