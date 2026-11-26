# 通用 AI 平台 A / B / C — V3

唯一有效需求：[`docs/SPEC_V3.md`](docs/SPEC_V3.md)。原始 ESP32 USB NCM 基线保留。当前设备仍运行原始网页，新固件只编译验证，没有进行首次分区迁移或烧录。

* A：ESP32-S3 N16R8，USB 本地网页、独立 Wi-Fi、签名双 SPIFFS 网页与 OTA 双槽、异步 C 客户端。页面源在 `web/`，嵌入式救援页在 `main/recovery.html`。
* B：`Start_B.cmd` / `Start_B.sh` 启动便携式本地界面，复用现有 Python，模型在独立解释器进程执行。本地推理不依赖 C，远程模式主动连接 C。
* C：`python -m control_center.main --help`，独立服务，处理身份、逐对授权、目录、私有文件和 task/attempt 状态。当前本地 HTTP 拓扑验证通过，未部署真实云；MQTT TLS 通知、事务 outbox 和 broker 逐设备配置代码已加入，真实 broker 尚未验收。
* 扩展：`sdk/` 提供 A/B/C 显式目标 SDK 与受 scope 限制的 MCP stdio，`server_b/models/` 是明确标记 demo 的通用插件，无 SOC/SOH 伪推理。

测试：`python -m unittest discover -s tests -v`。网页：`python tools/deploy_web.py check`。隔离编译：`python tools/firmware.py build`。便携包：`python tools/package_b.py`。主机测试需要已存在的 `cryptography`，浏览器独立验证使用现有 Edge 和 websocket-client。不会安装本机工具。

查看 [进度](docs/progress.md)、[测试记录](docs/test_report.md)、[40 项验收](docs/acceptance.md)、[恢复/首次迁移](docs/recovery.md)、[B 迁移](docs/portable_b.md)、[插件契约](docs/model_plugin_guide.md)、[云部署边界](docs/deploy_center.md)、[安全](docs/security.md)。历史固定电池应用保存在 `examples/legacy_battery`，不是 V3 运行入口。

GitHub 源码提交与设备正式更新相互独立。发布者签名、产物回读和实际硬件验收通过后才允许激活更新清单；普通 push 不会向设备发布未经实测的固件。
