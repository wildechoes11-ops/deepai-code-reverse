# launch-deepai-filtered.ps1 - run DeepAI Code against the local model filter.
#
# DeepAI Code reads its API origin from two env vars (src/deepai/api.ts):
#   T3CODE_DEEPAI_LOCAL_TEST=true
#   T3CODE_DEEPAI_DEV_API_ORIGIN=http://127.0.0.1:<port>
# The pair is only honoured when the origin is a canonical loopback URL, which
# is exactly what deepai-model-filter.py listens on.
#
# The app inherits the environment at process start, so it has to be closed
# first. This script does that, starts the proxy, then relaunches.
#
#   .\launch-deepai-filtered.ps1              # filter, restart app
#   .\launch-deepai-filtered.ps1 -Port 8899
#   .\launch-deepai-filtered.ps1 -NoRestart   # proxy only
#   .\launch-deepai-filtered.ps1 -Stop        # kill proxy, relaunch clean

[CmdletBinding()]
param(
    [int]$Port = 8788,
    [switch]$NoRestart,
    [switch]$Stop,
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$proxy = Join-Path $here 'deepai-model-filter.py'
$exe = if ($env:DEEPAI_CODE_EXE) { $env:DEEPAI_CODE_EXE } else { Join-Path $env:LOCALAPPDATA 'Programs\deepai-code-desktop\DeepAI Code.exe' }
$origin = "http://127.0.0.1:$Port"

function Get-ProxyProcess {
    Get-CimInstance Win32_Process -Filter "name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like '*deepai-model-filter.py*' }
}

function Stop-Proxy {
    Get-ProxyProcess | ForEach-Object {
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    }
}

function Stop-App {
    Get-Process -Name 'DeepAI Code' -ErrorAction SilentlyContinue |
        Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep -Milliseconds 800
}

function Start-App {
    if (-not (Test-Path $exe)) { throw "DeepAI Code not found at $exe" }
    $env:T3CODE_DEEPAI_LOCAL_TEST = 'true'
    $env:T3CODE_DEEPAI_DEV_API_ORIGIN = $origin
    Start-Process -FilePath $exe
}

if ($Stop) {
    Stop-Proxy
    Remove-Item Env:T3CODE_DEEPAI_LOCAL_TEST -ErrorAction SilentlyContinue
    Remove-Item Env:T3CODE_DEEPAI_DEV_API_ORIGIN -ErrorAction SilentlyContinue
    Write-Host '  proxy stopped, env cleared' -ForegroundColor Yellow
    if (-not $NoRestart) {
        Stop-App
        Start-Process -FilePath $exe
        Write-Host '  app relaunched against api.deepai.org' -ForegroundColor Green
    }
    return
}

if (-not (Test-Path $proxy)) { throw "missing $proxy" }

# port free?
$busy = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($busy) {
    $owner = (Get-Process -Id $busy[0].OwningProcess -ErrorAction SilentlyContinue).ProcessName
    if ($Force) {
        Stop-Proxy
        Start-Sleep -Milliseconds 500
    } else {
        throw "port $Port already in use by $owner - pass -Force to replace it"
    }
}

Stop-Proxy
Start-Process -FilePath 'python' `
    -ArgumentList '-u', "`"$proxy`"", '--port', $Port `
    -WorkingDirectory $here `
    -RedirectStandardOutput (Join-Path $here 'filter.out') `
    -RedirectStandardError (Join-Path $here 'filter.err') `
    -WindowStyle Hidden

Start-Sleep -Milliseconds 1200
if (-not (Get-ProxyProcess)) {
    Get-Content (Join-Path $here 'filter.err') -ErrorAction SilentlyContinue
    throw 'proxy did not start'
}
Write-Host "  proxy up   $origin  (10 free models only)" -ForegroundColor Green

if ($NoRestart) {
    Write-Host "  set these in the shell that launches the app:" -ForegroundColor Yellow
    Write-Host "    `$env:T3CODE_DEEPAI_LOCAL_TEST='true'"
    Write-Host "    `$env:T3CODE_DEEPAI_DEV_API_ORIGIN='$origin'"
    return
}

Stop-App
Write-Host '  app closed' -ForegroundColor DarkGray
Start-App
Write-Host "  app relaunched against $origin" -ForegroundColor Green
Write-Host '  undo with: .\launch-deepai-filtered.ps1 -Stop' -ForegroundColor DarkGray
