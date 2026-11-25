param()
# 1. 优先使用仓库虚拟环境；云端 CI 与未建虚拟环境的机器回退到系统 Python。
 $ErrorActionPreference = 'Stop'
 $projectRoot = Split-Path -Parent $PSScriptRoot
 $pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) {
  $pythonPath = 'python'
  if (-not (Get-Command python -ErrorAction SilentlyContinue)) { throw '未找到可用 Python。请先按 README 创建 .venv，或安装 Python 3.12+ 后重试。' }
}
 $assetsDir = Join-Path $projectRoot 'src\reader_cache_toolkit\assets'
 $iconPath = Join-Path $assetsDir 'app.ico'
if (-not (Test-Path -LiteralPath $iconPath)) { throw "缺少应用图标 $iconPath，请先运行 python scripts\make_icon.py 生成。" }

# 2. 从 pyproject.toml 读取版本号，动态生成 PyInstaller 版本资源（exe 属性中的产品名、版本、版权）。
 $pyprojectPath = Join-Path $projectRoot 'pyproject.toml'
 $pyprojectText = Get-Content -LiteralPath $pyprojectPath -Raw
if ($pyprojectText -notmatch '(?m)^version\s*=\s*"([^"]+)"') { throw '无法从 pyproject.toml 读取 version 字段。' }
 $version = $Matches[1]
 $versionParts = @(($version -replace '[^\d.]', '').Split('.')) + @('0', '0', '0', '0')
 $numericVersion = ($versionParts[0..3] -join ',')
 $buildPath = Join-Path $projectRoot 'build'
 $distPath = Join-Path $projectRoot 'dist'
New-Item -ItemType Directory -Force -Path $buildPath | Out-Null
 $versionFile = Join-Path $buildPath 'app-version-info.txt'
 $versionText = @"
# UTF-8
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=($numericVersion), prodvers=($numericVersion),
    mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable('080404b0', [
        StringStruct('CompanyName', 'reader-cache-toolkit'),
        StringStruct('FileDescription', '本地安卓阅读缓存备份与多格式导出工具'),
        StringStruct('FileVersion', '$version'),
        StringStruct('InternalName', 'ReaderCacheToolkit'),
        StringStruct('LegalCopyright', '(c) reader-cache-toolkit. 保留所有权利。'),
        StringStruct('OriginalFilename', 'ReaderCacheToolkit.exe'),
        StringStruct('ProductName', '阅读缓存工具'),
        StringStruct('ProductVersion', '$version')
      ])
    ]),
    VarFileInfo([VarStruct('Translation', [2052, 1200])])
  ]
)
"@
# PyInstaller 解析版本文件要求 UTF-8 无 BOM，此处显式指定编码避免 PowerShell 默认编码干扰。
[System.IO.File]::WriteAllText($versionFile, $versionText, (New-Object System.Text.UTF8Encoding($false)))
Write-Host "版本 $version 已写入 $versionFile"

# 3. 打包目录版：启动快、便于随目录分发，产物为 dist\ReaderCacheToolkit\ReaderCacheToolkit.exe。
& $pythonPath -m PyInstaller --noconfirm --windowed --onedir --name ReaderCacheToolkit `
  --icon $iconPath --version-file $versionFile `
  --add-data "$assetsDir;assets" `
  --paths (Join-Path $projectRoot 'src') `
  --distpath $distPath --workpath $buildPath --specpath $buildPath `
  (Join-Path $PSScriptRoot 'desktop_entry.py')
if ($LASTEXITCODE -ne 0) { throw '目录版桌面程序打包失败。' }

# 4. 打包单文件版：便于单独传输，启动时解包略慢，产物为 dist\ReaderCacheToolkit-OneFile.exe。
& $pythonPath -m PyInstaller --noconfirm --windowed --onefile --name ReaderCacheToolkit-OneFile `
  --icon $iconPath --version-file $versionFile `
  --add-data "$assetsDir;assets" `
  --paths (Join-Path $projectRoot 'src') `
  --distpath $distPath --workpath $buildPath --specpath $buildPath `
  (Join-Path $PSScriptRoot 'desktop_entry.py')
if ($LASTEXITCODE -ne 0) { throw '单文件版桌面程序打包失败。' }

# 5. 汇报产物位置，两种形态均包含应用图标与版本信息。
Write-Host '桌面程序打包完成：'
Write-Host "  目录版   $distPath\ReaderCacheToolkit\ReaderCacheToolkit.exe（移动时需保留整个目录）"
Write-Host "  单文件版 $distPath\ReaderCacheToolkit-OneFile.exe"
