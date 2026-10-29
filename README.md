# tk对标工具台

TikTok 工作台本地最终版 2.0.0，支持 Windows 10/11 x64。

## 普通用户：下载后直接使用

到 [Releases](https://github.com/aozzz6/tk-duibiao-gongjutai/releases/latest) 下载 `TikTok工作台 Setup *.exe`（安装版），或下载全部 `tk-workbench-win-x64-part*.zip` 和 `extract-all.ps1`（便携版）并放在同一个文件夹。

安装版按向导安装即可。便携版右键在 PowerShell 中运行 `extract-all.ps1`，或分别解压所有 ZIP 到同一个目录并合并同名文件夹。必须解压全部分包，再打开 `TikTok工作台_本地最终版/TikTok工作台.exe`，不能只移动 EXE。

软件包含 Python、FFmpeg 和 Whisper medium 离线转写模型，无需另外安装 Node、Python、WorkBuddy 或 Agent。Code → Download ZIP 只有源码，普通使用者请下载 Releases 中的软件分包。

## 配置自己的 API

打开“工作台 → 设置”，分别填写文本模型和视觉模型的 API 地址、API Key、模型名称。接口采用 chat-completions 协议；视觉模型需要支持图片输入。也可使用兼容的本机模型服务。

- 本地功能：账号、分组、采集编排、离线转写、建表、回填、校验、统计报告、定时任务。
- 文本 API：翻译、AI 内容分析。
- 视觉 API：视频反推、提示词精修。
- 平台采集：需要可用网络、平台权限，必要时配置代理或登录态。
- 榜单监控：需要自行配置 Cookie 和监控来源；EchoTik 已下线。

首次启动数据为空。个人资料保存在 `%LOCALAPPDATA%\TikTokWorkbench`，不包含在发行包中。每位使用者自行配置 API 密钥、Cookie、账号与素材。定时任务需要软件运行且电脑保持唤醒。

## 从源码构建（维护者或开发者）

`desktop/` 为桌面程序，`payload/` 为已准备好的后端和前端资源，`standalone/` 为独立任务开发源码，`tools/` 保留原始准备工具，`tests/` 为测试脚本。

安装 Node.js 20 或更高版本，将完整发行包中的 `resources/runtime/` 复制到源码根目录的 `runtime/`，然后运行 `./build.ps1`。也可以直接传入运行时目录：`./build.ps1 -RuntimeSource 'D:\path\to\resources\runtime'`。脚本会生成安装版 `dist/TikTok工作台 Setup *.exe` 和便携目录 `dist/win-unpacked/`。

源码仓库不提交约 5.9 GB 的 Python、FFmpeg 和 Whisper 运行时；这是有意的。普通用户不需要构建，直接下载 Releases。发布者构建时应使用与当前版本匹配的完整运行时，并在发布前运行测试。

`tools/prepare.py` 和 `tools/final_audit.py` 保留原开发电脑路径，用于交付追溯，不属于上述构建流程。

## 首次使用与数据隔离

每台电脑首次启动都是空白工作台。账号、素材、台账、报告、模型 Key 和 Cookie 保存在当前 Windows 用户的 `%LOCALAPPDATA%\\TikTokWorkbench`，不会从发布者电脑带过去。打开“工作台 → 设置”，分别填写自己的文本模型和视觉模型 API 地址、Key 和模型名称；不填写模型 API 也可以使用本地转写、建表、回填和统计报告。

本公开版本面向本地分析、离线转写、建表、回填和报告。平台采集、榜单监控等功能需要用户自行提供网络权限、登录态和来源配置，默认不携带任何账号或 Cookie。

测试时设置 `TEST_RESOURCES` 为完整软件的 `resources` 路径，再运行对应的 `node tests/*.cjs`。具体操作见 [使用说明.txt](使用说明.txt)。

## 验证范围

原交付验收使用本机模拟模型服务和合成素材，没有用真实 Cookie/API 密钥执行平台采集或云端计费调用；第二台物理电脑尚未实测。实际效果取决于模型能力、网络和平台权限。历史验收见 [验收说明.md](验收说明.md)。
