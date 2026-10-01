# 离线转写方案调研（经主代理复核）

日期：2026-10-01。AGY 员工完成初步资料研究，主代理核对关键官方资料并重写过强结论。本文件是最终采用版本；员工原始回执留在 `.agy-staff/jobs/`，不作为已验证结论。

## 结论与证据边界

推荐第一轮原型用 Python + faster-whisper，理由是现成的段级时间戳、VAD 和 CPU int8 路径便于建立可测基线，不是它已被证明识别最准确。同时将 sherpa-onnx / SenseVoice 列为 CPU 候选，whisper.cpp 列为原生分发候选。三者均未在本机课程音频上进行 ASR 对比，不能给出速度、字错率或最优排名。

本轮网站实测和 FFmpeg 音频提取已经完成，见 `discovery-and-technical-plan.md`。下载课程需要网络；模型与依赖就绪后，识别和排版可以在本地运行。合适的承诺是“课程音频与逐字稿不上传云端识别服务”，不能承诺全流程绝对离线或零泄露风险。

## 引擎候选

| 候选 | 官方已提供的能力 | 本项目的限制与待测项 |
| --- | --- | --- |
| faster-whisper | CTranslate2 实现；CPU/GPU int8；段级与可选词级时间戳；Silero VAD；Python 3.9+。当前 GPU 路径要求匹配的 CUDA 12、cuBLAS、cuDNN 9 | 先跑 CPU 基线。Windows GPU 依赖应按发布版本核验；个别 DLL issue 不能推广成所有机器必然失败。提示词也可能引入偏差，不能当作强制纠错 |
| whisper.cpp | 原生 C/C++ Whisper 实现；支持量化、VAD，官方仓库列有 CPU、CUDA、Vulkan、OpenVINO 等后端 | 适合后续原生打包。具体 Windows release 中有哪些二进制、各后端的兼容条件必须在选定版本核验；不承诺任意 Intel/AMD GPU 都可加速 |
| sherpa-onnx / SenseVoice | 官方提供 SenseVoice ONNX 推理、Windows 支持、多语言模型、VAD/字幕示例 | 可作为 CPU 轻量候选。须核对所选 API 的输出时间戳，是 VAD 块边界还是模型内部 token 边界；不能把两者都叫精准词级时间戳。GPU provider 可用性须验证，不能从 ONNX Runtime 一般能力推导其打包版本支持 DirectML |

依据：[faster-whisper 官方仓库](https://github.com/SYSTRAN/faster-whisper)、[whisper.cpp 官方仓库](https://github.com/ggml-org/whisper.cpp)、[sherpa-onnx SenseVoice 官方文档](https://k2-fsa.github.io/sherpa/onnx/sense-voice/index.html)。公开基准的硬件、数据集、批大小不同，不直接外推成本项目速度或中文课堂准确率。

Whisper 与 SenseVoice 的模型结构不同，但非自回归不意味着静音或底噪免疫。所有候选均可能漏字、错字；需要带静音、混响、公式术语和中英混合的真实样本评测。架构资料：[Whisper 论文](https://arxiv.org/abs/2212.04356)、[SenseVoice 官方仓库](https://github.com/FunAudioLLM/SenseVoice)。

## 时间轴与逐字稿保真

保留原始 ASR segments JSON，再派生 Markdown 和 SRT。原始输出包含文本、start/end、引擎与模型版本、推理设置及疑似异常标记。默认不删口头禅、重复词或老师的自我纠正；只处理空白、排版与显式标注。词级时间戳属于可选升级，不承诺毫秒级准确。

长课采用可恢复的媒体块，块内启用 VAD；从源视频时间轴记录每块实际起点。公式为：`全局时间 = 流的 time_offset + 块在媒体中的起点 + 块内相对时间`。若主动把 VAD 后的语音拼接成新音频，必须另存每个语音区间的原始偏移，不能只加一个块起点。若引擎已经返回原始音频坐标，不得再次重复加偏移。

块长、静音阈值和边界 padding 是待调参数。切分尽量选停顿；连续长讲话必须允许带重叠的边界，重复识别只在重叠区结合时间去重，不能按相同字符串删除整课内容。缺失、超时或低置信块标记“待重试/待复核”，不能默默跳过并宣称成功。

标准化、标点和逆文本标准化都可能造成误改，尤其理工科变量、单位和代码。MVP 不额外引入这些处理模型；需要时以可选派生稿提供，并保留原稿。确定性规则也不等于语义一定正确。

## 浏览器与本机通信

优先考虑 Chromium 扩展 + 本地持久 worker。扩展只识别当前授权回放、提交和查询任务；持久队列和识别计算在本机。浏览器扩展可复用用户当前页面，独立桌面 WebView 通常要重新建立自己的登录会话；这里并未实测所有 SSO/WebVPN 环境。

Native Messaging 是推荐的正式集成候选：无需网页直接请求 localhost，但要安装本地主机配置。Windows 官方支持用户级注册表入口；content script 需经扩展 service worker 转发，宿主与扩展 ID 绑定。传元数据/控制消息，不传大块音频。[Chrome 官方 Native Messaging 文档](https://developer.chrome.com/docs/extensions/develop/concepts/native-messaging)

连接存活可能延长 MV3 worker 生命周期，但宿主退出、端口关闭和浏览器退出仍须处理。原生宿主不应直接拥有一小时识别任务的唯一生命周期：提交后由独立 worker 持续执行，重开浏览器能按 job ID 查询结果。[Chrome 官方生命周期文档](https://developer.chrome.com/docs/extensions/develop/concepts/service-workers/lifecycle)

带 host_permissions 的扩展 service worker 可以跨域请求；content script 则受页面来源约束。因而不能断言所有 localhost 方案不可行。localhost HTTP 可用于开发原型，但目标 Chrome/Edge 版本的本地网络权限、请求来源、服务鉴权与浏览器退出行为须实测，不能以旧 PNA 博客代替当前验证。[Chrome 官方网络请求文档](https://developer.chrome.com/docs/extensions/develop/concepts/network-requests)

脚本桥接响应时须注意 isolated world 与 MAIN world 的区别；建议仅匹配目标接口，提取必要元数据和有效媒体地址，不复制整份请求头、Cookie 或用户信息。[Chrome 官方 content scripts 文档](https://developer.chrome.com/docs/extensions/develop/concepts/content-scripts)

## 需求修正与分期

MVP：单节当前回放、媒体取音、串行持久队列、原始逐字稿、段级时间戳、Markdown/SRT/JSON、明确目录、失败可重试、成功后清理本任务临时媒体。

延期：批量课程、自动感知 Obsidian Vault、词级对齐、术语词典、桌面通知、原生安装器、跨源 HLS、板书或 PPT OCR。

普通跨域 iframe：主代理测得课程网页 `X-Frame-Options: SAMEORIGIN`，不能作为默认交付承诺。保留稳定课程页链接，后续由本工具扩展识别时间 hash 并执行 seek；删除本地视频和在线时间戳回看并不逻辑冲突，但源站原生时间参数尚未验证。

## 下一轮应验收的证据

1. 固定真实课堂样本和人工校对稿，在上述候选中测 CER、术语漏错、静音误出字、时间戳偏差、墙钟耗时和峰值内存；保存配置并区分原稿和格式化稿。
2. 一节完整课程的获取完整性和实际音频末尾覆盖；短样本成功不能替代长任务验收。
3. 断网/签名过期后的刷新与恢复；浏览器关闭、worker 重启后队列恢复；重提交同一课次不重复处理。
4. 固定 Chrome/Edge 与本地主机版本，实测安装、通信和任务脱离标签页生命周期。
