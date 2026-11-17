# Stage texture dump check (worker E, row 20: [Proxy] DumpStageTexturesPs / DumpStageTexturesStages). Needs a GPU, no game.
#   normal build must PASS tests\hitch\stage_texture_dump_test.cpp on a real D3D9 device
#   the .dds files it writes must decode with PIL to the pixels the test put in (an independent reader, not our header code)
#   mutant build (/DSTAGE_TEXTURE_DUMP_MUTANT, width and height swapped in the header) must FAIL, and PIL must not read 16x4
#   stage_texture_dump.h must read level 0 only through the surface LockRect, never IDirect3DTexture9::LockRect,
#   so the lockN fill counter (hooked on the texture LockRect only) cannot see the dump's read
# Exit code 0 = all as required, 2 = no D3D9 device.
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$remixRoot = Split-Path -Parent (Split-Path -Parent $here)
$unified = Join-Path $remixRoot 'play-setup\unified-build'
$build = Join-Path $here '_build'
$python = 'D:\BlacklistOverhaul\tools\upscalers\py314\Scripts\python.exe'
New-Item -ItemType Directory -Force -Path $build | Out-Null
$vcvars = 'C:\Program Files (x86)\Microsoft Visual Studio\18\BuildTools\VC\Auxiliary\Build\vcvarsall.bat'
if (-not (Test-Path -LiteralPath $vcvars)) { throw "MSVC not found: $vcvars" }
if (-not (Test-Path -LiteralPath $python)) { throw "Python not found: $python" }

function Build-Test([string]$name, [string]$define) {
    $exe = Join-Path $build "$name.exe"
    $obj = Join-Path $build $name
    New-Item -ItemType Directory -Force -Path $obj | Out-Null
    $cmd = "call `"$vcvars`" x86 >nul && cl.exe /nologo /std:c++17 /O2 /MT /W4 /WX /EHsc $define /I`"$unified`" " +
           "`"$(Join-Path $here 'stage_texture_dump_test.cpp')`" /Fe:`"$exe`" /Fo:`"$obj\\`" /link /NOLOGO d3d9.lib user32.lib"
    & $env:ComSpec /d /s /c $cmd | Out-Host
    if ($LASTEXITCODE -ne 0) { throw "compile of $name failed (exit $LASTEXITCODE)" }
    $exe
}

# Decodes the dumped files with PIL. Prints one line per file: size, then the mean of each 4-pixel column of luma (0..1).
$decode = @'
import sys
from PIL import Image
for path in sys.argv[1:]:
    try:
        im = Image.open(path)
        im.load()
        im = im.convert("L")
        w, h = im.size
        px = im.tobytes()
        cols = [sum(px[y * w + x] for y in range(h) for x in range(c, min(c + 4, w))) / (255.0 * h * min(4, w - c))
                for c in range(0, w, 4)]
        print(f"{w}x{h} " + ",".join(f"{v:.3f}" for v in cols))
    except Exception as exc:
        print(f"unreadable {type(exc).__name__}")
'@
$decodePy = Join-Path $build 'decode_stage_dump.py'
Set-Content -LiteralPath $decodePy -Value $decode -Encoding utf8

function Decode([string]$folder) {
    $files = @('stage_dump_dxt1.dds', 'stage_dump_argb.dds') | ForEach-Object { Join-Path $folder $_ }
    @(& $python -I $decodePy @files)
}

$ok = $true

$normalOut = Join-Path $build 'normal'
New-Item -ItemType Directory -Force -Path $normalOut | Out-Null
Get-ChildItem -LiteralPath $normalOut -Filter *.dds | Remove-Item
$normal = Build-Test 'stage_texture_dump_test' ''
& $normal $normalOut | Out-Host
if ($LASTEXITCODE -eq 2) { Write-Host 'stage texture dump test: NOT RUN (no D3D9 device)' -ForegroundColor Yellow; exit 2 }
if ($LASTEXITCODE -ne 0) { Write-Host 'FAIL: the normal build did not pass' -ForegroundColor Red; $ok = $false }

# Independent read: DXT1 16x4 is white, white, white, black by 4-pixel column; ARGB 2x2 is white/black over black/white.
$decoded = Decode $normalOut
Write-Host "PIL: $($decoded -join ' | ')"
if ($decoded.Count -ne 2 -or $decoded[0] -ne '16x4 1.000,1.000,1.000,0.000' -or $decoded[1] -ne '2x2 0.500') {
    Write-Host 'FAIL: PIL does not read the dumped files as the pixels the test wrote' -ForegroundColor Red
    $ok = $false
} else { Write-Host 'ok: PIL reads DXT1 16x4 as 1,1,1,0 by column and ARGB 2x2 at mean 0.5' }

$mutantOut = Join-Path $build 'mutant'
New-Item -ItemType Directory -Force -Path $mutantOut | Out-Null
Get-ChildItem -LiteralPath $mutantOut -Filter *.dds | Remove-Item
$mutant = Build-Test 'stage_texture_dump_mutant' '/DSTAGE_TEXTURE_DUMP_MUTANT'
& $mutant $mutantOut | Out-Host
$mutantExit = $LASTEXITCODE
$mutantDecoded = Decode $mutantOut
Write-Host "PIL (mutant): $($mutantDecoded -join ' | ')"
if ($mutantExit -ne 1 -or $mutantDecoded[0] -like '16x4 *') {
    Write-Host 'FAIL: the swapped-header build was not caught by both the test and PIL' -ForegroundColor Red
    $ok = $false
} else { Write-Host 'ok: the swapped-header build fails, and PIL does not read it as 16x4' }

# Source check: the dump reads through IDirect3DSurface9::LockRect only.
function Get-Locks([string]$text) {
    @([regex]::Matches($text, '(\w+)\s*->\s*LockRect\s*\(') | ForEach-Object { $_.Groups[1].Value })
}
$header = Get-Content -Raw (Join-Path $unified 'stage_texture_dump.h')
$locks = @(Get-Locks $header)
if ($locks.Count -ne 1 -or $locks[0] -ne 'surface') {
    Write-Host "FAIL: stage_texture_dump.h locks through '$($locks -join ', ')', not only surface->LockRect" -ForegroundColor Red
    $ok = $false
} else { Write-Host 'ok: stage_texture_dump.h reads only through surface->LockRect' }
# Fake case for the source check: a header that locks the texture itself must be caught.
$fakeLocks = @(Get-Locks ($header -replace 'surface->LockRect', 'texture->LockRect'))
if ($fakeLocks.Count -eq 1 -and $fakeLocks[0] -eq 'surface') {
    Write-Host 'FAIL: the source check does not catch texture->LockRect' -ForegroundColor Red
    $ok = $false
} else { Write-Host 'ok: the source check catches texture->LockRect' }

# Source check: with the dump off the draw dump lists stages 0..7 exactly as before (its texture loop in
# proxy_diagnostics.cpp is bounded by DrawDumpStageCount(stageDump), which the C++ test checks gives 8 when off).
function Get-TextureLoopBounds([string]$text) {
    @([regex]::Matches($text, 'for\s*\(\s*DWORD\s+i\s*=\s*0\s*;\s*i\s*<\s*([^;]+?)\s*;\s*\+\+i\s*\)\s*\{\s*IDirect3DBaseTexture9\*\s*texture\s*=\s*nullptr;\s*if\s*\(\s*FAILED\(\s*device->GetTexture\(\s*i') |
      ForEach-Object { $_.Groups[1].Value })
}
$diag = Get-Content -Raw (Join-Path $unified 'proxy_diagnostics.cpp')
$bounds = @(Get-TextureLoopBounds $diag)
if ($bounds.Count -ne 1 -or $bounds[0] -ne 'DrawDumpStageCount(stageDump)') {
    Write-Host "FAIL: the draw dump's texture loop is bounded by '$($bounds -join ', ')', not DrawDumpStageCount(stageDump)" -ForegroundColor Red
    $ok = $false
} else { Write-Host 'ok: the draw dump lists stages through DrawDumpStageCount(stageDump), 8 while the dump is off' }
# Fake case for this check: a loop that always lists 16 stages must be caught.
$fakeBounds = @(Get-TextureLoopBounds ($diag -replace 'i < DrawDumpStageCount\(stageDump\)', 'i < 16'))
if ($fakeBounds.Count -eq 1 -and $fakeBounds[0] -eq 'DrawDumpStageCount(stageDump)') {
    Write-Host 'FAIL: the loop check does not catch a fixed 16-stage loop' -ForegroundColor Red
    $ok = $false
} else { Write-Host 'ok: the loop check catches a fixed 16-stage loop' }

if ($ok) { Write-Host 'stage texture dump test: PASS' -ForegroundColor Green; exit 0 }
Write-Host 'stage texture dump test: FAIL' -ForegroundColor Red
exit 1
