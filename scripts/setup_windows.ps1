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

function Invoke-NativePassthrough([string]$Command, [string[]]$Arguments) {
    $oldPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        $output = @(& $Command @Arguments 2>&1)
        $exitCode = $LASTEXITCODE
        foreach ($line in $output) {
            if ($line) { Write-Host ([string]$line) }
        }
        return $exitCode
    }
    finally {
        $ErrorActionPreference = $oldPreference
    }
}

function Require-Command([string]$Name, [string]$Hint) {
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "Missing required command '$Name'. $Hint"
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
    throw "Google Chrome was not found in the standard install locations."
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

Require-Command "python" "Install Python 3.10 or newer first."
Require-Command "node" "Install Node.js 22 or newer first."
Require-Command "npm" "npm should be installed with Node.js."

$pythonVersion = (& python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')").Trim()
$pythonParts = $pythonVersion.Split('.')
if ([int]$pythonParts[0] -lt 3 -or ([int]$pythonParts[0] -eq 3 -and [int]$pythonParts[1] -lt 10)) {
    throw "Python 3.10+ is required. Detected $pythonVersion."
}

$nodeMajor = (& node -p "process.versions.node.split('.')[0]").Trim()
if ([int]$nodeMajor -lt 22) {
    throw "Node.js 22+ is required. Detected major version $nodeMajor."
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

Write-Host "Installing project-local redbook CLI (0.8.2)..."
$npmExit = Invoke-NativePassthrough "npm" @("install", "--prefix", $Runtime, "@lucasygu/redbook@0.8.2")
if ($npmExit -ne 0) { throw "redbook installation failed." }

$Chrome = Find-Chrome
Write-Host ""
Write-Host "Opening a dedicated Chrome profile for this tool..." -ForegroundColor Cyan
Write-Host "This profile is separate from your normal Chrome profile." -ForegroundColor Yellow
Start-DedicatedChrome $Chrome $DebugProfile $CdpPort

Write-Host ""
Write-Host "In the newly opened Chrome window:" -ForegroundColor Cyan
Write-Host "  1. Log in to Xiaohongshu normally."
Write-Host "  2. Make sure the feed/explore page is visible after login."
Write-Host "  3. Return to this window and press Enter."
Read-Host "Press Enter after login is complete" | Out-Null

Write-Host ""
Write-Host "Capturing the complete Xiaohongshu cookie set from the dedicated browser..."
$exportResult = Invoke-NodeCaptured @($Exporter, [string]$CdpPort, $CookieFile)
$exportExit = $exportResult.ExitCode
foreach ($line in $exportResult.Output) {
    if ($line) { Write-Host ([string]$line) }
}
if ($exportExit -eq 0) { Protect-CookieFile $CookieFile }
if ($exportExit -ne 0) {
    Write-Host "Cookie capture failed. Keep the dedicated Chrome window open, finish login, and run login.bat." -ForegroundColor Red
    exit 4
}

Write-Host ""
Write-Host "Verifying the captured session..."
$oldCookieFile = $env:REDBOOK_COOKIE_FILE
$oldPlatform = $env:REDBOOK_PLATFORM
try {
    $env:REDBOOK_COOKIE_FILE = $CookieFile
    $env:REDBOOK_PLATFORM = "xhs"
    $whoami = Invoke-NodeCaptured @($CliJs, "whoami", "--json")
    $loginOk = ($whoami.ExitCode -eq 0)
}
finally {
    if ($null -eq $oldCookieFile) { Remove-Item Env:REDBOOK_COOKIE_FILE -ErrorAction SilentlyContinue } else { $env:REDBOOK_COOKIE_FILE = $oldCookieFile }
    if ($null -eq $oldPlatform) { Remove-Item Env:REDBOOK_PLATFORM -ErrorAction SilentlyContinue } else { $env:REDBOOK_PLATFORM = $oldPlatform }
}

if (-not $loginOk) {
    Write-Host ""
    Write-Host "The dedicated browser cookie capture succeeded, but redbook /user/me was rejected." -ForegroundColor Red
    Write-Host "Run diagnostic.bat and share only its non-secret output." -ForegroundColor Yellow
    exit 5
}

Write-Host ""
Write-Host "Login verified." -ForegroundColor Green
Write-Host "Setup complete. Run: .\run.bat"
exit 0
