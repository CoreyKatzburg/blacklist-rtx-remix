# Texture fill counters check (worker E's poolN / lockN / updN tokens). Needs a GPU, no game.
#   normal build must PASS tests\hitch\texture_fill_test.cpp on a real D3D9 device
#   mutant build (/DTEXTURE_FILL_MUTANT, lock and update counters swapped) must FAIL
#   no-count build (/DTEXTURE_FILL_NOCOUNT, wrappers removed) must FAIL fake cases 1, 2 and 3
#   the normal build also measures the counters' cost per LockRect / UpdateTexture call and fails above its limit
#   d3d9_proxy.cpp's HookLockRect, HookUpdateTexture and HookCreateTexture must call the matching *Counted wrapper,
#   so the hooks are wired the way the test calls them
# Exit code 0 = all as required.
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$remixRoot = Split-Path -Parent (Split-Path -Parent $here)
$unified = Join-Path $remixRoot 'play-setup\unified-build'
$build = Join-Path $here '_build'
New-Item -ItemType Directory -Force -Path $build | Out-Null
$vcvars = 'C:\Program Files (x86)\Microsoft Visual Studio\18\BuildTools\VC\Auxiliary\Build\vcvarsall.bat'
if (-not (Test-Path -LiteralPath $vcvars)) { throw "MSVC not found: $vcvars" }

function Build-Test([string]$name, [string]$define) {
    $exe = Join-Path $build "$name.exe"
    $obj = Join-Path $build $name
    New-Item -ItemType Directory -Force -Path $obj | Out-Null
    $cmd = "call `"$vcvars`" x86 >nul && cl.exe /nologo /std:c++17 /O2 /MT /W4 /WX /EHsc $define /I`"$unified`" " +
           "`"$(Join-Path $here 'texture_fill_test.cpp')`" /Fe:`"$exe`" /Fo:`"$obj\\`" /link /NOLOGO d3d9.lib user32.lib"
    & $env:ComSpec /d /s /c $cmd | Out-Host
    if ($LASTEXITCODE -ne 0) { throw "compile of $name failed (exit $LASTEXITCODE)" }
    $exe
}

$ok = $true
$normal = Build-Test 'texture_fill_test' ''
& $normal | Out-Host
if ($LASTEXITCODE -eq 2) { Write-Host 'texture fill test: NOT RUN (no D3D9 device)' -ForegroundColor Yellow; exit 2 }
if ($LASTEXITCODE -ne 0) { Write-Host 'FAIL: the normal build did not pass' -ForegroundColor Red; $ok = $false }

$mutant = Build-Test 'texture_fill_mutant' '/DTEXTURE_FILL_MUTANT'
& $mutant | Out-Host
if ($LASTEXITCODE -ne 1) { Write-Host 'FAIL: the swapped-counter build did not fail' -ForegroundColor Red; $ok = $false }
else { Write-Host 'ok: the swapped-counter build fails, as it must' }

# Rule removed: D3D9 called without the wrappers. E2's three fake cases (1, 2, 3) must each fail.
$nocount = Build-Test 'texture_fill_nocount' '/DTEXTURE_FILL_NOCOUNT'
$out = & $nocount | Out-String
Write-Host $out
$missed = @('1', '2', '3') | Where-Object { $out -notmatch "(?m)^FAIL $_ " }
if ($LASTEXITCODE -ne 1 -or $missed) {
    Write-Host "FAIL: the no-count build did not fail every fake case (still passing: $($missed -join ', '))" -ForegroundColor Red
    $ok = $false
} else { Write-Host 'ok: the no-count build fails fake cases 1, 2 and 3, as it must' }

# Wiring: each hook body (from its signature to the next "\n}") must call its own wrapper and not the other one.
$proxySource = Get-Content -Raw (Join-Path $unified 'd3d9_proxy.cpp')
foreach ($pair in @(@('HookLockRect', 'LockRectCounted', 'UpdateTextureCounted'),
                    @('HookUpdateTexture', 'UpdateTextureCounted', 'LockRectCounted'),
                    @('HookCreateTexture', 'CreateTextureCounted', 'LockRectCounted'))) {
    $m = [regex]::Match($proxySource, "HRESULT WINAPI $($pair[0])\([\s\S]*?\r?\n\}")
    $body = $m.Value
    if (-not $m.Success -or $body -notmatch $pair[1] -or $body -match $pair[2]) {
        Write-Host "FAIL: $($pair[0]) in d3d9_proxy.cpp does not call $($pair[1]) (or calls $($pair[2]))" -ForegroundColor Red
        $ok = $false
    } else { Write-Host "ok: $($pair[0]) calls $($pair[1])" }
}

if ($ok) { Write-Host 'texture fill test: PASS' -ForegroundColor Green; exit 0 }
Write-Host 'texture fill test: FAIL' -ForegroundColor Red
exit 1
