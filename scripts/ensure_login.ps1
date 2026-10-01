$ErrorActionPreference = "Stop"

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

function Wait-Cdp([int]$Port) {
    for ($i = 0; $i -lt 30; $i++) {
        Start-Sleep -Milliseconds 400
        if (Test-Cdp $Port) { return $true }
    }
    return $false
}

function Invoke-NodeCaptured([string[]]$Arguments) {
    # Windows PowerShell 5.1 turns native stderr lines into ErrorRecord objects.
    # redbook writes harmless status text (for example "Using saved cookie file")
    # to stderr, so running it under the script-wide ErrorActionPreference=Stop
    # can abort an otherwise successful login check. Capture both streams while
    # temporarily using Continue, then decide success from the native exit code.
    $oldPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        $output = @(& node @Arguments 2>&1)
        $exitCode = $LASTEXITCODE
        return [PSCustomObject]@{
            ExitCode = $exitCode
            Output = $output
        }
    }
    catch {
        return [PSCustomObject]@{
            ExitCode = 1
            Output = @($_.Exception.Message)
        }
    }
    finally {
        $ErrorActionPreference = $oldPreference
    }
}

function Test-Session([string]$CliJs, [string]$CookieFile) {
    if (-not (Test-Path $CookieFile)) { return $false }
    $oldCookieFile = $env:REDBOOK_COOKIE_FILE
    $oldPlatform = $env:REDBOOK_PLATFORM
    try {
        $env:REDBOOK_COOKIE_FILE = $CookieFile
        $env:REDBOOK_PLATFORM = "xhs"
        $result = Invoke-NodeCaptured @($CliJs, "whoami", "--json")
        return ($result.ExitCode -eq 0)
    }
    finally {
        if ($null -eq $oldCookieFile) { Remove-Item Env:REDBOOK_COOKIE_FILE -ErrorAction SilentlyContinue } else { $env:REDBOOK_COOKIE_FILE = $oldCookieFile }
        if ($null -eq $oldPlatform) { Remove-Item Env:REDBOOK_PLATFORM -ErrorAction SilentlyContinue } else { $env:REDBOOK_PLATFORM = $oldPlatform }
    }
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

function Export-Cookies([int]$Port, [string]$Exporter, [string]$CookieFile) {
    $result = Invoke-NodeCaptured @($Exporter, [string]$Port, $CookieFile)
    $ok = ($result.ExitCode -eq 0)
    if ($ok) {
        Protect-CookieFile $CookieFile
    }
    else {
        foreach ($line in $result.Output) {
            if ($line) { Write-Host ([string]$line) -ForegroundColor Red }
        }
    }
    return $ok
}

function Start-DedicatedChrome([string]$Chrome, [string]$ProfileDir, [int]$Port, [bool]$Headless) {
    New-Item -ItemType Directory -Force -Path $ProfileDir | Out-Null
    $headlessArg = if ($Headless) { " --headless=new --disable-gpu" } else { "" }
    $target = if ($Headless) { "about:blank" } else { "https://www.xiaohongshu.com/explore" }
    $argLine = "--remote-debugging-address=127.0.0.1 --remote-debugging-port=$Port --remote-allow-origins=http://127.0.0.1:$Port,http://localhost:$Port --user-data-dir=`"$ProfileDir`" --no-first-run --no-default-browser-check$headlessArg `"$target`""
    return Start-Process -FilePath $Chrome -ArgumentList $argLine -PassThru
}

function Stop-ProcessTree($Process) {
    if ($null -eq $Process) { return }
    try {
        & taskkill /PID $Process.Id /T /F 2>$null | Out-Null
    }
    catch {
        try { Stop-Process -Id $Process.Id -Force -ErrorAction SilentlyContinue } catch {}
    }
    Start-Sleep -Milliseconds 800
}

$Root = Split-Path -Parent $PSScriptRoot
$Runtime = Join-Path $Root ".runtime"
$CookieFile = Join-Path $Root ".redbook-cookies.json"
$ProfileDir = Join-Path $Root ".chrome-debug-profile"
$CliJs = Join-Path $Runtime "node_modules\@lucasygu\redbook\dist\cli.js"
$Exporter = Join-Path $PSScriptRoot "export_cookies_cdp.mjs"
$VisiblePort = 9333
$HeadlessPort = 9334

if (-not (Test-Path $CliJs)) {
    Write-Host "Local redbook runtime is missing. Run setup.bat first." -ForegroundColor Red
    exit 2
}
if (Test-Session $CliJs $CookieFile) {
    Write-Host "Xiaohongshu session is ready." -ForegroundColor Green
    exit 0
}

Write-Host "Saved API session is unavailable. Trying automatic recovery..." -ForegroundColor Yellow
$Chrome = Find-Chrome

# Reuse an already-running dedicated browser first.
if (Test-Cdp $VisiblePort) {
    if ((Export-Cookies $VisiblePort $Exporter $CookieFile) -and (Test-Session $CliJs $CookieFile)) {
        Write-Host "Session refreshed from the running dedicated Chrome." -ForegroundColor Green
        exit 0
    }
    Write-Host "The dedicated Chrome is running but not authenticated." -ForegroundColor Yellow
    Write-Host "Log in to Xiaohongshu in that dedicated Chrome window, then press Enter here."
    Read-Host "Press Enter after login" | Out-Null
    if ((Export-Cookies $VisiblePort $Exporter $CookieFile) -and (Test-Session $CliJs $CookieFile)) {
        Write-Host "Login restored." -ForegroundColor Green
        exit 0
    }
    Write-Host "Login is still unavailable. Run login.bat if you need to retry." -ForegroundColor Red
    exit 3
}

# Silent recovery: launch the persisted dedicated profile headlessly, capture fresh
# cookies, verify, and close it without asking the user to do anything.
$headless = Start-DedicatedChrome $Chrome $ProfileDir $HeadlessPort $true
if (Wait-Cdp $HeadlessPort) {
    $exported = Export-Cookies $HeadlessPort $Exporter $CookieFile
    $verified = $false
    if ($exported) { $verified = Test-Session $CliJs $CookieFile }
    Stop-ProcessTree $headless
    if ($verified) {
        Write-Host "Session automatically refreshed from the dedicated Chrome profile." -ForegroundColor Green
        exit 0
    }
}
else {
    Stop-ProcessTree $headless
}

# The profile itself is no longer authenticated. Only now ask for a real login.
Write-Host "The dedicated Chrome profile also needs login." -ForegroundColor Yellow
$visible = Start-DedicatedChrome $Chrome $ProfileDir $VisiblePort $false
if (-not (Wait-Cdp $VisiblePort)) {
    Write-Host "Dedicated Chrome did not start correctly." -ForegroundColor Red
    exit 4
}
Write-Host "A dedicated Chrome window is open. Log in to Xiaohongshu there." -ForegroundColor Cyan
Read-Host "Press Enter after login is complete" | Out-Null
if ((Export-Cookies $VisiblePort $Exporter $CookieFile) -and (Test-Session $CliJs $CookieFile)) {
    Write-Host "Login restored. You may close the dedicated Chrome window." -ForegroundColor Green
    exit 0
}
Write-Host "Login verification failed. Run login.bat to retry." -ForegroundColor Red
exit 5
