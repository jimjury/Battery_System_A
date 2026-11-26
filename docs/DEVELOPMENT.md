# 系统 A 开发与部署

## 当前阶段

网页独立更新代码及自动化脚本已就绪，首次迁移待批准；设备上的旧固件尚不具备这些 API。先按 `MIGRATION.md` 完成首次迁移和实机验证，再进行电池业务界面开发。`web/` 当前仅保留连接验证页面，没有模拟电池数据。

## 日常操作

- 修改网页：双击项目根目录 `deploy-web.cmd`，保持窗口运行，编辑 `web/index.html`、`web/style.css`、`web/app.js`。保存后脚本等待 0.8 秒稳定期，检查并上传；验证完成后已打开的设备页面在约 2 秒内自动刷新。停止监听按 Ctrl+C。
- 检查网页包：`F:\Anaconda\python.exe tools\deploy_web.py check`，不访问设备。
- 单次网页部署：`F:\Anaconda\python.exe tools\deploy_web.py push --open`。
- 查询版本：`F:\Anaconda\python.exe tools\deploy_web.py status`。
- 手动回退网页：`F:\Anaconda\python.exe tools\deploy_web.py rollback`。
- 只编译固件：双击 `build-only.cmd`。
- 日常串口编译烧录：首次迁移完成后双击 `build-flash.cmd`。脚本拒绝未经批准的分区布局。
- 网络固件 OTA：`F:\Anaconda\python.exe tools\firmware.py ota`。仅上传应用 BIN，不改 bootloader、分区表或网页。自动重启、核验正在运行的应用完整 SHA-256、检查 HTTP 后确认固件。
- 本机测试：`F:\Anaconda\python.exe -m unittest discover -s tests -v`。

脚本不安装或升级工具。网页部署仅用 Anaconda 的 Python 标准库。固件构建复用 `F:\Espressif\tools\python_env\idf5.5_py3.11_env` 中已存在的 IDF Python，以及已安装的 CMake、Ninja、Xtensa 工具链。

## 更新过程与边界

网页分区使用只读资源包，不需要额外文件系统依赖或镜像生成工具。包内含固定格式目录、每个文件的 SHA-256、整体内容 SHA-256。以整个包的 SHA-256 作为不可变版本号。每个包最多 32 个文件、总大小最多 1 MiB，含 `/index.html`。路径只允许英文字母、数字、`/._-`，不接受上级目录、API 路径、符号链接、密钥或 `.env`。

上传前检查 UTF-8 文本、JSON 语法、HTML 本地资源引用和包大小；拒绝依赖外部 CDN。若系统已有 Node，额外执行 JavaScript 语法检查；缺少 Node 时不会安装，`check` 会说明浏览器验证仍待完成。

设备只擦写非活动网页槽；上传中断或校验失败保留当前活动槽。切换后处于待确认状态，电脑必须逐个下载所有资源并比较 SHA-256，才提交确认。核验失败时脚本请求回退；如果电脑失联，120 秒未确认则设备自动回退，待确认期间设备重启也回退。首次上传失败回到固件内置恢复页。确认后的网页保留上一版，可手动回退；本地 `.deploy/<SHA256>/` 保留每版包、清单和成功回执。

浏览器没有更新密钥，不具有更新权限，只轮询状态并在已提交版本变化后刷新。浏览器缓存关闭，资源带 SHA-256 ETag。校验只证明资源字节完整及本地约束通过；业务行为、JavaScript 实际执行与视觉效果仍须实机浏览器验证。

固件通过 ESP-IDF `esp_ota_*` 写入非活动应用槽，通过内容 SHA-256 与 ESP 镜像检查才选择下一启动槽。新固件启动后须在 120 秒内由部署端确认，否则回退并重启；启动崩溃重启由 bootloader 回退。Wi-Fi 接口未来启用后可用同一服务，通过 `--url http://<设备Wi-Fi地址>` 指定入口；当前未配置 Wi-Fi 凭据，尚未验证 Wi-Fi 更新。

网页资源与固件 OTA 槽是各自独立的，后续更改 API 时应保持旧网页兼容。协议升级、bootloader 或分区表更新应走单独审查的串口迁移。

## 认证及本地配置

首次 `tools/firmware.py setup` 生成随机 256 位密钥，写入 `.local/update_key.txt`，生成编译用的 `.local/update_secret.h`。不要把该目录上传到仓库或放入网页。生成后请保留，丢失密钥需要经串口更换固件中的密钥。

更新使用 HMAC-SHA256，签名绑定设备随机挑战、请求方法、路径和上传内容摘要。挑战 60 秒有效且一次性使用；网络中不传输密钥，上传内容仍需与签名中的 SHA-256 一致才激活。HTTP 不提供数据加密，当前默认使用本地 USB 链路。请求不经过系统代理，不允许重定向，不配置浏览器跨域写入权限。

本地 Git 忽略 `.local/`、`.deploy/`、`sdkconfig`、`sdkconfig.old`、构建输出、受管组件、`.env`、PEM/KEY。可复现的非敏感参数通过 `sdkconfig.defaults` 管理；将来增加 Wi-Fi 配置时，凭据保留在被忽略的本地配置或 NVS 中，禁止放入网页、跟踪文件、命令行日志或默认配置。

## 网络架构

USB NCM 固定为设备 `192.168.7.1/24`、电脑 `192.168.7.2/24`；原 USB 收发和 TinyUSB 初始化逻辑保留。USB 链路不配置默认网关或 DNS，不作为电脑上网路径。未来 Wi-Fi 仅为 ESP32 自身联网。

禁止 Windows Internet Connection Sharing、网桥、ESP32 IP 转发及 NAPT。迁移配置明确禁用 ESP32 IP 转发与 NAPT。脚本不添加路由、不启用热点、不修改电脑适配器、不启动 Internet 共享。电脑继续使用自己的网络连接，ESP32 未来使用自己的 Wi-Fi 连接。

## Git 与备份

原始备份目录位于项目外；本地 Git 原始基线提交与后续更新提交分别保留已验证 NCM 源码和新实现。不要推送包含配置或 Flash 镜像的完整备份。固件 BIN 和完整 Flash 镜像含编译进去的更新密钥，也应仅在本机保管。
