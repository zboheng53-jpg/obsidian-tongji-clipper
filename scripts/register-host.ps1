[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)]
    [ValidatePattern('^[a-p]{32}$')]
    [string]$ExtensionId,
    [switch]$DryRun
)
$ErrorActionPreference = 'Stop'
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$manifestPath = Join-Path $scriptDir 'com.tongji.clipper.json'
$targets = @(
    'HKCU:\Software\Google\Chrome\NativeMessagingHosts\com.tongji.clipper',
    'HKCU:\Software\Microsoft\Edge\NativeMessagingHosts\com.tongji.clipper'
)
# Check ownership of all keys before making any change.
foreach ($key in $targets) {
    if (Test-Path -LiteralPath $key) {
        $value = (Get-Item -LiteralPath $key).GetValue('')
        if ($value -and $value -ne $manifestPath) {
            throw "Existing host belongs to another installation: $key -> $value"
        }
    }
}
$manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding utf8 | ConvertFrom-Json
$projectDir = Split-Path -Parent $scriptDir
$hostPath = Join-Path $projectDir '.venv\Scripts\tongji-bridge.exe'
if (-not (Test-Path -LiteralPath $hostPath)) {
    throw 'Install the project into .venv first: uv pip install --python .venv\Scripts\python.exe -e ".[asr]"'
}
# A stale console entry point can recreate the Python window even with an .exe host.
$hostBytes = [System.IO.File]::ReadAllBytes($hostPath)
$peOffset = [System.BitConverter]::ToInt32($hostBytes, 0x3c)
if ($peOffset -lt 0 -or $peOffset + 94 -gt $hostBytes.Length -or [System.BitConverter]::ToUInt16($hostBytes, $peOffset + 92) -ne 2) {
    throw 'Host must be a GUI executable. Reinstall: uv pip install --python .venv\Scripts\python.exe -e ".[asr]"'
}
$manifest.path = $hostPath
$existing = @($manifest.allowed_origins | Where-Object { $_ -match '^chrome-extension://[a-p]{32}/$' })
$manifest.allowed_origins = @($existing + "chrome-extension://$ExtensionId/" | Select-Object -Unique)
$json = $manifest | ConvertTo-Json -Depth 5
if ($DryRun) {
    Write-Output $json
    Write-Output ('Would register current-user keys: ' + ($targets -join ', '))
    return
}
[System.IO.File]::WriteAllText($manifestPath, $json, (New-Object System.Text.UTF8Encoding($false)))
foreach ($key in $targets) {
    if (-not (Test-Path -LiteralPath $key)) { New-Item -Path $key -Force | Out-Null }
    Set-ItemProperty -LiteralPath $key -Name '(Default)' -Value $manifestPath
}
Write-Output 'Native host registered for this extension. Reload the course page.'
