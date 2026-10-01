$ErrorActionPreference = "Stop"

$Source = Split-Path -Parent $PSScriptRoot

function Pick-TargetFolder {
    $shell = New-Object -ComObject Shell.Application
    $folder = $shell.BrowseForFolder(0, "选择现有 XHS Favorites Picker v0.4/v0.5/v0.6.0 安装目录", 0, 0)
    if ($null -eq $folder) { return "" }
    return $folder.Self.Path
}

$Target = Pick-TargetFolder
if (-not $Target) {
    Write-Host "已取消升级。"
    exit 0
}

$SourceFull = [IO.Path]::GetFullPath($Source).TrimEnd('\')
$TargetFull = [IO.Path]::GetFullPath($Target).TrimEnd('\')
if ($SourceFull -ieq $TargetFull) {
    Write-Host "当前已经位于目标安装目录，不需要复制。" -ForegroundColor Yellow
    exit 0
}

if (-not (Test-Path (Join-Path $TargetFull "xhs_pick.py")) -and -not (Test-Path (Join-Path $TargetFull "downloads"))) {
    throw "所选目录看起来不是现有 XHS Favorites Picker：未找到 xhs_pick.py 或 downloads。"
}

Write-Host "" 
Write-Host "将新版程序文件写入：$TargetFull" -ForegroundColor Cyan
Write-Host "以下用户状态明确不会被覆盖：" -ForegroundColor Yellow
Write-Host "  downloads\"
Write-Host "  .runtime\"
Write-Host "  .chrome-debug-profile\"
Write-Host "  .redbook-cookies.json"
Write-Host "  .subscriptions.json"
Write-Host "  .favorites-cache.json"
Write-Host ""

$RootPatterns = @("*.py", "*.bat", "*.md", ".gitignore")
foreach ($pattern in $RootPatterns) {
    Get-ChildItem -Path $SourceFull -Filter $pattern -File -ErrorAction SilentlyContinue | ForEach-Object {
        Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $TargetFull $_.Name) -Force
    }
}

foreach ($dir in @("web", "scripts", "tests")) {
    $srcDir = Join-Path $SourceFull $dir
    $dstDir = Join-Path $TargetFull $dir
    if (Test-Path $srcDir) {
        New-Item -ItemType Directory -Force -Path $dstDir | Out-Null
        Copy-Item -Path (Join-Path $srcDir "*") -Destination $dstDir -Recurse -Force
    }
}

Write-Host "" 
Write-Host "程序文件升级完成；旧 downloads 仓库和登录/订阅状态均保留。" -ForegroundColor Green
Write-Host "建议下一步在旧目录运行 self_test.bat，然后运行 run.bat。"
exit 0
