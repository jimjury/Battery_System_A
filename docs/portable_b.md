# 便携式系统 B

双击根目录 `Start_B.cmd`，或 Linux/macOS 执行 `sh Start_B.sh`。启动器选择已经存在的 Python 3.10+，不安装或升级依赖；网页默认只监听回环地址。管理员密码首次在 B 的 `.runtime/admin_password.txt` 生成，不进入便携包。选择器可通过 `AB_PYTHON` 或私有 `.local/launcher.json` 显式指定解释器。

“运行环境”页会启动候选解释器探测实际 Python、包、PyTorch/CUDA 与 GPU；手动路径同样实际探测。没有 CUDA 时显式选择 CUDA 会拒绝，自动模式只选择模型声明支持且实际可用的设备。每个任务使用独立子进程，固定解释器、代码、权重和预处理哈希；环境不满足时不自动安装、不换成 demo。

本地目录、上传、执行、取消和导出不依赖 C。远程模式另行配置独立 C HTTPS、登记 B 身份并开启接单；每一 A-B 关系单独授权。远程完成先归档为 `local_completed`，等 C 确认实际附件和哈希后记录同步完成，网络故障时保留 outbox。

运行 `python tools/package_b.py` 生成 `dist/AB_B_light_v3.zip`。轻量包不含解释器、设备身份、令牌、私有模型或数据。复制后形成新的 UUID；迁移同一身份需停旧实例，私有复制 `.runtime`，重新探测/绑定解释器并等待 C 旧租约过期。不要同时运行复制的身份；C 会拒绝重复实例。

目前在 Windows 10 上验证；其他操作系统和真正另一台电脑尚未验收。没有把整个 Conda/PyTorch 环境打进包，也没有声称该轻量包在任意电脑无需依赖即可运行。
