# Game-free test of the play-setup installer (play-setup\RemixPlayCommon.ps1): Install-PlaySetup, a pkg rebuild,
# Uninstall-PlaySetup and a reinstall, against a scratch game folder and a fake pkg (REMIX_TEST_* variables).
# The real setup files (proxy, resting proxy, configs, glow pack, materials) are only read. Exit 0 = all checks pass.
# Run with Windows PowerShell 5.1 (the desktop shortcuts use powershell.exe): powershell -File test_installer.ps1
$ErrorActionPreference = 'Stop'
$scratch = Join-Path ([IO.Path]::GetTempPath()) ('remix-installer-test-' + [Guid]::NewGuid().ToString('N').Substring(0, 8))
$game = Join-Path $scratch 'game'; $pkg = Join-Path $scratch 'pkg'
New-Item -ItemType Directory -Force "$game\src\SYSTEM\.trex", "$pkg\.trex\usd" | Out-Null
$env:REMIX_TEST_GAME_ROOT = $game; $env:REMIX_TEST_PKG = $pkg; $env:REMIX_TEST_BACKUPS = Join-Path $scratch 'backups'
function Bytes($path, $text) { [IO.File]::WriteAllText($path, $text) }
Bytes "$pkg\d3d9.dll" 'bridge client v1'
Bytes "$pkg\.trex\NvRemixBridge.exe" 'bridge v1'
Bytes "$pkg\.trex\d3d9.dll" 'runtime v1'
Bytes "$pkg\.trex\nvngx_dlss.dll" 'dlss old'
Bytes "$pkg\.trex\usd\plugInfo.json" '{}'
# the game folder before: a stale runtime file, a deliberate DLSS upgrade (the old one kept as .original), a log
Bytes "$game\src\SYSTEM\.trex\d3d9.dll" 'runtime v0'
Bytes "$game\src\SYSTEM\.trex\nvngx_dlss.dll" 'dlss NEW'
Bytes "$game\src\SYSTEM\.trex\nvngx_dlss.dll.original" 'dlss old'
Bytes "$game\src\SYSTEM\.trex\keep.dmp" 'crash dump'

. (Join-Path $PSScriptRoot '..\..\play-setup\RemixPlayCommon.ps1')
$failures = 0
function Check($name, $ok, $detail = '') {
    if ($ok) { Write-Host "[PASS] $name" } else { Write-Host "[FAIL] $name $detail"; $script:failures++ }
}
$text = { param($p) if (Test-Path -LiteralPath $p) { [IO.File]::ReadAllText($p) } else { '<missing>' } }
$offExtra = { @(Get-OffMismatches | Where-Object { $_ -ne 'launcher still switched to DX9' }) }   # real setup's note, not ours

try {
    Check 'paths point at the scratch folder' ($S -eq "$game\src\SYSTEM" -and $Pkg -eq $pkg)
    $before = @(Get-PlayMismatches)
    Check 'fresh folder reports what is missing' ($before -contains '.trex\d3d9.dll' -and $before -contains 'd3d9.dll (camera proxy)' -and
        @($before | Where-Object { $_ -like 'mod link*' }).Count -eq $ModLinks.Count) ($before -join ', ')
    Check 'DLSS upgrade with matching .original is not a mismatch' ($before -notcontains '.trex\nvngx_dlss.dll')

    # The proxy pin: a setup d3d9.dll that differs from $ProxyHash must be refused. Then pin the current builds, so
    # this test checks the install mechanics even while another track has rebuilt the proxy before integration.
    $pinned = $ProxyHash; $ProxyHash = '0' * 64
    $refused = $false; try { [void](Install-PlaySetup) } catch { $refused = $_.Exception.Message -like '*unexpected hash*' }
    Check 'install refuses a proxy build that is not the pinned one' ($refused -and -not (Test-Path "$S\d3d9.dll"))
    $ProxyHash = Get-Hash "$Setup\d3d9.dll"; $RestingProxyHash = Get-Hash "$Setup\resting\d3d9.dll.off"
    if ($ProxyHash -ne $pinned) { Write-Host "  note: play-setup\d3d9.dll differs from `$ProxyHash in RemixPlayCommon.ps1 (rebuilt, pin not updated yet)" }

    $left = @(Install-PlaySetup)
    Check 'install: nothing left to fix' ($left.Count -eq 0) ($left -join ', ')
    Check 'install: proxy and bridge client' ((Get-Hash "$S\d3d9.dll") -eq $ProxyHash -and (& $text "$S\d3d9_remix.dll") -eq 'bridge client v1')
    Check 'install: runtime copied, nested folders too' ((& $text "$S\.trex\d3d9.dll") -eq 'runtime v1' -and (& $text "$S\.trex\usd\plugInfo.json") -eq '{}')
    Check 'install: replaced runtime file kept in runtime-previous' ((& $text "$RuntimeBackup\d3d9.dll") -eq 'runtime v0')
    Check 'install: DLSS upgrade kept, unrelated files kept' ((& $text "$S\.trex\nvngx_dlss.dll") -eq 'dlss NEW' -and (Test-Path "$S\.trex\keep.dmp"))
    foreach ($link in $ModLinks.Keys) {
        Check "install: $(Split-Path -Leaf $link) linked to $($ModLinks[$link])" (Test-ModLinked $link)
    }
    foreach ($path in $Configs.Keys) {
        Check "install: $(Get-Label $path) equals setup copy" ((Get-Hash $path) -eq (Get-Hash (Join-Path $Setup $Configs[$path])))
    }

    Bytes "$pkg\.trex\d3d9.dll" 'runtime v2 (rebuilt)'
    $HashCache.Clear()
    $after = @(Get-PlayMismatches)
    Check 'pkg rebuild is detected' ($after.Count -eq 1 -and $after[0] -eq '.trex\d3d9.dll') ($after -join ', ')
    $left = @(Install-PlaySetup)
    Check 'pkg rebuild is installed' ($left.Count -eq 0 -and (& $text "$S\.trex\d3d9.dll") -eq 'runtime v2 (rebuilt)') ($left -join ', ')

    $backup = Uninstall-PlaySetup
    $off = & $offExtra
    Check 'off: stock state reached' ($off.Count -eq 0) ($off -join ', ')
    Check 'off: no d3d9.dll, no configs, no links' (-not (Test-Path "$S\d3d9.dll") -and -not (Test-Path "$S\rtx.conf") -and
        -not (Test-Path "$Root\rtx.conf") -and -not (Test-Path "$Root\rtx-remix\mods\materials") -and -not (Test-Path $LegacyGlowMod))
    Check 'off: link targets untouched' (Test-Path (Join-Path $RemixDir 'materials\mod.usda'))
    Check 'off: parked DLLs' ((Get-Hash "$S\d3d9.dll.off") -eq $RestingProxyHash -and (& $text "$S\d3d9_remix.dll.off") -eq 'bridge client v1')
    Check 'off: backup written' (Test-Path (Join-Path $backup 'd3d9.dll'))

    $left = @(Install-PlaySetup)
    Check 'reinstall from the off state' ($left.Count -eq 0 -and -not (Test-Path "$S\d3d9.dll.off") -and -not (Test-Path "$S\d3d9_remix.dll.off")) ($left -join ', ')
    [void](Uninstall-PlaySetup)
} catch {
    Check 'no exception' $false $_.Exception.Message
} finally {
    Remove-ModLinks
    # .NET's recursive delete removes a junction without entering it, so the real materials\ and glow\mod are safe
    # even if a link were left behind (Remove-Item -Recurse in PowerShell 5.1 can follow junctions).
    if (Test-Path -LiteralPath $scratch) { [IO.Directory]::Delete($scratch, $true) }
}
if ($failures) { Write-Host "INSTALLER TEST FAILED ($failures)"; exit 1 }
Write-Host 'INSTALLER TEST PASSED'
exit 0
