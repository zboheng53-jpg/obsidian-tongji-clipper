$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Venv = Join-Path $Root ".venv"

if (Get-Command py -ErrorAction SilentlyContinue) {
    & py -3 -m venv $Venv
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    & python -m venv $Venv
} else {
    throw "Python 3 was not found. Install Python 3.11 or newer first."
}

$Python = Join-Path $Venv "Scripts\python.exe"
& $Python -m pip install --upgrade pip
& $Python -m pip install -r (Join-Path $Root "requirements.txt")

$Config = Join-Path $Root "config.json"
if (-not (Test-Path -LiteralPath $Config)) {
    Copy-Item -LiteralPath (Join-Path $Root "config.example.json") -Destination $Config
}

& (Join-Path $Root "enable-autostart.ps1")

function Test-ClipperService {
    try {
        $Health = Invoke-RestMethod -Uri "http://127.0.0.1:8765/health" -TimeoutSec 1
        return $Health.app -eq "Tongji Course Clipper"
    } catch {
        return $false
    }
}

if (-not (Test-ClipperService)) {
    $StartScript = Join-Path $Root "start.ps1"
    Start-Process -FilePath "powershell.exe" `
        -ArgumentList @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$StartScript`"") `
        -WorkingDirectory $Root `
        -WindowStyle Hidden
}

$Ready = $false
for ($Attempt = 0; $Attempt -lt 20; $Attempt++) {
    if (Test-ClipperService) {
        $Ready = $true
        break
    }
    Start-Sleep -Milliseconds 500
}

if (-not $Ready) {
    throw "Installation finished, but the local service did not start. Run start.ps1 to see the error."
}

Write-Host "Installation complete."
Write-Host "Open the browser extension settings and choose your Obsidian Vault."
