# Tongji Course Clipper

面向 `https://look.tongji.edu.cn/livingroom` 的一键 Obsidian 课程剪藏原型。

它不调用 Agent、OpenAI API 或任何云端 LLM。浏览器扩展读取当前已登录页面中的课程信息和临时视频地址；本机 Python 服务下载视频、使用本地 faster-whisper 识别语音、通过 OpenCC 统一为简体中文、按固定规则生成 Markdown，然后删除临时视频。

> 注意：本项目仅供拥有相应课程访问权限的用户在本机整理自己的学习资料。请遵守同济大学平台的使用条款、课程版权规定及适用法律；请勿分享、传播或公开上传课程视频与转写内容。

## 安装

1. 安装 Python 3.11 或更高版本。
2. 在 PowerShell 中运行 `install.ps1`。
3. 在 Chrome 或 Edge 打开扩展管理页面，启用“开发者模式”，选择“加载已解压的扩展程序”，加载 `extension` 文件夹。
4. 打开扩展的“扩展选项”，点击“选择文件夹”并选择你的 Obsidian Vault。
5. 点击“生成测试笔记”；在 Obsidian 中看到测试文件后即配置完成。

`install.ps1` 会启动本地服务，并为当前用户设置开机自启，不需要管理员权限。以后正常使用时无需打开 PowerShell。运行 `disable-autostart.ps1` 可取消开机自启；需要手动启动或查看错误时可运行 `start.ps1`。

设置页还可以修改笔记子目录、Whisper 模型和是否保留临时视频，无需手动编辑 `config.json`。

第一次转写时会下载 faster-whisper `small` 模型；以后会复用本地缓存。

## 隐私与数据

- 课程元数据、视频下载、转写和 Markdown 写入均在本机完成；服务只绑定 `127.0.0.1`。
- `config.json`、任务记录、模型缓存、测试 Vault 与服务日志均不会纳入版本控制。
- 请不要将自己的 Vault 路径、课程链接、签名视频地址或实际课程内容提交到仓库。

## 使用

1. 登录同济录课平台并打开一节课程回放，等待播放器出现。
2. 点击浏览器工具栏中的 Tongji Course Clipper 图标一次。
3. 图标显示 `ASR` 表示正在处理；完成后显示 `OK` 并弹出通知。

如果尚未选择 Vault，点击扩展图标会自动打开设置页。如果显示本地服务未启动，请重新运行 `install.ps1`；需要查看具体启动错误时运行 `start.ps1`。

生成的文件名取自课程标题，例如：

```text
高级语言程序设计实验 2026-03-04第5-6节.md
```

笔记包含 YAML、课程链接/iframe、教师和教室信息，以及带时间戳的完整自动字稿。视频只保存在系统临时目录，成功或失败后默认删除。

## 自动化边界

- 页面必须已经登录，并且视频播放器已加载。
- `video_url` 是短期有效的签名地址，因此扩展点击后立即提交给本机服务。
- 转写完全自动，但 ASR 仍可能误识别专有名词、公式、代码和数字。
- 固定模板不会自动撰写课程摘要；这样可以保证流程不依赖 LLM。

## 本机接口

本机服务地址为 `http://127.0.0.1:8765`；它只监听本机回环地址，不会暴露到局域网。

- `GET /health`：服务健康状态和配置状态。
- `GET /settings`：读取可展示的设置。
- `PUT /settings`：保存笔记与转写设置。
- `POST /settings/select-vault`：打开 Windows 文件夹选择器并保存 Vault。
- `POST /settings/test-write`：生成测试 Markdown。
- `POST /jobs`：提交课程元数据和视频地址。
- `GET /jobs/{id}`：查询下载、转写和写入状态。

服务只监听 `127.0.0.1`，并拒绝普通网页来源的跨域任务提交。

## 验证

运行自动化测试：

```powershell
.venv\Scripts\python.exe -m unittest discover -s tests -v
node tests\test_extension.mjs
node tests\test_options.mjs
```

真实课程端到端验证数据见 `VALIDATION.md`。
