param(
    [string]$BindHost = "127.0.0.1",
    [int]$Port = 5000,
    [string]$OpenIdMethod = ""
)

$ErrorActionPreference = "Stop"

$RootDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$LogDir = Join-Path $RootDir "logs"
$PidPath = Join-Path $LogDir "web.pid"
$RequirementsPath = Join-Path $RootDir "requirements.txt"
$ServerUrl = "http://$BindHost`:$Port/"
$HealthUrl = "http://$BindHost`:$Port/api/openid_status"

Set-Location $RootDir
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

function Write-Step {
    param([string]$Message)
    Write-Host "[wei-class] $Message" -ForegroundColor Cyan
}

function Resolve-BasePython {
    $pyLauncher = Get-Command py -ErrorAction SilentlyContinue
    if ($pyLauncher) {
        return @{
            FilePath = $pyLauncher.Source
            PrefixArgs = @("-3")
            Label = "py -3"
        }
    }

    $python = Get-Command python -ErrorAction SilentlyContinue
    if ($python) {
        return @{
            FilePath = $python.Source
            PrefixArgs = @()
            Label = $python.Source
        }
    }

    throw "Python 3 was not found. Please install Python 3 and enable Add Python to PATH."
}

function Invoke-PythonCommand {
    param(
        [Parameter(Mandatory = $true)]
        [string]$FilePath,
        [string[]]$PrefixArgs = @(),
        [Parameter(Mandatory = $true)]
        [string[]]$Args,
        [string]$WorkingDirectory = $RootDir
    )

    $allArgs = @($PrefixArgs) + @($Args)
    & $FilePath @allArgs
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed: $FilePath $($allArgs -join ' ')"
    }
}

function Test-ServiceAvailable {
    param([string]$Url)
    try {
        $null = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 2
        return $true
    } catch {
        return $false
    }
}

function Ensure-VenvPython {
    $venvPython = Join-Path $RootDir ".venv\Scripts\python.exe"
    if (Test-Path $venvPython) {
        return $venvPython
    }

    $basePython = Resolve-BasePython
    Write-Step "Creating the virtual environment..."
    Invoke-PythonCommand -FilePath $basePython.FilePath -PrefixArgs $basePython.PrefixArgs -Args @("-m", "venv", ".venv")

    if (-not (Test-Path $venvPython)) {
        throw "Failed to create the virtual environment. Please check the local Python installation."
    }

    return $venvPython
}

function Get-RequirementsHash {
    if (-not (Test-Path $RequirementsPath)) {
        throw "requirements.txt was not found."
    }
    return (Get-FileHash -Path $RequirementsPath -Algorithm SHA256).Hash
}

function Test-DependenciesReady {
    param([string]$PythonPath)

    & $PythonPath -c "import flask, requests, websockets, uiautomation, pyautogui, cv2"
    return $LASTEXITCODE -eq 0
}

function Ensure-Dependencies {
    param([string]$PythonPath)

    $stampPath = Join-Path $LogDir "requirements.sha256"
    $currentHash = Get-RequirementsHash
    $stampHash = if (Test-Path $stampPath) { (Get-Content $stampPath -Raw).Trim() } else { "" }
    $depsReady = Test-DependenciesReady -PythonPath $PythonPath

    if ($depsReady -and $stampHash -eq $currentHash) {
        Write-Step "Dependencies are already installed. Skipping pip install."
        return
    }

    Write-Step "Installing or updating dependencies. This can take a few minutes..."
    Invoke-PythonCommand -FilePath $PythonPath -Args @("-m", "pip", "install", "--disable-pip-version-check", "-r", "requirements.txt")
    Set-Content -Path $stampPath -Value $currentHash -Encoding UTF8
}

function Remove-StalePidFile {
    if (-not (Test-Path $PidPath)) {
        return
    }

    $pidValue = (Get-Content $PidPath -Raw).Trim()
    if (-not $pidValue) {
        Remove-Item $PidPath -Force -ErrorAction SilentlyContinue
        return
    }

    if ($pidValue -notmatch '^\d+$') {
        Remove-Item $PidPath -Force -ErrorAction SilentlyContinue
        return
    }

    $process = Get-Process -Id ([int]$pidValue) -ErrorAction SilentlyContinue
    if (-not $process) {
        Remove-Item $PidPath -Force -ErrorAction SilentlyContinue
    }
}

function Start-ServerProcess {
    param([string]$PythonPath)

    $argumentList = @("web.py", "--host", $BindHost, "--port", "$Port")
    if (-not [string]::IsNullOrWhiteSpace($OpenIdMethod)) {
        $argumentList += @("--openid-method", $OpenIdMethod)
    }

    $process = Start-Process -FilePath $PythonPath -ArgumentList $argumentList -WorkingDirectory $RootDir -PassThru
    Set-Content -Path $PidPath -Value $process.Id -Encoding UTF8
    return $process
}

Remove-StalePidFile

if (Test-ServiceAvailable -Url $HealthUrl) {
    Write-Step "The service is already running. Opening the page directly."
    Start-Process $ServerUrl | Out-Null
    exit 0
}

$venvPython = Ensure-VenvPython
Ensure-Dependencies -PythonPath $venvPython

Write-Step "Starting the local web service..."
$serverProcess = Start-ServerProcess -PythonPath $venvPython

$ready = $false
for ($index = 0; $index -lt 40; $index++) {
    Start-Sleep -Milliseconds 750

    $processAlive = Get-Process -Id $serverProcess.Id -ErrorAction SilentlyContinue
    if (-not $processAlive) {
        break
    }

    if (Test-ServiceAvailable -Url $HealthUrl) {
        $ready = $true
        break
    }
}

if (-not $ready) {
    throw "The service failed to start. Check the server window output, then run stop_web_app.ps1 before trying again."
}

Write-Step "Service is ready. Opening the browser..."
Start-Process $ServerUrl | Out-Null
Write-Host ""
Write-Host "The app is ready in your browser:" -ForegroundColor Green
Write-Host "  $ServerUrl" -ForegroundColor Green
Write-Host ""
Write-Host "To stop it later, double-click stop_web_app.ps1 or the stop batch file." -ForegroundColor Yellow
