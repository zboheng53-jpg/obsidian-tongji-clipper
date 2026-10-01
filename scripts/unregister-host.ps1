<#
.SYNOPSIS
    注销 Chrome 与 Edge 浏览器的 Native Messaging Host (com.tongji.clipper)
#>

[CmdletBinding()]
param (
    [switch]$DryRun
)

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$manifestPath = Join-Path $scriptDir "com.tongji.clipper.json"

$targets = @(
    @{ Name = "Google Chrome"; Key = "HKCU:\Software\Google\Chrome\NativeMessagingHosts\com.tongji.clipper" },
    @{ Name = "Microsoft Edge"; Key = "HKCU:\Software\Microsoft\Edge\NativeMessagingHosts\com.tongji.clipper" }
)

foreach ($target in $targets) {
    Write-Host "`n检查: $($target.Name) -> $($target.Key)"
    if (Test-Path $target.Key) {
        $existingVal = (Get-ItemProperty -Path $target.Key -Name "(Default)" -ErrorAction SilentlyContinue)."(Default)"
        if ($existingVal -ne $manifestPath) {
            Write-Host "拒绝删除：该注册表项并不指向本项目 ($existingVal)，操作已跳过。" -ForegroundColor Red
            continue
        }

        if ($DryRun) {
            Write-Host "[-DryRun 模式] 发现属于本项目的注册表项，未执行删除。" -ForegroundColor Yellow
        } else {
            Remove-Item -Path $target.Key -Recurse -Force
            Write-Host "已安全删除注册表项。" -ForegroundColor Green
        }
    } else {
        Write-Host "注册表项不存在，无需注销。" -ForegroundColor Gray
    }
}
