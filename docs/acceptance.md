# V3 40 item acceptance ledger

Software tests do not imply physical ESP32, public cloud, MQTT or research-model acceptance.

| ID | Requirement | Evidence and status |
|---|---|---|
| T01 | 现有环境与构建 | Not accepted: actual end-to-end evidence is not yet complete; see test_report.md. |
| T02 | 原生 USB/Win10 | Not accepted: actual end-to-end evidence is not yet complete; see test_report.md. |
| T03 | 电脑完全断网 | Not accepted: actual end-to-end evidence is not yet complete; see test_report.md. |
| T04 | A 配网/重连 | Not accepted: actual end-to-end evidence is not yet complete; see test_report.md. |
| T05 | DHCP/新电脑 | Not accepted: actual end-to-end evidence is not yet complete; see test_report.md. |
| T06 | GitHub HTTPS | Not accepted: actual end-to-end evidence is not yet complete; see test_report.md. |
| T07 | UI 远程更新 | Not accepted: actual end-to-end evidence is not yet complete; see test_report.md. |
| T08 | UI 错误包及恢复 | Not accepted: actual end-to-end evidence is not yet complete; see test_report.md. |
| T09 | OTA 与回退 | Not accepted: actual end-to-end evidence is not yet complete; see test_report.md. |
| T10 | C 独立部署 | Not accepted: actual end-to-end evidence is not yet complete; see test_report.md. |
| T11 | 设备注册/身份 | Not accepted: actual end-to-end evidence is not yet complete; see test_report.md. |
| T12 | 多对多发现 | Partial: simulated 2A/2B; real HTTP services and UUID/ACL checks; no second physical A. |
| T13 | 配对授权 | Partial: pair-scope/revocation tests passed; real cloud pending. |
| T14 | 撤销隔离 | Not accepted: actual end-to-end evidence is not yet complete; see test_report.md. |
| T15 | ACL 与对象越权 | Not accepted: actual end-to-end evidence is not yet complete; see test_report.md. |
| T16 | B 本地离线推理 | Not accepted: actual end-to-end evidence is not yet complete; see test_report.md. |
| T17 | 环境自动/手动 | Not accepted: actual end-to-end evidence is not yet complete; see test_report.md. |
| T18 | 多环境隔离 | Not accepted: actual end-to-end evidence is not yet complete; see test_report.md. |
| T19 | 模型多权重 | Not accepted: actual end-to-end evidence is not yet complete; see test_report.md. |
| T20 | 动态目录 | Not accepted: actual end-to-end evidence is not yet complete; see test_report.md. |
| T21 | 非电池任务 | Not accepted: actual end-to-end evidence is not yet complete; see test_report.md. |
| T22 | B 便携迁移 | Not accepted: actual end-to-end evidence is not yet complete; see test_report.md. |
| T23 | 并行本地/远程 | Partial: worker isolation and queue/cancel unit tests; sustained load pending. |
| T24 | 单 A 多 B | Partial: one simulated A sends one file to two real local B services. |
| T25 | 单 B 多 A | Partial: 2A authorization isolation; simultaneous physical A requests pending. |
| T26 | 文件完整性 | Partial: part SHA, private file ownership, quota and bounded stream tests. |
| T27 | 文件续传 | Partial: offset/conflict/hash tests; physical Wi-Fi/browser reconnection pending. |
| T28 | 真实体全链路 | Not accepted: actual end-to-end evidence is not yet complete; see test_report.md. |
| T29 | 双端结果 | Not accepted: actual end-to-end evidence is not yet complete; see test_report.md. |
| T30 | B 重启/迁移 | Not accepted: actual end-to-end evidence is not yet complete; see test_report.md. |
| T31 | MQTT 重复/乱序 | Not accepted: actual end-to-end evidence is not yet complete; see test_report.md. |
| T32 | C 断线 | Not accepted: actual end-to-end evidence is not yet complete; see test_report.md. |
| T33 | A 换电脑/拔线 | Not accepted: actual end-to-end evidence is not yet complete; see test_report.md. |
| T34 | 取消和晚到结果 | Not accepted: actual end-to-end evidence is not yet complete; see test_report.md. |
| T35 | API/SDK | Partial: actual B/C HTTP SDK calls passed; A hardware API pending. |
| T36 | MCP/AI | Partial: MCP read-only/allowlist negative tests; real Agent call pending. |
| T37 | 真实模型 | Blocked: actual trained weights, preprocessing and reference outputs not provided. |
| T38 | 安全数据/秘密 | Partial: ignore rules and release/package scanning implemented; production audit pending. |
| T39 | 长时/资源 | Not accepted: long-duration physical memory/latency tests pending. |
| T40 | 备份与交付 | Partial: verified source backups and recovery scripts; actual restore pending. |
