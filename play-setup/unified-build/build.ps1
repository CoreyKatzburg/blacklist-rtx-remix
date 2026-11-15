[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$taskDirectory = Split-Path -Parent $MyInvocation.MyCommand.Path
$vcvars = 'C:\Program Files (x86)\Microsoft Visual Studio\18\BuildTools\VC\Auxiliary\Build\vcvarsall.bat'

if (-not (Test-Path -LiteralPath $vcvars -PathType Leaf)) {
    throw "MSVC environment script was not found: $vcvars"
}

$sources = @(
    "$taskDirectory\d3d9_proxy.cpp",
    "$taskDirectory\proxy_log.cpp",
    "$taskDirectory\decal_tag.cpp",
    "$taskDirectory\handoff_renderer.cpp",
    "$taskDirectory\draw_filter.cpp",
    "$taskDirectory\proxy_diagnostics.cpp",
    "$taskDirectory\lightpub.cpp",
    "$taskDirectory\shader_traits.cpp",
    "$taskDirectory\camera_solver.cpp",
    "$taskDirectory\pass_graph.cpp"
) | ForEach-Object { "`"$_`"" }

$command = @"
call "$vcvars" x86 >nul && cl.exe /nologo /std:c++17 /O2 /MT /W4 /WX /EHsc /LD $($sources -join ' ') /Fo:"$taskDirectory\\" /link /NOLOGO /DEF:"$taskDirectory\d3d9.def" /OUT:"$taskDirectory\d3d9.dll" /IMPLIB:"$taskDirectory\d3d9.lib" /PDB:"$taskDirectory\d3d9.pdb"
"@

Push-Location -LiteralPath $taskDirectory
try {
    & $env:ComSpec /d /s /c $command
    if ($LASTEXITCODE -ne 0) {
        throw "MSVC failed with exit code $LASTEXITCODE"
    }
}
finally {
    Pop-Location
}

Write-Host "Built x86 proxy: $taskDirectory\d3d9.dll"
