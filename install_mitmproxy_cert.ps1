param(
    [string]$CertPath = "$env:USERPROFILE\.mitmproxy\mitmproxy-ca-cert.cer"
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path $CertPath)) {
    throw "mitmproxy certificate not found: $CertPath"
}

$cert = New-Object System.Security.Cryptography.X509Certificates.X509Certificate2($CertPath)
$existing = Get-ChildItem Cert:\CurrentUser\Root |
    Where-Object { $_.Thumbprint -eq $cert.Thumbprint }

if ($existing) {
    Write-Host "mitmproxy certificate already trusted in CurrentUser\\Root"
    $existing | Select-Object Subject, Thumbprint, NotAfter
    exit 0
}

Import-Certificate -FilePath $CertPath -CertStoreLocation "Cert:\CurrentUser\Root" |
    Select-Object Subject, Thumbprint, PSParentPath
