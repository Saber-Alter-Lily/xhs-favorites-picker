$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent $PSScriptRoot
$Runtime = Join-Path $Root ".runtime"
$CookieFile = Join-Path $Root ".redbook-cookies.json"
$DebugProfile = Join-Path $Root ".chrome-debug-profile"
$CliJs = Join-Path $Runtime "node_modules\@lucasygu\redbook\dist\cli.js"
$CdpPort = 9333

Write-Host "XHS Picker diagnostic (no cookie values are printed)" -ForegroundColor Cyan
Write-Host "Node: $(& node --version)"
Write-Host "Python: $(& python --version)"
Write-Host "Dedicated Chrome profile exists: $(Test-Path $DebugProfile)"
try {
    $null = Invoke-RestMethod -Uri "http://127.0.0.1:$CdpPort/json/version" -TimeoutSec 2
    Write-Host "Dedicated Chrome CDP reachable: True"
} catch {
    Write-Host "Dedicated Chrome CDP reachable: False"
}
Write-Host "Cookie file exists: $(Test-Path $CookieFile)"
if (Test-Path $CookieFile) {
    try {
        $obj = Get-Content -Raw $CookieFile | ConvertFrom-Json
        $keys = @($obj.cookies.PSObject.Properties.Name)
        Write-Host "Cookie key count: $($keys.Count)"
        Write-Host "Has a1: $($keys -contains 'a1')"
        Write-Host "Has web_session: $($keys -contains 'web_session')"
        Write-Host "Has webId: $($keys -contains 'webId')"
        Write-Host "Saved platform: $($obj.platform)"
    } catch {
        Write-Host "Cookie file parse error: $($_.Exception.Message)"
    }
}
if (Test-Path $CliJs) {
    $oldCookieFile = $env:REDBOOK_COOKIE_FILE
    $oldPlatform = $env:REDBOOK_PLATFORM
    try {
        $env:REDBOOK_COOKIE_FILE = $CookieFile
        $env:REDBOOK_PLATFORM = "xhs"
        Write-Host ""
        Write-Host "redbook whoami result:"
        & node $CliJs whoami
        Write-Host "Exit code: $LASTEXITCODE"
    } finally {
        if ($null -eq $oldCookieFile) { Remove-Item Env:REDBOOK_COOKIE_FILE -ErrorAction SilentlyContinue } else { $env:REDBOOK_COOKIE_FILE = $oldCookieFile }
        if ($null -eq $oldPlatform) { Remove-Item Env:REDBOOK_PLATFORM -ErrorAction SilentlyContinue } else { $env:REDBOOK_PLATFORM = $oldPlatform }
    }
}
