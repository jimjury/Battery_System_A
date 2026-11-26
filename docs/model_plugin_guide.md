# 模型插件契约

参考 `server_b/models/demo_table/model.json` 与 `demo_file/model.json`。模型包由本地管理员明确选择可信目录导入；来自 A/C 的输入文件永远不能成为执行代码。模型适配器不是安全沙箱，只运行审核过的代码。

一个模型架构版本包含多个 variant，每个 variant 声明自己的资源文件、权重版本及预处理；deployment 引用一个确定的 variant。对外只发布契约、版本和哈希，不发布权重路径或二进制。输入由 `inputs` 声明，可为文件、文件列表、文本或 JSON；参数用受支持 JSON Schema 描述，不存在平台固定 V/I/T 或 SOC 输入字段。

适配器模块提供 `create_adapter()`，实例实现 `self_test(variant)`、`validate(contract, inputs, parameters)`、`load(contract, variant, device)`、`preprocess(inputs, parameters)`、`predict(prepared, progress, cancelled, output)` 和 `postprocess(predictions)`，实际格式以两个随附插件为准。导入后通过 `POST /api/v1/deployments/{id}/validate` 指定 `deployment_version`、可选 `interpreter` 与 `device` 执行实际自检；只有当前代码/资源/环境快照验证通过才能就绪。更改模型代码或权重需要新版本和重新验证。不要在自检中只返回固定成功；研究模型应使用真实离线基准和明确容差。

产物保存在 worker 提供的输出目录，返回真实附件元数据、表格预览或指标。平台重新计算文件哈希并限制输出大小。不能返回工作区外路径。模型声明的任务资源与测试不是训练入口，已训练 SOC/SOH 直接加载用户选定权重推理。

随附 `demo_table` 是对任意用户指定 CSV 数值列求和并应用两组确定性参数；`demo_file` 对实际上传文本进行字节/词/行统计。二者仅验证平台，明确标记 demo，不能代表 SOC/SOH 研究结果。真实模型代码、选定权重、预处理、单位、样本和容差尚待接入。
