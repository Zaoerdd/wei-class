param(
    [string]$ListenHost = "127.0.0.1",
    [int]$ListenPort = 8080,
    [string]$MitmDumpPath = "$PSScriptRoot\.venv-mitm\Scripts\mitmdump.exe",
    [string]$AddonPath = "$PSScriptRoot\mitmproxy_openid_addon.py",
    [string]$OutputPath = "$PSScriptRoot\logs\mitm_openid_result.txt",
    [string]$TargetDomain = "v18.teachermate.cn",
    [string]$UpstreamProxy = "",
    [switch]$NoUpstream
)

$ErrorActionPreference = "Stop"

function Get-SystemProxyServer {
    $settings = Get-ItemProperty -Path "HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings"
    if (-not $settings.ProxyEnable -or [string]::IsNullOrWhiteSpace($settings.ProxyServer)) {
        return $null
    }

    $proxyServer = $settings.ProxyServer.Trim()
    if ($proxyServer -notmatch "=") {
        return $proxyServer
    }

    foreach ($entry in $proxyServer -split ";") {
        $parts = $entry -split "=", 2
        if ($parts.Count -eq 2 -and $parts[1].Trim()) {
            return $parts[1].Trim()
        }
    }

    return $null
}

if (-not (Test-Path $MitmDumpPath)) {
    throw "mitmdump not found: $MitmDumpPath"
}

if (-not (Test-Path $AddonPath)) {
    throw "addon not found: $AddonPath"
}

$selectedUpstream = $UpstreamProxy
if (-not $NoUpstream -and [string]::IsNullOrWhiteSpace($selectedUpstream)) {
    $currentProxy = Get-SystemProxyServer
    $currentCaptureProxy = "$ListenHost`:$ListenPort"
    if ($currentProxy -and $currentProxy -ne $currentCaptureProxy) {
        $selectedUpstream = $currentProxy
    }
}

$env:WECHAT_MITM_OUTPUT_PATH = $OutputPath
$env:WECHAT_MITM_TARGET_DOMAIN = $TargetDomain

$arguments = @(
    "-s", $AddonPath,
    "--listen-host", $ListenHost,
    "-p", "$ListenPort"
)

if (-not [string]::IsNullOrWhiteSpace($selectedUpstream)) {
    $arguments += @("--mode", "upstream:http://$selectedUpstream")
    Write-Host "Using upstream proxy: $selectedUpstream"
} else {
    Write-Host "Using direct upstream connection"
}

Write-Host "openid output file: $OutputPath"
Write-Host "listen endpoint: $ListenHost`:$ListenPort"

& $MitmDumpPath @arguments
