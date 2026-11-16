# HitchSampler Present tag check (no game, no GPU). Builds tests\hitch\hitch_tag_test.cpp twice, x86 like the proxy,
# against the real play-setup\unified-build\proxy_diagnostics.cpp sampler:
#   normal build  must PASS: a stall inside the fake Present is tagged "present", a stall after it is not
#   mutant build  (/DHITCH_TAG_MUTANT, Present called without proxy::CallPresentTagged) must FAIL
# Exit code 0 = both as required.
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

# proxy_diagnostics.cpp and what it links against; d3d9_proxy.cpp (the DLL entry points) stays out.
$sources = @('proxy_diagnostics.cpp', 'proxy_log.cpp', 'lightpub.cpp', 'shader_traits.cpp', 'camera_solver.cpp',
             'pass_graph.cpp', 'draw_filter.cpp', 'decal_tag.cpp', 'handoff_renderer.cpp') |
    ForEach-Object { "`"$(Join-Path $unified $_)`"" }

function Build-Test([string]$name, [string]$define) {
    $exe = Join-Path $build "$name.exe"
    $obj = Join-Path $build $name
    New-Item -ItemType Directory -Force -Path $obj | Out-Null
    # "\\" before the closing quote: a single backslash would escape the quote for cl's parser.
    $cmd = "call `"$vcvars`" x86 >nul && cl.exe /nologo /std:c++17 /O2 /MT /W4 /WX /EHsc $define /I`"$unified`" " +
           "`"$(Join-Path $here 'hitch_tag_test.cpp')`" $($sources -join ' ') /Fe:`"$exe`" /Fo:`"$obj\\`" " +
           "/link /NOLOGO d3d9.lib user32.lib psapi.lib"
    & $env:ComSpec /d /s /c $cmd | Out-Host
    if ($LASTEXITCODE -ne 0) { throw "compile of $name failed (exit $LASTEXITCODE)" }
    $exe
}

$ok = $true
$normal = Build-Test 'hitch_tag_test' ''
& $normal | Out-Host
if ($LASTEXITCODE -ne 0) { Write-Host 'FAIL: the tagged build did not pass' -ForegroundColor Red; $ok = $false }

$mutant = Build-Test 'hitch_tag_mutant' '/DHITCH_TAG_MUTANT'
& $mutant | Out-Host
if ($LASTEXITCODE -eq 0) { Write-Host 'FAIL: the build without the tag passed, so the test cannot fail' -ForegroundColor Red; $ok = $false }
else { Write-Host 'ok: the build without the tag fails, as it must' }

if ($ok) { Write-Host 'hitch tag test: PASS' -ForegroundColor Green; exit 0 }
Write-Host 'hitch tag test: FAIL' -ForegroundColor Red
exit 1
