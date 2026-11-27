# 手机访问软件验证报告 · 2026-11-27

## 已执行

| 项目 | 实际结果 | 边界 |
|---|---|---|
| 修改前备份 | 110 个 Git 跟踪源文件逐一核对，ZIP SHA-256 `1191b949d4ee4257328a4da340416d61d78691d860ed60dcddc1e3580d39ccdf` | `Battery_System_A_backups/pre_mobile_20261127_143650/source.zip`，不是 Flash 备份 |
| 完整现有主机回归 | 73 tests，25.738 秒，OK | 不执行手机/ESP32 物理测试 |
| 网页检查 | 5 文件，56,355 字节，Node 语法检查及资源打包通过 | 包 SHA-256 `1a8070f4ae7dda18f08e426d282e53d917340ae87f1182ce3c27f9378511cdb6` |
| Edge 手机界面 | 390×844，管理导航、取消、配对撤销隐藏，导航限制生效，无横向溢出、无 JS 异常，手机测试请求均为 GET | `tools/mobile_browser_check.py` 使用明确标记的拦截响应 fixture，仅验证界面，不能证明固件权限或真实数据链路 |
| USB 界面回归 | 热点管理事件及升级导航仍可用 | 同上界面 fixture |
| 实际 B HTTP + Edge | 登录、模型目录、上传真实文本、真实子进程推理、结果 3 词；丢失已持久化提交响应后重试仍只有一个任务；无 JS 异常 | task `27800425-8be0-507e-8646-b2483a7c3066`，明确为演示文本统计模型 |
| ESP-IDF v5.5.6 最终候选编译 | `build-review/tusb_ncm.bin`，大小 `0x11f960`，4 MiB 应用分区余量 72% | 只构建隔离 review 配置，未写分区表或固件到设备 |
| NCM 保留检查 | `main/tusb_ncm_main.c` 未修改 | 设备仍运行原 USB 网页版本 |
| 网络隔离静态检查 | review 配置 IP_FORWARD 关闭；未加入 NAT/NAPT/桥接，DHCP 明确关闭路由和 DNS 提供 | 仍须实机抓包与路由测试 |

原始输出位于本机 `.local/mobile-tests.log`、`.local/mobile-build-final.log`、`.local/mobile-browser/report.json`、`.local/mobile-browser/phone-fixture.png`、`.local/mobile-b-regression.log`；这些本地测试文件不含部署凭据，不作为真实手机验收证据。

## 发现与修复

1. 手机分支最初会进入 B 环境事件绑定，改为明确的 B 条件，Edge 无异常。
2. 限制手机 GET scope，防止轮询 USB 管理操作的异步响应；C 配置读取保持 USB 管理员专用。
3. DHCP 初始化容许已经停止状态；启用热点前在无线电关闭时配置 WPA2，避免开放热点窗口。
4. Wi-Fi 变更及 NVS 写入失败均尝试恢复；变更时清除手机会话。
5. 浏览器检查脚本初次运行因测试表达式顶层 await 使用错误失败；改为 async 函数后通过。该失败属于测试工具问题，已记录。

## 待真实验证（未通过、不冒充完成）

首次烧录/分区迁移尚未批准，未操作 COM12，未读取 Flash。批准后先双读 16 MiB 备份并核对，再执行手机关联、DHCP 无网关/DNS、USB 同时访问、登录过期/限速/跨接口会话隔离、伪造 Host/Origin、未登录读取、手机写入/OTA/API-key/管理 job 拒绝、结果 SHA-256 导出、AP/STA 信道切换、传输中拒绝变更、密码轮换、掉电和 NVS/无线故障恢复、无互联网共享抓包测试。需要手机配合的环节届时说明。
