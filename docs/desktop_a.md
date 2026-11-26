# PC 系统 A

本目录为 `F:\ESP32_Projects\AI_Platform_PC`，原 `Battery_System_A` 的 ESP32 工程保持独立。

双击 `Start_A.cmd`，默认访问 `http://127.0.0.1:8766`；B 仍用 `Start_B.cmd`，默认 `http://127.0.0.1:8765`。两个程序复用现有 Python 3.10+，不安装依赖。Linux/macOS 可分别使用 `Start_A.sh` / `Start_B.sh`，这些平台尚待实际迁移验收。

PC A 管理员密码首次生成于 `desktop_a/.runtime/admin_password.txt`；B 位于 `server_b/.runtime/admin_password.txt`。身份、C 凭据、API key 哈希均在 `.runtime`，不可提交公共仓库或放入公共便携包。

A 页面“通信中心”配置 HTTPS 地址、别名和可选 CA PEM 的绝对路径，然后用 C 管理员签发的 A 登记码登记。B 使用相同 C 地址与 CA，独立登记 B 并接单；A 使用 B 签发的配对码授权。CA 仅用于这一连接，程序不修改 Windows 全局证书存储，也不关闭 TLS 检查。

PC A 的本地 API 与 SDK target=`A` 保持同一语义；浏览器使用 HttpOnly 会话及 CSRF，程序访问可创建受 scope 限制的 API key。A 只代理声明过的 C 业务 API，不能传任意 URL、安装模型或取得其他身份数据。附件以 16 KiB 流式传输并校验 SHA-256。

C 离线时 PC A 本地页面继续可访问并明确报告离线，已上传任务由 C/B 保管；本版不伪造离线远程结果。ESP32 网络设置/固件更新按钮不出现在 PC A 界面。
