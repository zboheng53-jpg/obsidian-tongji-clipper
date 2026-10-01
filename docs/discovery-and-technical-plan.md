# 同济课程逐字稿工具：需求、网页结构与技术路径

调研日期：2026-10-01，Asia/Shanghai。

此文保留 2026-10-01 调研阶段的观察、假设和当时未验收项，不表示这些项目目前仍未实现。执行阶段的当前实现、验收和安装状态以 [README](../README.md) 为准；以下的“本轮”“尚未”均指本调研阶段。

本轮范围是需求梳理、登录网站实查和技术可行性验证。项目起初只有功能需求文档，尚无应用代码。本轮新增媒体探测脚本和调研资料，不将它们称为完整转写工具。原始需求文档保持不变。

## 1. 推荐路线

**Windows 本地 worker + Chromium 浏览器扩展 + FFmpeg + 可替换的本地 ASR 引擎。**

网站获取路线已在一门课上验证：读取正常播放器的有效签名 MP4 → 本地 FFmpeg 提取音轨 → 生成 16 kHz 单声道 PCM。没有复制登录 Cookie，也没有将课程音频发送给 AGY 或云端识别服务。第二门课确认相同 MP4 形态，但未做第二次取音验证。

ASR 原型先采用 faster-whisper CPU int8 建立基线，SenseVoice/sherpa-onnx 作为另一候选。推荐依据是接口与时间戳功能便利，准确率与速度仍待真实课堂对比。正式浏览器集成优先试 Native Messaging；本轮没有安装扩展、注册宿主或部署常驻服务。[ASR 调研及官方资料](offline-asr-research.md)

当前已登录的内置浏览器用于本轮勘查；正式扩展要在目标 Chrome/Edge 的登录页面中工作。不能假定本轮内置浏览器的会话会自动转移给其他浏览器或本地应用。

## 2. 本次观察与实验

| 项目 | 示例课程A | 示例课程B |
| --- | --- | --- |
| 稳定课程标识 | tenant=222 / course=12001 / sub=23001 | tenant=222 / course=12002 / sub=23002 |
| 课次标题 | 2026-09-30第3-4节 | 2026-09-23第19:20:00 |
| 当前课次主讲字段 | 示例教师A | 示例教师B |
| 课表时间 | 10:00–11:35，95分钟 | 19:20–20:05，45分钟 |
| 实际视频时长 | 5997.7025秒，约100分钟 | 5547.431771秒，约92分钟 |
| 流形态 | 带签名 MP4；HTTP 206 Range | DOM 直接 MP4；API is_m3u8=no |
| 字幕线索 | 无 HTML track；enable_translation=0 | 无 HTML track；enable_translation=0 |
| 本地取音实测 | 成功，120秒起取30秒 | 未进行 |

“无字幕”仅指这两门样本的 DOM/API 线索，不能推断全站永远没有字幕。PPT 区均显示“未获取到PPT”，导出课件不是逐字稿入口。

力学样本完整 MP4 的响应标明 799,998,732 字节；实际编码 H.264 1280×720，AAC 48 kHz 双声道。虽然媒体文件名包含 `1920_1080`，实际分辨率不同。

FFmpeg 最终探测脚本执行成功：30秒样本输出 PCM s16le、16000 Hz、1声道、实际时长29.999813秒、960,072字节；脚本用 ffprobe 验收并写入 `artifacts/probe/latest-probe.json`。这证明短段读取与转码可行，不证明整课下载完整、ASR准确或签名永久有效。

机器可读取的信息为 Core Ultra 9 285H、16逻辑处理器、约31.5 GiB内存。GPU型号查询受当前环境限制，未确认GPU加速能力；不以CPU型号推断CUDA可用。已有 FFmpeg/ffprobe 9.0，Python转写环境及模型尚未配置验证。

## 3. 网页与接口结构

首页有课程广场、我的课程、今日课程、周课表和最近学习。最近学习/课程回放入口会打开课程页新标签。

稳定回放 URL：

```text
https://look.tongji.edu.cn/livingroom?course_id=<course>&sub_id=<sub>&tenant_code=222
```

播放器为同页 `HTMLVideoElement`，容器 ID `cmc_cmcPlayer_container`，脚本来自 `/static/cmcPlayer/`。标题、教师/教室/课表时间、课程目录与PPT/笔记区同页展示。无需靠全屏录制或等课程实时播放完，当前样本可以直接读媒体。

以下都是观察到的正常 GET 请求，不是推测的隐藏接口：

| 接口 | 参数/作用 | 关键数据及处理 |
| --- | --- | --- |
| `/courseapi/v3/multi-search/get-course-detail` | course_id；课程及课次概况 | `title`、`term`、`sub_list`；课程教师不必等于本节主讲 |
| `/courseapi/v3/portal-home-setting/get-sub-info` | course_id、sub_id；当前课次 | `{code,msg,data}`；`course_title/sub_title/lecturer_name/room_name/start_at/end_at/duration` |
| `/courseapi/v2/course/catalogue` | course_id；课次目录 | `{success,result:{code,msg,data}}`；部分 `content` 是 JSON 字符串，返回包装与上一个不同 |
| `/coursesourceapi/course/study-auth/<course>/<sub>` | 当前用户观看权限 | `hasPermission/userPermission/videoPermission` 等；无权限应停止，不通过改ID尝试其他课程 |
| `/courseapi/v2/play-template/get-config` | tenant_id、course_id、sub_id | 页面模块配置；两个样本均有视频、PPT、目录、笔记模块 |

`get-sub-info`、`catalogue` 和 `study-auth` 的正常请求中观察到 Authorization 请求头。本轮未保存或转发其值。**短段媒体无需 Cookie 的成功结果，不代表元数据接口无需登录。**

媒体相关字段：`playurl`、`video_list`、`segment_video_list`、`content.save_playback`。`video_list` 在样本中是以数字键组成的对象，不能只按数组实现。流带 `time_offset`，两个样本均为0；站点存在多路/分段结构，应在适配层保留，不能随意拼接各路视频。

`content.download_url` 在样本中是服务端 Windows 路径，而非HTTP地址，不能直接当下载链接。任务应使用已加载播放器的 `video.currentSrc` 或有效 `preview_url/playurl`。

观察到 MP4 签名查询参数名为 `clientUUID` 与 `t`。页面重载后地址值会变化，实际有效期未测。签名只属于短期输入，不应进入Markdown、日志或幂等键。稳定来源链接只保存课程页 URL。

## 4. 应采用的采集方式

先用 DOM 读取视频与可见元数据完成最小链路；扩展适配器再捕获正常 `get-sub-info` 响应中的必要字段，以区分本节主讲与课程教师。优先复用已授权页面响应，避免导出整套账号凭据。

拟实现：在匹配课程页的 `document_start` 安装小范围 MAIN-world 响应桥接，仅观察目标 fetch/XHR 的正常响应，再将必要字段交给 isolated content script 和扩展后台。此方案尚未在扩展里实测；DOM 是回退路径。仅有 webRequest 通常不足以直接取得响应 JSON；本轮 CDP 能读响应体，不意味着正式扩展也天然具有同等权限。

元数据统一结构建议：

```text
CourseCapture
  schema_version, tenant_code, course_id, sub_id
  source_url, course_title, sub_title
  lecturer_name, course_teachers[], room_name
  scheduled_start/end, media_duration
  media_streams[{stream_id, role, time_offset, signed_url}]
```

`signed_url` 仅在受控临时任务数据中使用；长期笔记和元数据不保存。主讲缺失时注明未知，不拿课程教师集合推断当堂发言者。字段取不到时应允许空值并提示，避免猜测。

## 5. 需求修正

| 原需求 | 本轮建议 |
| --- | --- |
| 逐字稿，同时删除口吃/重复词 | 默认保留原话、老师更正及真实重复；只对分块重叠造成的重复做时序去重。整理稿另作可选输出 |
| 全流程离线、零数据出机 | 改成“媒体从同济获取，模型下载后在本地识别；课程音频/逐字稿不上传第三方识别服务” |
| iframe在笔记内播放原网页 | 对课程页 HEAD 实测 `X-Frame-Options:SAMEORIGIN`；普通 Obsidian 跨域 iframe 不应承诺。MVP保存来源链接 |
| 点击时间戳自动回看 | 调研时尚未验证源站原生参数，提出由扩展识别 `#clip-t=120` 并 seek；该机制已进入当前扩展实现，具体验收范围见 [README](../README.md) |
| 根据课表时间截取音频 | 使用媒体实际时长；示例课程B的45分钟课表对应92分钟录像，按课表截断会丢内容 |
| 即转即删 | 只在完整输出写入并验证后清理该任务临时媒体；失败块保留可重试，不能先删后发现笔记没写成功 |
| 不用LLM就保证准确纪实 | 不用LLM二次改写仍可能ASR错字。保留原始segments和待复核标记，不能承诺字字准确 |

本地只落音频会节约磁盘，但**不等于只下载音频的网络字节**：MP4音视频通常交错存储。整课取音可能仍要读大量复用数据；短段seek成功不能推出整课会节约同样比例流量。模型识别进度和媒体获取进度分别记录，不能合成虚假的完成百分比。

## 6. MVP 架构与生命周期

```mermaid
flowchart LR
    A[已登录课程回放] --> B[扩展：元数据和有效媒体地址]
    B --> C[原生桥：提交和查询]
    C --> D[本地持久队列]
    D --> E[FFmpeg获取音频]
    E --> F[分块与本地ASR]
    F --> G[JSON / Markdown / SRT]
    G --> H[验证后清理临时媒体]
```

扩展后台不拥有长时间识别任务；本地主机桥只转发命令，独立 worker + SQLite 持久化任务。Native Messaging 是首选候选；localhost HTTP可作为开发选项，仍需在目标浏览器核验网络权限。原生桥断连后，应能够通过持久 job ID 查询，而不是丢失任务。[Chrome Native Messaging](https://developer.chrome.com/docs/extensions/develop/concepts/native-messaging)、[扩展生命周期](https://developer.chrome.com/docs/extensions/develop/concepts/service-workers/lifecycle)

MVP只允许一条媒体获取任务和一个ASR计算任务，不进行多课程高并发。幂等键以 tenant/course/sub/选择的主音轨及转写配置为基础，不包含变化的签名 URL；排队中重复提交复用任务，换模型的重转写需明确区分。

状态至少区分：queued → acquiring → transcribing → writing → completed；附带 needs_refresh、failed、cancelled/interrupted。任务块保存进度，重启后重新确认中断块；禁止将漏转块默认为完成。

**签名刷新是重要未决项**：排队过久的地址可能失效，不能反复请求过期URL。获取阶段启动时优先从仍登录的课程页刷新；页面已关闭/会话失效时进入 needs_refresh，引导重新打开课程。只有媒体已完整落入本地缓存后，才能保证关闭课程页不影响识别。若要承诺“点击立刻离开”，需专门验证先获取后排队或自动打开授权课程页的策略。

Markdown按明确输出目录写入；默认保存原始segments JSON与SRT，原始音视频为临时介质。文件名包含规范化课程名+课次，清除非法字符，冲突时不能覆盖用户唯一改动。Frontmatter保存稳定课程页、课程/课次标识、教师、生成时间、模型版本；不嵌入签名URL。

## 7. 实施顺序与验收

1. **取音原型（本轮已完成短段）**：实际课程签名→本地音频，输出规格可验证。下一阶段扩为一节完整课，检查开头、末尾与总时长，并测获取流量/耗时。
2. **转写原型**：选含术语、静音与中英混合的样本，生成未经改写的逐字稿，对照人工听写；记录CER/关键术语错漏、时间偏差、耗时和内存，再确定模型档位。
3. **一键链路**：目标Chrome/Edge登录页→扩展→持久worker→Markdown/SRT/JSON；证明离开标签页、浏览器重开、重复点击、网络失败、签名过期与输出目录不可写时行为正确。
4. **体验完善**：模型和宿主安装包、设置、完成通知、Obsidian目录绑定与时间戳在线跳转。批量学期、词级对齐、PPT/板书OCR后置。

暂不实现自动总结、LLM“润色”、全站爬取、语音合成、移动端或复杂知识图谱。这些不是本轮逐字稿目标所必需。

截至本调研阶段，本轮尚未验证整课转写质量、完整媒体抓取、签名有效期/续传、新登录状态与权限拒绝分支、多路音轨和HLS、扩展安装通信及网页 seek。之后的实现与验收有更新；哪些已经通过、仍待实测，以 [README 当前验收与限制](../README.md#实际验收与限制) 为准。两门普通 MP4 样本不能代表平台所有历史资源。

## 8. 已交付的复现材料

- `scripts/probe-media.ps1`：接收当前有效媒体 URL，最多取60秒样本；不存签名值，用ffprobe验收规格。
- `artifacts/probe/latest-probe.json`：最终脚本成功执行的验收结果。
- `artifacts/probe/site-observations.json`：脱敏网页/API观察与两门样本信息。
- `docs/offline-asr-research.md`：AGY调研经主代理核验、修订后的离线ASR候选与依据。

运行样例（不要将有效签名复制进提交到Git的脚本）：

```powershell
# 将当前播放器的有效URL放入进程环境变量，仅用于本次调用。
.\scripts\probe-media.ps1 -MediaUrl $env:TONGJI_MEDIA_URL -StartSeconds 120 -DurationSeconds 30
```

当前保留约0.96MB的短音频样本作为验证材料，不是完整课程存档。它不含转写结果，也不会被送给AGY。
