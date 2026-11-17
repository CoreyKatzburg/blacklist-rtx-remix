# Builds and runs every game-free check for the Remix / bridge source changes (Track B).
# Exit code 0 = all passed. No game, no GPU.
#
#   capture_analysis_test   vertex shader analysis (geometry hash ranges, normal output) on synthetic
#                           shaders and on all 263 shaders in the game's VertexShaders.bin
#   check_capture_dump.py   independent Python cross-check of the analysis against the constant tables
#   runtime_logic_test      camera-cut rule, instance trace classification, texture slot retire,
#                           telemetry line format and writer
#   check_telemetry_contract.py  telemetry sample matches the Phase 5 JSONL contract
#   draw_log_test           per-draw scene log line format, texcoord source rule, arming (rtx_draw_log.h)
#   bridge_queue_test       command batching in the bridge queue (needs the x64 bridge built once:
#                           build_custom_remix.ps1 -Target bridge)
#   bridge_reply_wrap_test  server replies wrap their data queue without waiting on the game
[CmdletBinding()]
param(
    [string]$GameShaders = "D:\Steam\steamapps\common\Tom Clancy's Splinter Cell Blacklist\data\Shaders\d3d9\VertexShaders.bin"
)

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$remixRoot = Split-Path -Parent (Split-Path -Parent $here)
$bridgeDir = Join-Path $remixRoot 'src\dxvk-remix\bridge'
$build = Join-Path $here '_build'
New-Item -ItemType Directory -Force -Path $build | Out-Null

$vcvars = 'C:\Program Files (x86)\Microsoft Visual Studio\18\BuildTools\VC\Auxiliary\Build\vcvarsall.bat'
$python = if (Test-Path 'C:\Python314\python.exe') { 'C:\Python314\python.exe' } else { 'python' }
if (-not (Test-Path -LiteralPath $vcvars)) { throw "MSVC not found: $vcvars" }

$failed = New-Object System.Collections.Generic.List[string]

function Invoke-Cl([string]$Name, [string]$Arguments, [string]$LinkArguments = '') {
    $installer = 'C:\Program Files (x86)\Microsoft Visual Studio\Installer'
    $cmd = "set `"PATH=%PATH%;$installer`" && call `"$vcvars`" x64 >nul && cd /d `"$build`" && cl.exe /nologo /std:c++17 /O2 /EHsc /MT $Arguments /Fe:`"$build\$Name.exe`" /link /NOLOGO $LinkArguments"
    $output = & $env:ComSpec /d /s /c $cmd 2>&1
    $code = $LASTEXITCODE
    $output | Where-Object { "$_" -notmatch '^\S+\.cpp$' } | ForEach-Object { Write-Host $_ }
    if ($code -ne 0) { $failed.Add("$Name (compile)"); return $false }
    return $true
}

function Invoke-Check([string]$Name, [scriptblock]$Run) {
    Write-Host "=== $Name ===" -ForegroundColor Cyan
    & $Run
    if ($LASTEXITCODE -ne 0) { $failed.Add($Name) }
}

# 1. Vertex shader capture analysis.
if (Invoke-Cl 'capture_analysis_test' "/W4 /WX `"$here\capture_analysis_test.cpp`"") {
    $dump = Join-Path $build 'capture_dump.jsonl'
    Invoke-Check 'capture_analysis_test' { & "$build\capture_analysis_test.exe" --game $GameShaders --dump $dump }
    if (Test-Path -LiteralPath $GameShaders) {
        Invoke-Check 'check_capture_dump.py' { & $python -I "$here\check_capture_dump.py" $dump $GameShaders }
    } else {
        Write-Host "SKIP check_capture_dump.py: game shaders not found" -ForegroundColor Yellow
    }
}

# 2. Runtime pure logic + telemetry.
# /wd4267: size_t -> uint32_t narrowing in upstream SparseUniqueCache lines.
if (Invoke-Cl 'runtime_logic_test' "/W4 /WX /wd4267 `"$here\runtime_logic_test.cpp`"") {
    Invoke-Check 'runtime_logic_test' { & "$build\runtime_logic_test.exe" $build }
    Invoke-Check 'check_telemetry_contract.py' { & $python -I "$here\check_telemetry_contract.py" (Join-Path $build 'telemetry_sample.jsonl') }
}

# 2b. Per-draw scene log (rtx.drawLog.tag): line format, texcoord source rule, arming.
if (Invoke-Cl 'draw_log_test' "/W4 /WX `"$here\draw_log_test.cpp`"") {
    Invoke-Check 'draw_log_test' { & "$build\draw_log_test.exe" }
}

# 2c. USD capture material binding (rtx.capture.bindInstanceMaterials). The /DFORCE_OPTION_OFF build runs the
#     "option on" expectations with the option off and must fail: that proves the test needs the fix to pass.
if (Invoke-Cl 'capture_binding_test' "/W4 /WX `"$here\capture_binding_test.cpp`"") {
    Invoke-Check 'capture_binding_test' { & "$build\capture_binding_test.exe" }
}
if (Invoke-Cl 'capture_binding_test_off' "/W4 /WX /DFORCE_OPTION_OFF `"$here\capture_binding_test.cpp`"") {
    Write-Host '=== capture_binding_test_off (must fail) ===' -ForegroundColor Cyan
    & "$build\capture_binding_test_off.exe" | Out-Null
    if ($LASTEXITCODE -eq 0) { $failed.Add('capture_binding_test_off passed without the fix') } else { Write-Host 'ok   fails without the fix' }
}

# 3. Bridge command queue (real header, linked with the x64 bridge util library).
$libutil = Join-Path $bridgeDir '_Comp64Release\src\util\libutil.a'
if (Test-Path -LiteralPath $libutil) {
    $inc = "/I`"$bridgeDir\src\util`" /I`"$bridgeDir\src`""
    if (Invoke-Cl 'bridge_queue_test' "/W3 /wd4267 /DNOMINMAX /DNDEBUG $inc `"$here\bridge_queue_test.cpp`"" "`"$libutil`" user32.lib advapi32.lib shell32.lib ole32.lib") {
        Invoke-Check 'bridge_queue_test' { & "$build\bridge_queue_test.exe" }
    }
    # Server-flavoured build: the real server reply path must never wait on the game (2026-11-12 freeze).
    if (Invoke-Cl 'bridge_reply_wrap_test' "/W3 /wd4267 /DNOMINMAX /DNDEBUG /DREMIX_BRIDGE_SERVER $inc `"$here\bridge_reply_wrap_test.cpp`"" "`"$libutil`" user32.lib advapi32.lib shell32.lib ole32.lib") {
        Invoke-Check 'bridge_reply_wrap_test' { & "$build\bridge_reply_wrap_test.exe" }
    }
    if (Invoke-Cl 'bridge_fast_query_test' "/W3 /wd4267 /DNOMINMAX /DNDEBUG $inc `"$here\bridge_fast_query_test.cpp`"" "`"$libutil`" user32.lib advapi32.lib shell32.lib ole32.lib") {
        Invoke-Check 'bridge_fast_query_test' { & "$build\bridge_fast_query_test.exe" }
    }
} else {
    $failed.Add("bridge tests (missing $libutil; run build_custom_remix.ps1 -Target bridge)")
}

if ($failed.Count -gt 0) {
    Write-Host ("FAILED: " + ($failed -join ', ')) -ForegroundColor Red
    exit 1
}
Write-Host 'ALL REMIX SOURCE CHECKS PASSED' -ForegroundColor Green
exit 0
