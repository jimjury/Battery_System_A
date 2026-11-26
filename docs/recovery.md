# 恢复与首次迁移

当前板仍运行原有 USB NCM 网页。新固件只在 `build-review` 编译，尚未修改活动分区表或刷写。首次迁移必须由用户确认方案及备份完整后运行 `python tools/firmware.py migrate --approved --port COM12`。脚本先独立读取整片 16 MiB 两次并比对 SHA-256，成功后才修改分区、编译和烧录；不执行 erase_flash，不清空 NVS。

分区方案：NVS 0x9000/0x6000，PHY 0xf000/0x1000，OTA0 0x10000/4 MiB，OTA1 0x410000/4 MiB，otadata 0x810000/0x2000，web_a 0x820000/1 MiB，web_b 0x920000/1 MiB。原 NCM 已验证函数保留。迁移会重启板并改变固件/Flash 内容，当前没有得到首次写入确认。

迁移后 `/recovery` 是固件内嵌救援页，不受网页包损坏影响。网页写入非活动 SPIFFS，校验发布者签名、整包与文件 SHA-256 后原子切换；USB 更新需要回读资源确认，超时/重启未确认回退。固件写入非活动 OTA 槽，启动健康检查后确认，否则由 IDF rollback 回退。以上机制目前编译和软件验证通过，断电等实机验收仍待首次迁移。

日常页面：`python tools/deploy_web.py watch`，配置保存在本机忽略目录；检查 JS/资源依赖后签名、上传、下载核验、确认并刷新。日常固件：`python tools/firmware.py ota`，仅已迁移设备允许。公共 GitHub 发布另走 `tools/publish.py`，正式入口需要匹配实际产物的硬件验收记录；普通源码提交不自动发布设备更新。

B/C 备份含私有任务，不能加入轻量包。恢复时先停止进程，使用一致性备份恢复 DB 和文件，核查哈希；迁移 B 同身份前停止旧实例。恢复原始 A 需要已校验整片 Flash 备份及明确串口写入授权，禁止把候选固件误当原厂恢复镜像。
