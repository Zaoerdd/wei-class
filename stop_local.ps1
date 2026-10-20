param([int]$Port = 5000, [int]$CapturePort = 18080)
$ErrorActionPreference = "Stop"
$rootDir = $PSScriptRoot
# Wait for collection to finish so its finally block can restore the system proxy.
try {
    $status = Invoke-RestMethod "http://127.0.0.1:$Port/api/openid_status" -TimeoutSec 3
    if ($status.is_refreshing -or $status.wechat_ui_busy) { throw "Collection is running. Wait until it finishes before stopping." }
} catch {
    if ($_.Exception.Message -like "Collection is running*") { throw }
}
$owned = Get-CimInstance Win32_Process | Where-Object {
    ($_.Name -eq "mitmdump.exe" -or ($_.Name -eq "python.exe" -and $_.CommandLine -match '\bweb\.py\b')) -and
    $_.ExecutablePath -and $_.ExecutablePath.StartsWith($rootDir + "\", [System.StringComparison]::OrdinalIgnoreCase)
}
foreach ($process in $owned) { Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue }
$pidPath = Join-Path $rootDir "logs\mitmproxy.pid"
if (Test-Path -LiteralPath $pidPath) {
    $processId = (Get-Content -LiteralPath $pidPath -Raw).Trim()
    if ($processId -match '^\d+$') {
        $wrapper = Get-CimInstance Win32_Process -Filter "ProcessId = $processId"
        if ($wrapper.Name -eq "powershell.exe" -and $wrapper.CommandLine.Contains((Join-Path $rootDir "start_mitmproxy_openid.ps1"))) { Stop-Process -Id $wrapper.ProcessId -Force -ErrorAction SilentlyContinue }
    }
}
foreach ($name in @("web.pid", "mitmproxy.pid")) {
    Remove-Item -LiteralPath (Join-Path $rootDir "logs\$name") -Force -ErrorAction SilentlyContinue
}
# A capture run killed mid-way leaves the system proxy on the stopped capture port; undo it from the snapshot.
$venvPython = Join-Path $rootDir ".venv\Scripts\python.exe"
if ((Test-Path -LiteralPath (Join-Path $rootDir "logs\proxy_snapshot.json")) -and (Test-Path -LiteralPath $venvPython)) {
    Push-Location $rootDir
    try {
        & $venvPython -c "import logging, sys; log = logging.getLogger('stop_local'); log.addHandler(logging.StreamHandler(sys.stdout)); log.setLevel(logging.INFO); import wechat_openid_strategy as s; s.recover_stale_capture_proxy(log)"
        if ($LASTEXITCODE -ne 0) { Write-Warning "Restoring the system proxy from logs\proxy_snapshot.json failed." }
    } finally {
        Pop-Location
    }
}
$proxySettings = Get-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings'
if ($proxySettings.ProxyEnable -and $proxySettings.ProxyServer -eq "127.0.0.1:$CapturePort") {
    Write-Warning "System proxy still points to the stopped capture proxy 127.0.0.1:$CapturePort. Turn it off in Windows Settings > Network > Proxy."
}
Write-Host "wei-class stopped."
