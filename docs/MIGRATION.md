# 系统 A：首次分区迁移方案（待用户确认）

目前设备继续运行原固件。本次仅完成源码、脚本和本机检查；没有执行串口读写、分区迁移或新固件烧录。网页资源独立更新的实机验收须在用户批准首次迁移后进行。完整业务界面尚未开始。

## 已核验的原始备份

- 目录：`F:\ESP32_Projects\Battery_System_A_backups\20261126_195122`
- `project.zip`：原项目全部 3,779 个文件，包括 main、main_backup、sdkconfig、受管组件、原 bootloader、原分区表、原应用及构建产物。
- 每个文件均重新从 ZIP 读取，SHA-256 与 `manifest.json` 一致。
- ZIP SHA-256：`975f796087bcc20d7210563de2a054e6daee1d99acd8cafe5dafaa4da8312249`。
- `verification.json` 记录检查结果。备份含本地配置，应留在本机，不加入 Git。
- 尚未读取设备的完整 Flash；串口读取会重启设备。批准后，迁移脚本首先连续读取两次完整 16 MiB Flash，检查长度及两次 SHA-256 完全一致，保存原始镜像和校验报告。检查失败便停止，不改分区、不烧录。

## 拟采用的 16 MiB 布局

当前为 ESP-IDF 默认单应用分区，新布局如下。保留 NVS 与 phy_init 的原位置，第一应用仍从 `0x10000` 开始。

| 分区 | 类型 / 子类型 | 起始地址 | 大小 | 用途 |
| --- | --- | --- | --- | --- |
| nvs | data / nvs | 0x009000 | 0x006000 | 原配置、更新元数据 |
| phy_init | data / phy | 0x00F000 | 0x001000 | PHY 初始化 |
| ota_0 | app / ota_0 | 0x010000 | 0x400000 | 固件 A，4 MiB |
| ota_1 | app / ota_1 | 0x410000 | 0x400000 | 固件 B，4 MiB |
| otadata | data / ota | 0x810000 | 0x002000 | OTA 选择及启动状态 |
| web_a | data / 0x40 | 0x820000 | 0x100000 | 网页 A，1 MiB |
| web_b | data / 0x40 | 0x920000 | 0x100000 | 网页 B，1 MiB |
| 未分配 | — | 0xA20000 | 0x5E0000 | 后续扩展 |

`partitions.csv` 尚未生成或启用；方案存于 `tools/firmware.py`。只有显式运行 `migrate --approved` 且完整 Flash 备份通过后，才写入分区表并启用自定义布局及 bootloader OTA 回退。

## 批准后执行的步骤

1. 停止网页监听脚本，确认 COM12 无串口监视器占用。
2. 两次读取 16 MiB Flash；核验并保存到项目外的备份目录。
3. 启用以上布局、16 MiB Flash、OTA 回退；禁用 ESP32 IP 转发和 NAPT。
4. 使用现有 IDF v5.5.6 和现有 IDF Python 环境重新编译。
5. 通过 COM12 烧录 bootloader、分区表、OTA 初始数据和应用；不执行整片擦除，不覆盖原 NVS。
6. 等待 USB NCM 恢复，检查 `/` 与 `/api/status`。
7. 上传首个网页包，逐资源下载并对比 SHA-256，再确认提交。
8. 实机验证修改网页自动部署与浏览器刷新，再验证错误认证、损坏上传、回退、重启保持版本和 OTA；全部通过后开展完整业务界面。

批准后由开发代理执行：

```bat
F:\Anaconda\python.exe tools\firmware.py migrate --approved --port COM12
```

首次迁移会中断当前 USB 网页连接并重启设备。分区表与 bootloader 的串口迁移不受应用 OTA 双槽保护；因此迁移前完整 Flash 备份是强制步骤。首次迁移没有可自动启动的旧 OTA 应用；后续 OTA 才有上一固件槽可回退。

## 恢复原固件

若首次迁移失败，在 COM12 可用时，优先使用双读核验过的 `flash.bin` 恢复原始 16 MiB 镜像：使用现有 IDF Python 的 `esptool`，参数为 `--chip esp32s3 --port COM12 write_flash 0 <flash.bin绝对路径>`。执行前先检查镜像 SHA-256 与同目录 `verification.json` 相符。

完整镜像恢复会覆盖整个 Flash，包括更新后的配置；只在明确需要回到备份时执行。原项目 ZIP 还保留原有可烧录产物，设备无法自动进入下载模式时需要按开发板 BOOT/RESET 操作。不要对尚未核验的镜像执行恢复。
