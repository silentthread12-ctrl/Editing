$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$root = Split-Path -Parent $PSScriptRoot
$bin = Join-Path $root 'bin'
New-Item -ItemType Directory -Force $bin | Out-Null
$tmp = Join-Path $env:TEMP 'shorts_ffmpeg_dl'
Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
New-Item -ItemType Directory $tmp | Out-Null
$urls = @(
  'https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip',
  'https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip'
)
foreach ($u in $urls) {
  try {
    Write-Host "  Downloading $u"
    $zip = Join-Path $tmp 'ff.zip'
    Invoke-WebRequest -Uri $u -OutFile $zip -UseBasicParsing
    Expand-Archive $zip -DestinationPath (Join-Path $tmp 'x') -Force
    $ff = Get-ChildItem -Recurse (Join-Path $tmp 'x') -Filter ffmpeg.exe | Select-Object -First 1
    $fp = Get-ChildItem -Recurse (Join-Path $tmp 'x') -Filter ffprobe.exe | Select-Object -First 1
    Copy-Item $ff.FullName $bin -Force
    Copy-Item $fp.FullName $bin -Force
    break
  } catch {
    Write-Host "  That source failed: $_"
  }
}
Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
if (-not (Test-Path (Join-Path $bin 'ffmpeg.exe'))) { exit 1 }
