# 同济课程逐字稿工具

Windows MVP。已实现本地离线转写、持久队列、Chrome/Edge 课程采集扩展，以及带时间戳的 Markdown、SRT 和原始 ASR JSON 导出。AGY 完成初版及修复，主代理审查、补修并做真实课堂短样本验收。

**当前状态：本地应用与 Edge 扩展已可用。** 已验证浏览器扩展可通过 Native Messaging 提交本地短样本并完成转写，课程时间戳跳转也已验证。整节在线课程仍未实测。

## 当前机器快速使用

项目已有 `.venv`、FFmpeg/ffprobe 和本地模型 `models/faster-whisper-small`。在本目录运行：

```powershell
# 转写自己选定的本地音视频，首次提交会自动启动后台 worker
.venv\Scripts\tongji-clipper.exe submit "D:\课程录音.wav" --model "D:\tongji-cliper\models\faster-whisper-small" --offline --title "课程名称" --lecturer "教师姓名" --watch

# 查询、取消、停止后台进程
.venv\Scripts\tongji-clipper.exe status
.venv\Scripts\tongji-clipper.exe cancel <job_id>
.venv\Scripts\tongji-clipper.exe pause <job_id>
.venv\Scripts\tongji-clipper.exe resume <job_id>
.venv\Scripts\tongji-clipper.exe retry <job_id>
.venv\Scripts\tongji-clipper.exe worker stop
```

默认导出到项目 `output/`；`--output-dir` 可指向 Obsidian Vault 的子目录。`--keep-audio` 保留任务的 16kHz WAV。`--time-offset 120` 表示输入片段从课程第120秒开始，**仅偏移导出时间轴，不截掉输入前120秒**。

`submit --resource-profile low|balanced|fast` 选择低占用、均衡或快速档位，默认均衡。档位控制 CPU 线程预算；低占用档在 Windows 上降低进程优先级。它不是 CPU 使用率的硬百分比限制，也不改变所选识别模型。

## 任务中心与日常使用

- 扩展弹窗用于确认当前课程并提交；点击“任务中心”打开常驻管理页面，查看完整任务列表、搜索和状态筛选。
- 在任务中心设置模型、离线模式、资源档位、输出目录与保留音频策略。本地模型路径与预设模型互斥；配置自检会检查运行依赖、模型和目录可写性。
- 已验证音频保存完成后，界面才提示可关闭课程页。关闭弹窗、切换软件不会停止独立 worker；电脑睡眠或关机不能继续计算。
- “正在取消／正在暂停”表示请求已收到、执行尚在退出。底层模型加载和单次推理不能保证瞬时中断，界面确认终态后才代表停止。暂停保留完整分块断点；未完成块在恢复时重新处理。
- 失败或取消后可直接重试，已验证缓存可复用；媒体地址失效时打开课程页刷新，再从弹窗重新提交。
- 完成后可预览逐字稿、打开笔记或目录，也可重新导出到其他目录。重新导出复用识别结果，不再进行 ASR；已有文件保留并生成 revision。
- “建议复核”是识别质量提示，不是准确率。点击逐字稿时间戳跳转课程页，需要扩展正常加载及课程访问权限。
- 缓存清理只针对工具管理的非活动任务音频缓存。原始输入、笔记和断点保留；清理后再次执行可能需要重新获取媒体。
- 浏览器运行时，扩展对完成、失败和需刷新地址的状态变化发送去重通知。浏览器关闭期间不会发送浏览器通知，重新打开任务中心可查看历史。

更新后请在浏览器扩展管理页重新加载扩展，并在任务停止或暂停后重启 worker，使前后端使用同一版本。队列和已完成分块保留。独立演示入口 `extension/preview.html` 使用合成数据，不连接真实 worker。

worker 在转写当前任务的同时最多预取下一个任务的音频，后续任务等待预取退出后才接管。预取检查 512 MiB 缓存预算和 1 GiB 剩余空间底线；阈值触发时停止预取，任务仍保留在队列。此预算只限制提前获取，正常执行整课仍需足够磁盘空间。

## 安装浏览器扩展

以下步骤适用于首次安装或更换浏览器。

1. 在 Edge 手动打开 `edge://extensions/`，或 Chrome 打开 `chrome://extensions/`。
2. 启用开发人员模式，加载解压缩的扩展，选择项目中的 `extension` 目录。
3. 复制扩展的32位 ID，运行下面的宿主注册命令。也可以将 ID 回复给 Codex，由它继续注册和验收。

```powershell
# 先预览；不修改文件或注册表
powershell -ExecutionPolicy Bypass -File scripts\register-host.ps1 -ExtensionId <32位扩展ID> -DryRun

# 注册当前用户的 Chrome/Edge Native Messaging 宿主
powershell -ExecutionPolicy Bypass -File scripts\register-host.ps1 -ExtensionId <32位扩展ID>
```

宿主清单只允许指定扩展 ID；遇到其他安装的同名宿主会拒绝覆盖。卸载时运行 `scripts/unregister-host.ps1`，仅删除指向本项目的注册项。

4. 在该浏览器登录同济平台，刷新课程回放页。扩展无法继承 Codex 内置浏览器的登录状态。
5. 打开扩展面板，确认课程及媒体流。首次打开时，扩展会为未设置的选项填入本机模型路径、离线模式和默认输出目录；已有用户设置会保留。请按实际安装位置设置模型和输出目录。
6. 提交任务。**进入“转写”阶段意味着音频已完整落盘，这时可以关闭课程页。** 尚在排队或获取音频时，签名失效可能需要刷新课程页后重新提交。面板显示错误、阶段及输出路径，可手动刷新状态和取消。

课程页与媒体地址不同：

```text
稳定来源： https://look.tongji.edu.cn/livingroom?course_id=...&sub_id=...&tenant_code=222
临时媒体： https://look.tongji.edu.cn/play/...（带签名，仅在本机队列使用）
```

扩展从正常页面响应读取必要元数据，或从 `<video>` 获取当前地址，不导出 Cookie/Authorization。Markdown 保存稳定来源和 `#clip-t=123` 时间戳。跳转由本工具的内容脚本实现，需扩展在目标浏览器正常加载。

## 换机器安装

安装 Python 3.11+、uv、FFmpeg/ffprobe 后：

```powershell
uv sync --extra asr --locked
ffmpeg -version
ffprobe -version
```

`uv.lock` 固定公开 Python 依赖。首次使用模型名称（如 `small`）时会从 Hugging Face 下载公开模型；下载后用本地路径及 `--offline` 避免模型联网查找。课程媒体获取需要同济网站网络，识别在本机完成。

```powershell
.venv\Scripts\tongji-clipper.exe verify-offline --model "D:\tongji-cliper\models\faster-whisper-small"
```

## 数据与恢复

- worker 默认使用项目 `.runtime/`：SQLite任务队列、随机本地通信 token、日志、模型缓存。可用 `TONGJI_CLIPPER_HOME` 指定另一目录。
- 原生桥只转发操作，worker 独立运行且只绑定 `127.0.0.1`。长任务不依赖扩展后台持续存活。
- 任务以课程/课次/流及影响识别结果的配置去重，不以签名 URL 去重。排队中重提交会更新签名；失败/取消后可重试。更改导出目录请使用任务中心的重新导出功能，无需重复识别。
- 已完整获取的音频可用于恢复；未完成的获取会重试。Checkpoint 记录完成块、音频指纹和原始分段；不把最后一句的结束时间当作完成边界。
- 每个任务独立目录，已有产物生成 revision，避免覆盖用户编辑。输出包括 `.md`、`.srt`、`.segments.json`、`.raw.json`；checkpoint 保留原始块和置信度。
- 成功写入并核验后清理本任务临时音频，保留原始输入。失败时保留缓存以便重试。临时媒体签名不进入笔记和公开状态。
- 没有 LLM 总结或润色。跨块去重只比较时间重叠的同句；保留原始 ASR 块供复核，无法承诺消除所有边界识别差异。

## 实际验收与限制

任务窗口可见时每 2 秒自动刷新，隐藏时停止高频刷新。转写百分比按已完成分块计算（默认每块约 5 分钟），不代表整项任务的时间比例。音频获取显示实际处理时长和速度；总时长未知时不编造百分比。剩余时间只在有处理样本后估算。更新失败会提示数据可能已过期。

- 本轮 Python 回归：84 项通过，覆盖队列状态、取消竞态、签名刷新、块恢复、模型切换、协议边界和导出防覆盖。
- JS 接口夹具和语法检查通过：`preview_url`、数字键对象流、元数据白名单、来源参数和 seek。
- 真实30秒课堂音频在离线 CPU int8 模式下，经 CLI→FFmpeg→Whisper small→导出完成；原生 stdio ping、桥退出后 worker 可查询、重复提交及临时音频清理通过。[运行证据](artifacts/implementation/runtime-validation.json)
- 真实 Edge 页面调试验证了 DOM 采集；用户安装扩展后，实际扩展的 Native Messaging ping、短样本提交→转写→状态查询，以及 `#clip-t=123` 跳转均通过。测试使用本地短音频，不等于整课在线取音或 popup 点击全流程已验收。[浏览器证据](artifacts/implementation/browser-validation.json)
- 短样本直接 ASR 推理耗时约2.30秒；应用验收总耗时约10.72秒，包含模型加载、媒体转换、CLI轮询与协议检查，不能外推为整课速度。[质量记录](artifacts/implementation/asr-baseline-notes.md)
- 已发现疑似专业术语错字；没有人工参考稿，不能报告准确率。模型可替换，产物需对照录音复核。
- 本轮新增合成数据验收：真实 Headless Edge 中的任务中心筛选、控制、预览、键盘焦点与窄窗口布局通过；真实 HTTP worker 完成合成音频处理后，任务中心预览及重新导出实际文件通过。识别部分使用 mock，不代表整课识别质量或速度测试。
- **尚未验收**：popup 点击提交的完整在线课程链路、整节长课、签名有效期、HLS/多路音轨、浏览器重启和完整安装迁移。

复验命令：

```powershell
.venv\Scripts\python.exe -m unittest discover tests -q
node tests/test-extension.cjs
node tests/test-popup-progress.cjs
node tests/test-background-messaging.cjs
node tests/test-extension-isolation.cjs
node tests/test-dom-fallback.cjs
node tests/test-dashboard-and-settings.cjs
node tests/test-preview-isolation.cjs
node tests/test-settings-bootstrap.cjs
# 浏览器验收需可解析的 playwright 包与其浏览器；可设置 PLAYWRIGHT_MODULE、BROWSER_EXECUTABLE
node tests/test-playwright-dashboard.cjs
node tests/test-dashboard-integration.cjs
.venv\Scripts\python.exe scripts/validate-runtime.py
```

运行验收脚本会使用本项目保留的课堂短样本；脚本不会将音频或转写发送给第三方。

## 调研资料

- [需求、网页结构与技术路径（调研阶段记录）](docs/discovery-and-technical-plan.md)
- [本地 ASR 候选与官方资料](docs/offline-asr-research.md)
- [媒体探测脚本](scripts/probe-media.ps1)

原始需求文档保留不变。全站爬取、自动总结、板书/PPT OCR 和移动端不属于当前 MVP。
