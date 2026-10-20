param(
    [int]$Port = 5000,
    [int]$CapturePort = 18080,
    [ValidateSet("browser", "mitmproxy")][string]$OpenIdSource = "browser"
)
$ErrorActionPreference = "Stop"
$rootDir = $PSScriptRoot
$logDir = Join-Path $rootDir "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$env:WECHAT_OPENID_METHOD = "cv"
$env:WECHAT_CV_OPENID_SOURCE = $OpenIdSource
$env:WECHAT_CV_PROXY_PORT = "$CapturePort"
$env:WECHAT_CV_MITM_TIMEOUT = "45"
$env:WECHAT_CV_MITM_RESULT_PATH = Join-Path $logDir "mitm_openid_result.txt"
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUNBUFFERED = "1"
if ($env:WECHAT_CV_OPENID_SOURCE -eq "browser") {
    & (Join-Path $rootDir "start_web_app.ps1") -OpenIdMethod cv -Port $Port
    exit $LASTEXITCODE
}
$mitmExe = Join-Path $rootDir ".venv-mitm\Scripts\mitmdump.exe"
if (-not (Test-Path -LiteralPath $mitmExe)) { throw "Install .venv-mitm and mitmproxy first (see README.md)." }
$listener = Get-NetTCPConnection -State Listen -LocalPort $CapturePort -ErrorAction SilentlyContinue
if ($listener) {
    $owner = Get-CimInstance Win32_Process -Filter "ProcessId = $($listener[0].OwningProcess)"
    if ($owner.ExecutablePath -ne $mitmExe) { throw "Capture port $CapturePort is occupied by another application." }
} else {
    $arguments = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$(Join-Path $rootDir 'start_mitmproxy_openid.ps1')`"", "-ListenPort", "$CapturePort")
    $proxyProcess = Start-Process powershell.exe -ArgumentList $arguments -WorkingDirectory $rootDir -WindowStyle Hidden -RedirectStandardOutput (Join-Path $logDir "mitmproxy.stdout.log") -RedirectStandardError (Join-Path $logDir "mitmproxy.stderr.log") -PassThru
    Set-Content (Join-Path $logDir "mitmproxy.pid") $proxyProcess.Id
    $ready = $false
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        Start-Sleep -Milliseconds 500
        if ($proxyProcess.HasExited) { throw "Capture proxy failed; see logs/mitmproxy.stderr.log." }
        if (Get-NetTCPConnection -State Listen -LocalPort $CapturePort -ErrorAction SilentlyContinue) { $ready = $true; break }
    }
    if (-not $ready) { throw "Capture proxy startup timed out; see logs/mitmproxy.stderr.log." }
}
& (Join-Path $rootDir "start_web_app.ps1") -OpenIdMethod cv -Port $Port
