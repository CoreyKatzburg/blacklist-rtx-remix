# Builds and runs the lightpub logic tests (light_calibration.h), no game, no GPU:
#   light_trace_test.cpp     the change trace behind lightpub_trace.csv
#   flare_standin_test.cpp   flare stand-in choice and the "lightpub: stray" counts, partly on fixtures\run20
# Exit code 0 = all passed.
$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$vcvars = 'C:\Program Files (x86)\Microsoft Visual Studio\18\BuildTools\VC\Auxiliary\Build\vcvarsall.bat'
$build = Join-Path $env:TEMP 'blacklist_light_trace_test'
New-Item -ItemType Directory -Force -Path $build | Out-Null
$run20 = Join-Path $here '..\..\fixtures\run20'
$failed = 0
foreach ($test in @(@{ name = 'light_trace_test'; args = @() }, @{ name = 'flare_standin_test'; args = @($run20) })) {
    $exe = Join-Path $build "$($test.name).exe"
    $cmd = "call `"$vcvars`" x64 >nul && cd /d `"$build`" && cl.exe /nologo /std:c++17 /O2 /W4 /WX /EHsc /MT `"$here\$($test.name).cpp`" /Fe:`"$exe`" /link /NOLOGO"
    & $env:ComSpec /d /s /c $cmd | Out-Host
    if ($LASTEXITCODE -ne 0) { Write-Host "$($test.name): compile failed"; $failed++; continue }
    & $exe @($test.args)
    if ($LASTEXITCODE -ne 0) { $failed++ }
}
exit [int]($failed -ne 0)
