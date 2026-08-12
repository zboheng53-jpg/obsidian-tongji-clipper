# 方案与边界

## 目标

在已登录的同济课程回放页点击浏览器扩展一次，自动生成可放入 Obsidian 的 Markdown 课程笔记。笔记保留课程链接和带时间戳的文本，不永久保存视频。

## 数据流

```text
已登录的 look.tongji.edu.cn/livingroom 页面
  └─ 浏览器扩展读取 DOM 和 video.currentSrc
       └─ POST http://127.0.0.1:8765/jobs
            └─ 本机 Python 服务分块下载临时 MP4
                 └─ 本地 faster-whisper 语音识别
                      └─ 固定规则合并时间戳
                           └─ 写入 Obsidian Vault
                                └─ 删除临时 MP4
```

首次配置使用独立的数据流：

```text
扩展设置页
  └─ 本机设置接口打开 Windows 文件夹选择器
       └─ 校验并保存 Obsidian Vault
            └─ 测试写入 Markdown
```

## 为什么不需要 Agent 或 LLM

- 页面提取由固定 CSS 选择器和正则表达式完成。
- 视频下载使用标准 HTTP Range 请求。
- 语音识别只使用本机 faster-whisper；它是 ASR 模型，不调用聊天模型或云端文本接口。
- 繁简转换只使用本机 OpenCC 的固定 `t2s` 字典。
- Markdown 由固定模板和确定性字符串处理生成。
- 不生成语义摘要，不进行 LLM 润色，也不需要 Codex 在运行时参与。
- 源码和依赖中没有 OpenAI、Anthropic、Gemini 等客户端。

## 运行时组件

### 浏览器扩展

- `extension/extractor.js`：读取课程名、教师、日期、教室、上课时间和当前视频地址。
- `extension/background.js`：响应工具栏点击，提交任务并轮询状态。
- `extension/options.html`、`options.css`、`options.js`：图形化选择 Vault、保存转写设置并测试写入。
- `extension/manifest.json`：只申请当前标签页、脚本注入、通知、任务状态和两个目标域名所需权限。

### 本机服务

- 只监听 `127.0.0.1`。
- 未选择 Vault 时仍可启动，但只提供健康检查和扩展设置流程；任务提交返回配置错误。
- 配置和任务接口只接受浏览器扩展来源及专用请求头，拒绝普通网页和无来源请求。
- 只接受 `look.tongji.edu.cn` 的页面和视频 URL。
- 同一课程正在处理时会复用现有任务，避免重复下载。
- 成功和失败时默认清理临时视频。

## 可复现性

每节新课程只改变输入数据：页面 URL、页面元数据和短期视频签名。下载、识别、时间戳合并、文件命名及写入步骤使用同一套代码。Whisper 模型第一次下载后保存在本地缓存，后续课程无需重复安装。

## 已知限制

- 同济页面若修改 `.course_name`、`.course_info_detail` 或播放器结构，需要更新选择器。
- 课程必须处于已登录且播放器加载完成的状态。
- ASR 可能误识别专有名词、代码、公式和数字；无 LLM 方案不会自动语义纠错。
- 处理时间取决于同济源站速度、课程时长和本机 CPU。
