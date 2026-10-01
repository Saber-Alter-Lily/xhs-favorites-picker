$ErrorActionPreference = "Stop"

function Invoke-NodeCaptured([string[]]$Arguments) {
    $oldPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        $output = @(& node @Arguments 2>&1)
        $exitCode = $LASTEXITCODE
        return [PSCustomObject]@{ ExitCode = $exitCode; Output = $output }
    }
    catch {
        return [PSCustomObject]@{ ExitCode = 1; Output = @($_.Exception.Message) }
    }
    finally {
        $ErrorActionPreference = $oldPreference
    }
}

function Find-Chrome {
    $candidates = @(
        "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
        "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
        "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe"
    )
    foreach ($path in $candidates) {
        if ($path -and (Test-Path $path)) { return $path }
    }
    throw "Google Chrome was not found."
}

function Test-Cdp([int]$Port) {
    try {
        $null = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/json/version" -TimeoutSec 2
        return $true
    }
    catch { return $false }
}

function Start-DedicatedChrome([string]$Chrome, [string]$ProfileDir, [int]$Port) {
    if (Test-Cdp $Port) { return }
    New-Item -ItemType Directory -Force -Path $ProfileDir | Out-Null
    $argLine = "--remote-debugging-address=127.0.0.1 --remote-debugging-port=$Port --remote-allow-origins=http://127.0.0.1:$Port,http://localhost:$Port --user-data-dir=`"$ProfileDir`" --no-first-run --no-default-browser-check `"https://www.xiaohongshu.com/explore`""
    Start-Process -FilePath $Chrome -ArgumentList $argLine | Out-Null
    for ($i = 0; $i -lt 30; $i++) {
        Start-Sleep -Milliseconds 500
        if (Test-Cdp $Port) { return }
    }
    throw "Dedicated Chrome did not expose its debugging port."
}

function Protect-CookieFile([string]$Path) {
    if (-not (Test-Path $Path)) { return }
    try {
        $identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
        & icacls $Path /inheritance:r /grant:r "$($identity):(F)" "SYSTEM:(F)" "*S-1-5-32-544:(F)" 2>$null | Out-Null
    }
    catch {
        Write-Warning "Could not tighten the cookie file ACL. Keep the project folder private."
    }
}

$Root = Split-Path -Parent $PSScriptRoot
$Runtime = Join-Path $Root ".runtime"
$CookieFile = Join-Path $Root ".redbook-cookies.json"
$DebugProfile = Join-Path $Root ".chrome-debug-profile"
$CliJs = Join-Path $Runtime "node_modules\@lucasygu\redbook\dist\cli.js"
$Exporter = Join-Path $PSScriptRoot "export_cookies_cdp.mjs"
$CdpPort = 9333

if (-not (Test-Path $CliJs)) {
    Write-Host "Local redbook runtime is missing. Run setup.bat first." -ForegroundColor Red
    exit 2
}

$Chrome = Find-Chrome
Start-DedicatedChrome $Chrome $DebugProfile $CdpPort
Write-Host "A dedicated Chrome window is open." -ForegroundColor Cyan
Write-Host "Log in to Xiaohongshu there (or confirm it is still logged in), then press Enter here."
Read-Host "Press Enter when ready" | Out-Null

$exportResult = Invoke-NodeCaptured @($Exporter, [string]$CdpPort, $CookieFile)
$exportExit = $exportResult.ExitCode
foreach ($line in $exportResult.Output) {
    if ($line) { Write-Host ([string]$line) }
}
if ($exportExit -eq 0) { Protect-CookieFile $CookieFile }
if ($exportExit -ne 0) {
    Write-Host "Cookie capture failed. Keep the dedicated Chrome window open and verify login." -ForegroundColor Red
    exit 3
}

$oldCookieFile = $env:REDBOOK_COOKIE_FILE
$oldPlatform = $env:REDBOOK_PLATFORM
try {
    $env:REDBOOK_COOKIE_FILE = $CookieFile
    $env:REDBOOK_PLATFORM = "xhs"
    $whoami = Invoke-NodeCaptured @($CliJs, "whoami", "--json")
    if ($whoami.ExitCode -ne 0) { exit 4 }
}
finally {
    if ($null -eq $oldCookieFile) { Remove-Item Env:REDBOOK_COOKIE_FILE -ErrorAction SilentlyContinue } else { $env:REDBOOK_COOKIE_FILE = $oldCookieFile }
    if ($null -eq $oldPlatform) { Remove-Item Env:REDBOOK_PLATFORM -ErrorAction SilentlyContinue } else { $env:REDBOOK_PLATFORM = $oldPlatform }
}
Write-Host "Login verified." -ForegroundColor Green
exit 0
