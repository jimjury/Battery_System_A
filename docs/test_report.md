## Additional actual V3 validation

65 tests passed in 21.493 s after adding simultaneous simulated A1/A2 tasks on actual B1 HTTP worker; expected independent predictions [3,7] and [11,13], cross-owner reads denied.

Portable light ZIP copied to .local/portable-verification and run using the existing IDF Python 3.11 rather than Conda: real text inference returned 4 words with a new node UUID. This is a second interpreter and directory, not a second physical computer. Evidence: portable-second-python.log.

User explicitly declined first flash for now. No COM12 operation, partition activation, cloud provisioning or formal release activation performed.

# Latest verified V3 evidence

- Host regression: 65 tests passed in 22.171 s (host-v3-regression.log); legacy tests are counted separately from 40 acceptance cases.
- Existing Edge: actual B login, dynamic deployment catalog, file upload, worker inference (3 words), results view passed; 0 captured JS exceptions. Evidence: .local/browser-check/report.json and b-results.png.
- Firmware review build: 0x117640 bytes, 4 MiB slots, 73% free; no flashing.
- MQTT, public cloud, physical A OTA/power-loss, trained SOC/SOH weights, long-duration tests remain NOT accepted.

# V3 项目测试报告

唯一验收表为 `SPEC_V3.md` 的 T01–T40；下面旧 T01–T17 的历史编号不可用于 V3 通过统计。

本轮实际执行：

- V3 候选固件隔离构建通过：`firmware-v3-review.log`；应用 `0x117630` 字节，bootloader `0x5040` 字节。没有烧录。
- `tests/test_center_v3.py`：18 项通过，实际测试 SQLite 事务、一次性登记、2A×2B 配对、单对撤销、私有文件、重试 ACK、任务快照、fencing、恢复。输出 `center-v3-tests.log`。
- `tests/test_platform_v3.py`：9 项通过，12.091 秒；真实启动本机 C 和两个 B HTTP 服务，两个模拟 A 使用 SDK 上传及多目标分派，真实 worker 子进程执行两 variant；导出实际 CSV 与期望 [3,7] / [7,15] 一致，SHA-256 相符；A2 越权 A1 任务被拒绝。
- B 本地 HTTP 推理在其 C 客户端关闭后仍成功，文件统计返回实际 3 个单词；不表示电脑 WLAN 已断开。
- 新网页包本机检查及现有 Node 语法检查通过；版本化资源按 asset_set 选择同一活动/前版，避免跨版本 HTML/JS/CSS 混用。最新包大小随后回归时记录。
- V3 修改前项目备份已逐文件核验；源代码、配置、现有二进制保存在项目外。设备完整 Flash 仍未读取。

尚无实机证据：A Wi-Fi、GitHub 自主 UI v1→v2、固件 OTA/失败回退、真实云 C TLS/Broker、断网 PC→USB A→云 C→B→A、真实模型、第二块实体 A、长时硬件资源测试。上述不记为通过。

## 以下为已被 V3 取代的历史测试记录

# 旧项目测试报告

日期：2026-11-26。已通过的旧实现主机测试记录见 `VALIDATION.md`；其不代表新 GitHub 更新、Wi-Fi 或 A/B 实机链路通过。

本轮已执行：

- 再次构建当前基线，退出码 0；日志 `baseline-M0.log`（本地忽略）。
- 新完整项目备份，3,878 文件从 ZIP 重新读取并逐一验证 SHA-256。
- GitHub 连接器确认 `jimjury/Battery_System_A` main 可读且当前账户具有 push 权限；完整读取原始四份远端文件。
- 原生 `git fetch` 实际失败：`Recv failure: Connection was reset`。未强制覆盖文件或改动宿主网络，改用已授权 GitHub 连接器。

## 实体及最终验收状态

T01 基线构建通过；T02 旧固件 HTTP 曾实测通过（未重新烧录）；T03–T17 与 E01–E20 按总纲列为待执行。特别是 Wi-Fi、签名远程 UI 更新、OTA、断电回退、真实 USB→A→B→A→USB 数据链路均未验收，不能由本机模拟结果替代。

缺失条件：首次迁移确认、ESP32 Wi-Fi 配置、B 独立部署地址/权限、真实模型与数据。缺失条件不阻止继续开发可自测的软件。
