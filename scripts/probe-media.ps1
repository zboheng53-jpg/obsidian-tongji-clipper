param(
    [Parameter(Mandatory = $true)][string]$MediaUrl,
    [ValidateRange(0, 14400)][int]$StartSeconds = 120,
    [ValidateRange(1, 60)][int]$DurationSeconds = 30,
    [string]$OutputDirectory = (Join-Path $PSScriptRoot '..\artifacts\probe')
)

$ErrorActionPreference = 'Stop'
$mediaUri = [uri]$MediaUrl
if ($mediaUri.Scheme -ne 'https' -or $mediaUri.Host -ne 'look.tongji.edu.cn' -or
    -not $mediaUri.AbsolutePath.StartsWith('/play/') -or $mediaUri.UserInfo) {
    throw '仅接受当前授权课程的 https://look.tongji.edu.cn/play/ 媒体地址。'
}
foreach ($program in @('ffmpeg', 'ffprobe')) {
    if (-not (Get-Command $program -ErrorAction SilentlyContinue)) {
        throw "缺少 $program，请先从其官方渠道安装。"
    }
}
$outputRoot = [IO.Path]::GetFullPath($OutputDirectory)
New-Item -ItemType Directory -Force -Path $outputRoot | Out-Null
# 每次独立样本，避免覆盖其他验证资料；签名 URL 不写入文件。
$samplePath = Join-Path $outputRoot ("sample-{0}-{1}s-{2}.wav" -f $StartSeconds, $DurationSeconds, [guid]::NewGuid().ToString('N').Substring(0, 8))
$probeLog = & ffmpeg -hide_banner -loglevel error -nostdin -rw_timeout 15000000 `
    -ss $StartSeconds -i $MediaUrl -t $DurationSeconds -map 0:a:0 -vn -ac 1 -ar 16000 $samplePath 2>&1
$probeExit = $LASTEXITCODE
if ($probeExit -ne 0) {
    $safeLog = ($probeLog | ForEach-Object {
        [regex]::Replace($_.ToString(), 'https?://[^\s'']+', '[media URL redacted]')
    }) -join [Environment]::NewLine
    throw "媒体读取失败（$probeExit）；可重新打开回放取得有效签名。`n$safeLog"
}
$metadataText = & ffprobe -v error -show_entries format=duration,size:stream=codec_name,sample_rate,channels -of json $samplePath
if ($LASTEXITCODE -ne 0) { throw 'ffprobe 无法读取生成的音频。' }
$metadata = ($metadataText -join "`n") | ConvertFrom-Json
$stream = @($metadata.streams)[0]
$measuredDuration = [double]::Parse($metadata.format.duration, [Globalization.CultureInfo]::InvariantCulture)
if ($stream.codec_name -ne 'pcm_s16le' -or $stream.sample_rate -ne '16000' -or
    $stream.channels -ne 1 -or [Math]::Abs($measuredDuration - $DurationSeconds) -gt 0.25) {
    throw '生成音频未达到采样率、声道数或时长验收；样本保留供检查。'
}
$result = [ordered]@{
    captured_at = [DateTimeOffset]::Now.ToString('o')
    source_host = $mediaUri.Host
    signature_query_names = @(([System.Web.HttpUtility]::ParseQueryString($mediaUri.Query)).AllKeys)
    offset_seconds = $StartSeconds
    requested_duration_seconds = $DurationSeconds
    actual_duration_seconds = $measuredDuration
    codec = $stream.codec_name
    sample_rate = [int]$stream.sample_rate
    channels = $stream.channels
    size_bytes = [long]$metadata.format.size
    sample_path = $samplePath
    credential_mode = 'signed URL only; no Cookie or Authorization supplied'
    verification = 'passed'
}
$result | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $outputRoot 'latest-probe.json') -Encoding utf8
$result | ConvertTo-Json -Depth 5
