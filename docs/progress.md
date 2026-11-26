# Current checkpoint: 2026-11-26

See [validation_20261126.md](validation_20261126.md) for current evidence: 73 host tests passed, actual Edge lost-reply retry is idempotent, 1,034 real local B tasks measured over 180 s, and isolated firmware app 0x11e350 bytes compiled. MQTT A/B/C code is implemented but real broker/cloud/hardware remain unverified.

User explicitly declined first flash; software work continues. GitHub source checkpoint 0a37c2a preserved the original bootstrap/update entry. The older progress entries below are retained as history and may contain superseded counts.

# Latest verified V3 evidence

- Host regression: 65 tests passed in 22.171 s (host-v3-regression.log); legacy tests are counted separately from 40 acceptance cases.
- Existing Edge: actual B login, dynamic deployment catalog, file upload, worker inference (3 words), results view passed; 0 captured JS exceptions. Evidence: .local/browser-check/report.json and b-results.png.
- Firmware review build: 0x117640 bytes, 4 MiB slots, 73% free; no flashing.
- MQTT, public cloud, physical A OTA/power-loss, trained SOC/SOH weights, long-duration tests remain NOT accepted.

# V3 M0–M8 当前执行进度

唯一有效需求：`SPEC_V3.md`，原文件完整读取 799 行；SHA-256 `88b1931337a73f12f95082516df6777e06708f76edf1b9c69352d0283daed771`。此前一对一电池架构已退役，不再作为平台内核。

| V3 阶段 | 当前实际状态 | 证据与待办 |
|---|---|---|
| M0 | 本机备份与构建通过；GitHub 源码同步进行中 | pre_V3_20261126_204626 备份 204 文件；ZIP SHA-256 `83395a5d51a0dfbe6c07ca66457dce1ff00dbf67235b648baf3e0080c6819ee7`；原 NCM 回调保留 |
| M1 | 配网/重连/TLS 代码已编译，未上板 | 当前板仍运行原 USB 测试网页；电脑断网及 ESP32 独立 Wi-Fi 待实测 |
| M2 | 双 SPIFFS、签名 UI、OTA、版本资源隔离和发布工具已实现，未实机验收 | 隔离 build-review 编译通过；4 MiB app 槽；没有执行分区迁移、串口读写、正式发布或清单激活 |
| M3 | C 核心软件测试通过 | 18 项 2A×2B 授权/文件/任务测试；9 项 B/HTTP/worker 联调测试通过。C 为本机独立进程测试，尚未部署到云；MQTT 通知接入继续开发 |
| M4 | 通用便携 B、双模式、环境探测和独立进程执行通过软件测试 | 表格两 variant 实际输出 [3,7] / [7,15]；通用文本统计真实执行；C 客户端关闭后仍可本地推理。启动器、迁移包及 GUI 正在补充回归 |
| M5 | A 通用 C 客户端已编译，A/B 动态 UI 和多目标协议已实现，继续验证 | 唯一数据链路为浏览器→A→独立 C→B；旧固定 V/I/T 与 B 地址模块移入 examples/legacy_battery，不进入新固件或网页包 |
| M6 | 外部条件阻塞；不记为通过 | 需要首次基础固件迁移确认、A 自身 Wi-Fi、独立云 C 的主机/域名/权限，以及实际 B 部署条件 |
| M7 | 等待用户提供模型；不训练 | SOC/SOH 原项目、选定权重、真实预处理及离线基准尚未提供；平台不猜特征、窗口或权重 |
| M8 | SDK 已用于真实本机 HTTP；MCP、文档、恢复、CI 和长时测试继续开发 | MCP stdio 保留同一 API 权限，不提供 shell/任意文件读取；40 项按 V3 单独登记，模拟 A 不当作实体板 |

最新实际构建：`firmware-v3-review.log`，应用 `0x117630` 字节，4 MiB 槽剩余约 73%；这是未烧录的候选产物。当前 Git 已提交基线仍为 `8f2cc95`，本轮改动正在整理成小步提交。没有重新安装 Python、Conda、IDF 或改动宿主网络。

## 以下为已被 V3 取代的历史记录，不再指导开发或验收

# M0–M7 历史执行进度

唯一源码目录：`F:\ESP32_Projects\Battery_System_A`。完整需求总纲已逐行阅读（794 行）；以其作为执行和验收依据。

| 阶段 | 当前状态 | 实际证据及剩余条件 |
|---|---|---|
| M0 | 进行中 | 原始 NCM 基线标签 `usb-ncm-verified-baseline`；当前基线再次编译通过；新完整备份 3,878 文件逐项核验。GitHub 已授权，原生 Git HTTPS 被连接重置，正通过连接器保留远端源码与历史 |
| M1 | 未完成 | 准备独立 Wi-Fi、USB 管理认证、校时与真实 HTTPS 检查；设备仍为旧 USB 网页固件 |
| M2 | 未完成 | USB 网页原型已做软件测试；按总纲升级为 SPIFFS + 公钥签名 + GitHub 自主更新，未迁移/烧录 |
| M3 | 未完成 | 完整离线工作台与数据协议待实现、测试 |
| M4 | 未完成 | B 原生服务、演示模型、持久化、独立界面待实现、测试 |
| M5 | 需要外部条件 | 首次基础固件迁移批准；可由 ESP32 独立 Wi-Fi 访问的 B 地址与部署授权 |
| M6 | 需要用户提供条件 | 真实模型结构、权重、元信息、训练预处理、数据与离线基准 |
| M7 | 未完成 | 完成前置阶段后进行长时实机测试及最终交付 |

新备份：`F:\ESP32_Projects\Battery_System_A_backups\before_M0_M7_20261126_201338`，ZIP SHA-256 `673521b1e5850696e49c884cfab789d84fdabaf97adc8fab18ee0094dcc1c534`。设备 Flash 尚未读取；首次迁移需先双读备份并核验。

禁止将软件模拟、演示模型、静态页面或未部署代码记为实机验收通过。测试证据见 `test_report.md`；每次恢复工作先读取本文件、测试报告与 Git 状态。
