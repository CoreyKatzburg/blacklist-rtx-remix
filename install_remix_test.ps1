# Installs RTX Remix 1.5.2 + the Blacklist camera proxy into the DX9 game folder.
# Nothing existing is overwritten (the script stops if a target already exists).
$ErrorActionPreference = 'Stop'
$g   = "D:\Steam\steamapps\common\Tom Clancy's Splinter Cell Blacklist\src\SYSTEM"
$pkg = Join-Path $PSScriptRoot "pkg"   # D:\BlacklistOverhaul\tools\remix\pkg
$cam = "D:\BlacklistOverhaul\experiments\remix-camera-dll"
foreach ($n in 'd3d9.dll','d3d9_remix.dll','.trex') { if (Test-Path "$g\$n") { throw "$n already exists in the game folder; not overwriting" } }
Copy-Item "$pkg\.trex" "$g\.trex" -Recurse
Copy-Item "$pkg\d3d9.dll" "$g\d3d9_remix.dll"
Copy-Item "$cam\d3d9.dll" "$g\d3d9.dll"
Write-Host "Installed: .trex\, d3d9_remix.dll (RTX Remix bridge), d3d9.dll (camera proxy)"
