# Rebuilds the custom RTX Remix binaries, runs the game-free checks, and stages them into pkg\.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File build_custom_remix.ps1
#
# Builds (release):
#   bridge x64 server  src\dxvk-remix\bridge\_Comp64Release\src\server\NvRemixBridge.exe -> pkg\.trex\NvRemixBridge.exe
#   bridge x86 client  src\dxvk-remix\bridge\_Comp32Release\src\client\d3d9.dll         -> pkg\d3d9.dll
#   dxvk-remix runtime src\dxvk-remix\_Comp64Release\src\d3d9\d3d9.dll                   -> pkg\.trex\d3d9.dll
# Then runs tests\remix_source\run_remix_source_tests.ps1 and, if they pass, stages pkg\ with the same
# layout as remix-1.5.2-release.zip: every other file is the stock file from that zip (restored if
# missing or changed in size), and the three files above are replaced by the custom builds.
[CmdletBinding()]
param(
    [ValidateSet('all', 'bridge', 'dxvk')]
    [string]$Target = 'all',
    [switch]$SkipTests,
    [switch]$NoStage
)

$ErrorActionPreference = 'Stop'
$remixRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$dxvkDir = Join-Path $remixRoot 'src\dxvk-remix'
$bridgeDir = Join-Path $dxvkDir 'bridge'
$pkgDir = Join-Path $remixRoot 'pkg'
$releaseZip = Join-Path $remixRoot 'remix-1.5.2-release.zip'

$vcvars = 'C:\Program Files (x86)\Microsoft Visual Studio\18\BuildTools\VC\Auxiliary\Build\vcvarsall.bat'
$ninjaDir = 'C:\Program Files (x86)\Microsoft Visual Studio\18\BuildTools\Common7\IDE\CommonExtensions\Microsoft\CMake\Ninja'
$vsInstallerDir = 'C:\Program Files (x86)\Microsoft Visual Studio\Installer'
$pyDir = 'C:\Python314'
$mesonScripts = 'C:\Users\Corey\AppData\Roaming\Python\Python314\Scripts'

foreach ($p in @($vcvars, "$ninjaDir\ninja.exe", "$pyDir\python.exe", "$mesonScripts\meson.exe", $releaseZip)) {
    if (-not (Test-Path -LiteralPath $p)) {
        throw "Required file or tool missing: $p"
    }
}

$extraPath = "$pyDir;$mesonScripts;$ninjaDir;$vsInstallerDir"

function Invoke-VcBuild {
    param(
        [string]$Arch,
        [string]$WorkDir,
        [string]$BuildDir,
        [string]$SetupArgs,
        [string]$NinjaTarget
    )
    $setup = if (Test-Path (Join-Path $WorkDir "$BuildDir\build.ninja")) { '' } else { "meson setup $SetupArgs $BuildDir && " }
    $fullCmd = "set `"PATH=$extraPath;%PATH%`" && call `"$vcvars`" $Arch >nul && cd /d `"$WorkDir`" && ${setup}ninja -C $BuildDir $NinjaTarget"
    & $env:ComSpec /d /s /c $fullCmd
    if ($LASTEXITCODE -ne 0) {
        throw "Build failed ($Arch in $WorkDir, exit $LASTEXITCODE): $NinjaTarget"
    }
}

$outputs = @(
    @{ Name = 'NvRemixBridge.exe (bridge x64 server)'; Src = "$bridgeDir\_Comp64Release\src\server\NvRemixBridge.exe"; Rel = '.trex/NvRemixBridge.exe' },
    @{ Name = 'd3d9.dll (bridge x86 client)';          Src = "$bridgeDir\_Comp32Release\src\client\d3d9.dll";         Rel = 'd3d9.dll' },
    @{ Name = '.trex\d3d9.dll (dxvk-remix runtime)';  Src = "$dxvkDir\_Comp64Release\src\d3d9\d3d9.dll";            Rel = '.trex/d3d9.dll' }
)

if ($Target -in @('all', 'bridge')) {
    Write-Host '=== Building bridge x64 server (NvRemixBridge.exe) ===' -ForegroundColor Cyan
    Invoke-VcBuild -Arch 'x64' -WorkDir $bridgeDir -BuildDir '_Comp64Release' -SetupArgs '--buildtype release --backend ninja' -NinjaTarget 'src/server/NvRemixBridge.exe src/util/libutil.a'

    Write-Host '=== Building bridge x86 client (d3d9.dll) ===' -ForegroundColor Cyan
    Invoke-VcBuild -Arch 'x86' -WorkDir $bridgeDir -BuildDir '_Comp32Release' -SetupArgs '--buildtype release --backend ninja' -NinjaTarget 'src/client/d3d9.dll'
}

if ($Target -in @('all', 'dxvk')) {
    Write-Host '=== Building dxvk-remix x64 runtime (.trex\d3d9.dll) ===' -ForegroundColor Cyan
    Invoke-VcBuild -Arch 'x64' -WorkDir $dxvkDir -BuildDir '_Comp64Release' -SetupArgs '--buildtype release --backend ninja -Denable_tracy=false' -NinjaTarget 'src/d3d9/d3d9.dll'
}

if (-not $SkipTests) {
    Write-Host '=== Running game-free checks ===' -ForegroundColor Cyan
    & (Join-Path $remixRoot 'tests\remix_source\run_remix_source_tests.ps1')
    if ($LASTEXITCODE -ne 0) {
        throw 'Checks failed; pkg\ was not changed.'
    }
}

if ($NoStage) {
    return
}

Write-Host '=== Staging pkg\ (release zip layout) ===' -ForegroundColor Cyan
foreach ($item in $outputs) {
    if (-not (Test-Path -LiteralPath $item.Src)) {
        throw "Build output missing, run with -Target all: $($item.Src)"
    }
}

Add-Type -AssemblyName System.IO.Compression.FileSystem
$custom = @{}
foreach ($item in $outputs) { $custom[$item.Rel] = $item }
$zip = [System.IO.Compression.ZipFile]::OpenRead($releaseZip)
try {
    foreach ($entry in $zip.Entries) {
        if ($entry.FullName.EndsWith('/')) { continue }
        $dst = Join-Path $pkgDir ($entry.FullName -replace '/', '\')
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $dst) | Out-Null
        if ($custom.ContainsKey($entry.FullName)) {
            Copy-Item -LiteralPath $custom[$entry.FullName].Src -Destination $dst -Force
            continue
        }
        $existing = Get-Item -LiteralPath $dst -ErrorAction SilentlyContinue
        if ($null -eq $existing -or $existing.Length -ne $entry.Length) {
            [System.IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $dst, $true)
            Write-Host "Restored stock $($entry.FullName)" -ForegroundColor Yellow
        }
    }
} finally {
    $zip.Dispose()
}

# Files left over from older staging runs (e.g. *.orig backups) are not part of the release layout.
Get-ChildItem -LiteralPath $pkgDir -Recurse -File -Filter '*.orig' | ForEach-Object {
    Remove-Item -LiteralPath $_.FullName -Force
    Write-Host "Removed $($_.FullName)" -ForegroundColor Yellow
}

foreach ($item in $outputs) {
    $dst = Join-Path $pkgDir ($item.Rel -replace '/', '\')
    $hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $dst).Hash
    Write-Host ("Staged {0,-40} {1}  {2}" -f $item.Name, $hash, $dst) -ForegroundColor Green
}
