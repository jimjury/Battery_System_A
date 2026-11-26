# 通用 AI 平台：PC A / ESP32 A / B / 独立 C

本目录是 PC 扩展工程，原 ESP32 工程保留在 `F:\ESP32_Projects\Battery_System_A`，没有烧录或改动板上固件。修订需求见 [V3](docs/SPEC_V3.md) 与 [PC 补充规范](docs/PC_A_V3_SUPPLEMENT.md)。

当前机器：双击 `Start_A.cmd` 打开 PC A（http://127.0.0.1:8766），`Start_B.cmd` 打开 B（http://127.0.0.1:8765）。已运行时只打开现有实例。管理员密码分别在 `desktop_a/.runtime/admin_password.txt` 与 `server_b/.runtime/admin_password.txt`，不公开、不写死。

C 已实际部署到 `https://60.204.129.7`，独立于 B 持续运行。A/B 使用 `client_config` 的公开地址与项目 CA 验证 HTTPS；GitHub 保存源码/版本和 CI，不承载 C 运行服务。设备凭据仅在各自 `.runtime`。当前 A/B 已用先前测试创建的普通身份登记及配对，不保存 C 管理员令牌。

使用流程：PC A 管理员解锁 → 模型/权重/部署目录选择就绪任务 → 生成输入表单 → 选择文件和参数 → 提交 → 任务列表查看 → 结果与附件导出。现有插件是明确标记 demo 的通用 CSV/文本计算；真实 SOC/SOH 权重仍需接入，不重新训练。

便携包：`python tools/package_pc.py` 生成 `dist/AB_PC_A_B_3.1.0.zip`。复制后使用已有 Python 3.10+，第一次分别登记 A/B 身份，按 B 配对码授权；不需要 ESP32。轻量包不含解释器、私有数据、身份或权重。

C 迁移/升级：见 [安装与恢复手册](docs/deploy_center_pc.md)，脚本提供包校验、SSH 上传、独立账户/systemd/TLS、自启、备份和失败回退；复用已有服务器 Python，不重装本机开发环境。

测试：`python -m unittest discover -s tests -v`。本地 PC A、实际浏览器、真实公网 C 链路、恢复和失败回退结果见 [本次报告](docs/validation_pc_20261126.md) 与 [40 项 PC 适用性](docs/acceptance_pc.md)。单元测试数量不等于 40 项完整验收，demo 不代表研究模型。
