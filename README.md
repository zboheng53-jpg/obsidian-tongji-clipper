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
.venv\Scripts\tongji-clipper.exe worker stop
```

默认导出到项目 `output/`；`--output-dir` 可指向 Obsidian Vault 的子目录。`--keep-audio` 保留任务的 16kHz WAV。`--time-offset 120` 表示输入片段从课程第120秒开始，**仅偏移导出时间轴，不截掉输入前120秒**。

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
- 任务以课程/课次/流及转写配置去重，不以签名 URL 去重。排队中重提交会更新签名；失败/取消后可重试。设置改变会生成不同任务。
- 已完整获取的音频可用于恢复；未完成的获取会重试。Checkpoint 记录完成块、音频指纹和原始分段；不把最后一句的结束时间当作完成边界。
- 每个任务独立目录，已有产物生成 revision，避免覆盖用户编辑。输出包括 `.md`、`.srt`、`.segments.json`、`.raw.json`；checkpoint 保留原始块和置信度。
- 成功写入并核验后清理本任务临时音频，保留原始输入。失败时保留缓存以便重试。临时媒体签名不进入笔记和公开状态。
- 没有 LLM 总结或润色。跨块去重只比较时间重叠的同句；保留原始 ASR 块供复核，无法承诺消除所有边界识别差异。

## 实际验收与限制

任务窗口每 2 秒自动刷新，显示阶段指示和转写进度条。转写百分比按已完成分块计算（默认每块约 5 分钟），音频获取、模型加载与导出显示动态条；百分比不代表整项任务的时间比例。更新失败会提示数据可能已过期。修改扩展后请在 Edge 扩展管理页点击“重新加载”，本机服务在空闲时重启后会使用最新进度回报。

- 最终 Python 回归：48 项通过，覆盖队列状态、取消竞态、签名刷新、块恢复、模型切换、协议边界和导出防覆盖。
- JS 接口夹具和语法检查通过：`preview_url`、数字键对象流、元数据白名单、来源参数和 seek。
- 真实30秒课堂音频在离线 CPU int8 模式下，经 CLI→FFmpeg→Whisper small→导出完成；原生 stdio ping、桥退出后 worker 可查询、重复提交及临时音频清理通过。[运行证据](artifacts/implementation/runtime-validation.json)
- 真实 Edge 页面调试验证了 DOM 采集；用户安装扩展后，实际扩展的 Native Messaging ping、短样本提交→转写→状态查询，以及 `#clip-t=123` 跳转均通过。测试使用本地短音频，不等于整课在线取音或 popup 点击全流程已验收。[浏览器证据](artifacts/implementation/browser-validation.json)
- 短样本直接 ASR 推理耗时约2.30秒；应用验收总耗时约10.72秒，包含模型加载、媒体转换、CLI轮询与协议检查，不能外推为整课速度。[质量记录](artifacts/implementation/asr-baseline-notes.md)
- 已发现疑似专业术语错字；没有人工参考稿，不能报告准确率。模型可替换，产物需对照录音复核。
- **尚未验收**：popup 点击提交的完整在线课程链路、整节长课、签名有效期、HLS/多路音轨、浏览器重启和完整安装迁移。

复验命令：

```powershell
.venv\Scripts\python.exe -m unittest discover tests -q
node tests/test-extension.cjs
node tests/test-popup-progress.cjs
.venv\Scripts\python.exe scripts/validate-runtime.py
```

运行验收脚本会使用本项目保留的课堂短样本；脚本不会将音频或转写发送给第三方。

## 调研资料

- [需求、网页结构与技术路径（调研阶段记录）](docs/discovery-and-technical-plan.md)
- [本地 ASR 候选与官方资料](docs/offline-asr-research.md)
- [媒体探测脚本](scripts/probe-media.ps1)

原始需求文档保留不变。全站爬取、自动总结、板书/PPT OCR 和移动端不属于当前 MVP。
