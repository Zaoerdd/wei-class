param([int]$Port = 5000, [int]$CapturePort = 18080)
$ErrorActionPreference = "Stop"
$rootDir = $PSScriptRoot
$proxySettings = Get-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings'
if ($proxySettings.ProxyEnable -and $proxySettings.ProxyServer -eq "127.0.0.1:$CapturePort") {
    throw "Capture proxy is in use. Wait for collection and proxy restoration before stopping."
}
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
Write-Host "wei-class stopped."
