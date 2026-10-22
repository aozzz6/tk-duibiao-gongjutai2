# tk对标工具台

TikTok 工作台本地最终版 2.0.0，支持 Windows 10/11 x64。

## 下载并使用

到 [Releases](https://github.com/aozzz6/tk-duibiao-gongjutai/releases/latest) 下载全部 `tk-workbench-win-x64-part*.zip` 和 `extract-all.ps1`，放在同一个文件夹。

右键在 PowerShell 中运行 `extract-all.ps1`，或分别解压所有 ZIP 到同一个目录并合并同名文件夹。必须解压全部分包，再打开 `TikTok工作台_本地最终版/TikTok工作台.exe`，不能只移动 EXE。

软件包含 Python、FFmpeg 和 Whisper medium 离线转写模型，无需另外安装 Node、Python、WorkBuddy 或 Agent。Code → Download ZIP 只有源码，普通使用者请下载 Releases 中的软件分包。

## 配置自己的 API

打开“工作台 → 设置”，分别填写文本模型和视觉模型的 API 地址、API Key、模型名称。接口采用 chat-completions 协议；视觉模型需要支持图片输入。也可使用兼容的本机模型服务。

- 本地功能：账号、分组、采集编排、离线转写、建表、回填、校验、统计报告、定时任务。
- 文本 API：翻译、AI 内容分析。
- 视觉 API：视频反推、提示词精修。
- 平台采集：需要可用网络、平台权限，必要时配置代理或登录态。
- 榜单监控：需要自行配置 Cookie 和监控来源；EchoTik 已下线。

首次启动数据为空。个人资料保存在 `%LOCALAPPDATA%\TikTokWorkbench`，不包含在发行包中。每位使用者自行配置 API 密钥、Cookie、账号与素材。定时任务需要软件运行且电脑保持唤醒。

## 源码与重新构建

`desktop/` 为桌面程序，`payload/` 为已准备好的后端和前端资源，`standalone/` 为独立任务开发源码，`tools/` 保留原始准备工具，`tests/` 为测试脚本。

安装 Node.js，将完整发行包中的 `resources/runtime/` 复制到源码根目录的 `runtime/`，然后运行 `./build.ps1`。构建使用现成的 `payload/`，结果输出到 `dist/win-unpacked/`。

`tools/prepare.py` 和 `tools/final_audit.py` 保留原开发电脑路径，用于交付追溯，不属于上述便携构建流程。

测试时设置 `TEST_RESOURCES` 为完整软件的 `resources` 路径，再运行对应的 `node tests/*.cjs`。具体操作见 [使用说明.txt](使用说明.txt)。

## 验证范围

原交付验收使用本机模拟模型服务和合成素材，没有用真实 Cookie/API 密钥执行平台采集或云端计费调用；第二台物理电脑尚未实测。实际效果取决于模型能力、网络和平台权限。历史验收见 [验收说明.md](验收说明.md)。
