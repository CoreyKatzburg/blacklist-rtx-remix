# Removes everything the Remix test added to the DX9 game folder: every item that was
# not there before install (list saved in game_SYSTEM_before.txt). Close the game first.
$g    = "D:\Steam\steamapps\common\Tom Clancy's Splinter Cell Blacklist\src\SYSTEM"
$snap = Join-Path $PSScriptRoot "game_SYSTEM_before.txt"   # D:\BlacklistOverhaul\tools\remix
$before = Get-Content $snap
$added = Get-ChildItem $g -Force | Where-Object { $before -notcontains $_.Name }
if (-not $added) { Write-Host "Nothing to remove; the folder matches the pre-install list."; return }
$added | ForEach-Object { Write-Host "Removing $($_.Name)"; Remove-Item $_.FullName -Recurse -Force }
Write-Host "Done. The DX9 game folder is back to its pre-install file list."
# Remix also creates an rtx-remix folder in the game's root folder.
$root = "D:\Steam\steamapps\common\Tom Clancy's Splinter Cell Blacklist\rtx-remix"
if (Test-Path $root) { Write-Host "Removing $root"; Remove-Item $root -Recurse -Force }
