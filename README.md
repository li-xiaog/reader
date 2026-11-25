# 阅读缓存工具

本地 Windows 桌面应用和命令行工具，用于从 Android 模拟器备份已下载的阅读缓存，并在本机转换 TXT、Markdown、EPUB 或 XHTML。支持番茄小说、起点读书、微信读书。

用于用户有权备份和转换的本地缓存。实际缺失的章节会记录在报告中，不补造正文，不联网获取书籍。应用不启动网络服务，也不改变模拟器权限。

## 安装

需要 Python 3.12 或更新版本；Windows Python 安装需包含 tkinter。ADB 仅在模拟器扫描、备份时需要。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock
.\.venv\Scripts\python.exe -m pip install -e '.[dev]'
```

## 桌面使用

本机已打包的版本可直接双击 `dist\ReaderCacheToolkit\ReaderCacheToolkit.exe`（目录版），或 `dist\ReaderCacheToolkit-OneFile.exe`（单文件版），运行时不需要另装 Python。目录版启动更快，移动程序时需要带上整个 `ReaderCacheToolkit` 目录；单文件版便于单独传输，首次启动需要几秒解包时间。源码运行方式如下：

```powershell
.\.venv\Scripts\python.exe -m reader_cache_toolkit gui
```

选择渠道、缓存目录、真实书籍 ID、尚不存在的输出目录和输出格式，再执行转换。微信读书旧备份需要原账号 VID 与 WRReader 数据库；通过本工具生成的备份会记录必要的本地元数据。

模拟器备份页先刷新设备，明确选择设备，再扫描书籍和备份。多设备、多账号遇到歧义会报错，不自动选取。设备需事先具有目标缓存目录的读取权限，程序不会执行 `adb root` 或启停模拟器。

## 命令行

```powershell
.\.venv\Scripts\reader-cache.exe devices
.\.venv\Scripts\reader-cache.exe scan weread --serial '<设备编号>'
.\.venv\Scripts\reader-cache.exe backup weread --serial '<设备编号>' --book-id '<书籍ID>' --output 'D:\reader-data\new-backup'
.\.venv\Scripts\reader-cache.exe convert qidian --source 'D:\reader-data\qidian-backup' --book-id '<书籍ID>' --output 'D:\reader-data\new-export' --formats txt epub
.\.venv\Scripts\reader-cache.exe convert fanqie --source 'D:\reader-data\fanqie-backup' --book-id '<书籍ID>' --output 'D:\reader-data\fanqie-export' --formats txt md epub xhtml
.\.venv\Scripts\reader-cache.exe convert weread --source 'D:\reader-data\weread-book' --reader-db 'D:\reader-data\WRReader' --account-id '<原账号VID>' --book-id '<书籍ID>' --output 'D:\reader-data\weread-export'
```

尖括号为必须替换的参数说明，并非可直接运行的示例数据。默认导出 TXT 和 EPUB，`--require-complete` 要求有可靠完整目录且全部已缓存。已存在的输出目录不会覆盖。

## 输出与错误

- TXT 为 UTF-8 BOM，包含合并版与分章版。
- Markdown 为按章节组织的合并文档。
- EPUB 保留微信读书的原生页面、图片、样式与脚注；番茄将原页面规范化并按已验证的缓存映射嵌入插图；起点由实际正文生成章节页。
- XHTML 导出已有原始页面和关联资源；无原始页面时由正文生成。
- `conversion_report.json` 记录范围、缺失项、诊断、输出哈希；`index.csv` 和 `chapters_meta` 保留章节索引及元信息。

章节解码失败、未知缓存结构、乱码替换字符、资源冲突或数据库损坏会终止转换。书籍先在独立临时目录生成并校验，成功后发布；失败只清理本轮创建的临时目录。报告不保存密钥或设备解析参数。

纯图片目录项没有可导出的真实文字，TXT/Markdown 会明确报错，应选择 EPUB 或 XHTML。原生 EPUB 引用本地不存在的资源时也会报错；可单独选择文本格式导出已经验证的文字。微信读书远程图片地址保留原样并在报告中说明，不自动联网补图。番茄图片通过实际缓存文件验证后嵌入 EPUB，缺失或无法唯一匹配时报错；XHTML 格式保留原文件和图片缓存，原链接不变。原始 XHTML 中值相同的重复 `data-*` 属性允许确定性去重，报告记录原始与生成资源哈希；其他结构损坏不自动修复。

## 工程结构与设计模式

`ui` 与 `cli` 负责输入输出；`service` 执行统一应用流程；`channels` 是渠道适配器；`codecs` 处理各自文件格式；`exporters` 按策略生成格式；`infrastructure` 管理只读数据库和 ADB。

- **适配器模式**：三种缓存布局统一为 `BookDocument` 与 `Chapter`，领域模型不依赖 UI。
- **策略模式**：TXT、Markdown、EPUB、XHTML 独立实现 `Exporter`，可以单独选择与扩展。
- **工厂模式**：工厂集中创建适配器和策略，桌面与 CLI 不包含渠道算法分支。
- **应用服务与端口隔离**：界面只发起请求、显示事件；工作线程执行服务，UI 事件通过队列回到主线程。

新增渠道需要实现 `ChannelAdapter.load` 并在工厂注册；新增格式需要实现 `Exporter.export`。不要在界面中加入解析算法或 ADB shell 拼接。

详细依赖关系和流程见 [架构说明](docs/architecture.md)。

## 开发与打包

```powershell
.\.venv\Scripts\python.exe -m ruff check src scripts
.\.venv\Scripts\python.exe -m ruff format --check src scripts
.\.venv\Scripts\python.exe -m build
.\scripts\make_icon.py
.\scripts\build-desktop.ps1
```

`make_icon.py` 生成包内 `src\reader_cache_toolkit\assets\app.ico` 应用图标；`build-desktop.ps1` 从 `pyproject.toml` 读取版本号并生成版本资源，一次性产出两种形态：目录版 `dist\ReaderCacheToolkit\ReaderCacheToolkit.exe` 与单文件版 `dist\ReaderCacheToolkit-OneFile.exe`，二者均内嵌应用图标与版本信息。

云端打包：仓库已包含 `.github\workflows\build-desktop.yml`，任意分支推送或手动触发即会在 GitHub Actions 的 Windows 环境自动完成检查、图标生成与两种形态打包；在对应 Run 页面的 Artifacts 下载 `ReaderCacheToolkit-windows-dir.zip` 与 `ReaderCacheToolkit-windows-onefile.zip` 即可使用。推送 `v*` 形式的标签（如 `v0.1.0`）时会同时把这些压缩包发布到该标签的 GitHub Release。

Windows 本地打包结果位于 `dist\`，构建结果、虚拟环境、用户缓存、数据库、原始 APK、正文、测试与临时文件均不进入 Git。

## 迁移范围

迁移自原目录的番茄、微信读书、起点实测 Python 脚本及安卓缓存备份流程。原代码和用户数据保留原位，以便核对迁移前后的结果。新工程不依赖原目录脚本，也不内置任何书籍、账号、密钥、设备标识或个人绝对路径。

起点当前已验证版本为 7.9.460 的无头信息 `.qd` 格式。其他应用版本以真实缓存解析校验为准；未知结构明确报错。保留原有版权页、作者信息与版权声明。

本机迁移验收已通过：番茄 724 章、起点 93 个目录项的四格式导出；微信读书 8 章四格式、68/91 章原生 EPUB 与 XHTML、55 章文本与 XHTML，以及 MuMu 新备份 11 章的文本与 XHTML 导出。起点实际目录共 166 项，未缓存 73 项如实记录；微信读书部分缓存存在纯图片页面或缺少图片，各格式按前述规则明确处理。章节数只表示此次备份范围，不代表应用当前全书目录。

本机完成了 Ruff 检查、全部方法注释检查、失败不发布半成品及原数据库哈希保持不变等验证。验证脚本和数据仅保存在 Git 忽略目录，遵循测试文件不入库的约定。
