# Builds and runs light_trace_test.cpp: the change trace behind lightpub_trace.csv (light_calibration.h).
# Exit code 0 = all passed. No game, no GPU.
$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$vcvars = 'C:\Program Files (x86)\Microsoft Visual Studio\18\BuildTools\VC\Auxiliary\Build\vcvarsall.bat'
$build = Join-Path $env:TEMP 'blacklist_light_trace_test'
New-Item -ItemType Directory -Force -Path $build | Out-Null
$exe = Join-Path $build 'light_trace_test.exe'
$cmd = "call `"$vcvars`" x64 >nul && cd /d `"$build`" && cl.exe /nologo /std:c++17 /O2 /W4 /WX /EHsc /MT `"$here\light_trace_test.cpp`" /Fe:`"$exe`" /link /NOLOGO"
& $env:ComSpec /d /s /c $cmd | Out-Host
if ($LASTEXITCODE -ne 0) { Write-Host 'light_trace_test: compile failed'; exit 1 }
& $exe
exit $LASTEXITCODE
