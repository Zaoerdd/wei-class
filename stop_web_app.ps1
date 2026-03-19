param(
    [int]$Port = 5000
)

$ErrorActionPreference = "Stop"

$RootDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$LogDir = Join-Path $RootDir "logs"
$PidPath = Join-Path $LogDir "web.pid"

Set-Location $RootDir

function Stop-ProcessSafe {
    param([int]$ProcessId)

    $process = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
    if ($process) {
        Stop-Process -Id $ProcessId -Force
        Write-Host "Stopped process PID=$ProcessId" -ForegroundColor Green
    }
}

$stopped = $false

if (Test-Path $PidPath) {
    $pidValue = (Get-Content $PidPath -Raw).Trim()
    if ($pidValue -match '^\d+$') {
        Stop-ProcessSafe -ProcessId ([int]$pidValue)
        $stopped = $true
    }
    Remove-Item $PidPath -Force -ErrorAction SilentlyContinue
}

$pythonProcesses = Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
    Where-Object {
        $_.CommandLine -like "*web.py*" -and $_.CommandLine -like "*--port $Port*"
    }

foreach ($process in $pythonProcesses) {
    Stop-ProcessSafe -ProcessId $process.ProcessId
    $stopped = $true
}

if (-not $stopped) {
    Write-Host "No running local service was found." -ForegroundColor Yellow
}
