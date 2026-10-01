# 真实课堂短样本 ASR 基线

2026-10-01，使用上一阶段保留的力学课程 120–150 秒片段。音频仅在本机处理，模型从公开仓库预先下载；运行设置 `HF_HUB_OFFLINE=1` 并以本地路径、`local_files_only=True` 加载。

- 输入：29.999813 秒，16 kHz 单声道 PCM。
- 引擎：faster-whisper 1.2.1 / CTranslate2 4.8.2，Whisper small，CPU int8，8 线程。
- 解码：中文、beam_size=5、VAD 开启。
- 模型加载：1.468 秒；分段识别：2.298 秒。
- 输出：5 段；原始结果见同目录之外的 `../probe/asr-baseline.json`。

这是直接引擎基线，不是完整应用链路的验收。短片速度不能直接外推整课耗时。结果出现“主使”“主剧”等疑似术语错字；尚无人工参考稿，不能报告 CER 或准确率，也没有据此改写老师原话。输出应对照录音复核。应用链路另由 `scripts/validate-runtime.py` 验证并记录。
