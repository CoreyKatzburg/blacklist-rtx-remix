# Shared paths, hashes and checks for Play_Blacklist_with_Remix.ps1 and Turn_Remix_Off.ps1.
# Windows PowerShell 5.1 compatible (the desktop shortcuts use powershell.exe).
$ErrorActionPreference = 'Stop'
$Setup = $PSScriptRoot
# REMIX_TEST_* point the install at a scratch game folder, pkg and backup folder (tests\runtime\test_installer.ps1 only).
$Root = if ($env:REMIX_TEST_GAME_ROOT) { $env:REMIX_TEST_GAME_ROOT } else { "D:\Steam\steamapps\common\Tom Clancy's Splinter Cell Blacklist" }
$S = "$Root\src\SYSTEM"
$Lock = 'D:\BlacklistOverhaul\experiments\GAME_IN_USE.txt'
$LockTag = 'PLAY-SETUP'
# pkg\ holds the Remix build to install: the bridge client (d3d9.dll -> SYSTEM\d3d9_remix.dll) and the runtime
# (.trex\* -> SYSTEM\.trex\*). It may be rebuilt at any time, so install checks compare against pkg's own hashes.
$RemixDir = Split-Path -Parent $Setup
$Pkg = if ($env:REMIX_TEST_PKG) { $env:REMIX_TEST_PKG } else { Join-Path $RemixDir 'pkg' }
$RemixPkgDll = Join-Path $Pkg 'd3d9.dll'
$RuntimeSource = Join-Path $Pkg '.trex'
$BackupRoot = if ($env:REMIX_TEST_BACKUPS) { $env:REMIX_TEST_BACKUPS } else { Join-Path $Setup 'backups' }
$RuntimeBackup = Join-Path $BackupRoot 'runtime-previous'
$VideoSettings = Join-Path ([Environment]::GetFolderPath('MyDocuments')) 'Ubisoft\Blacklist\videoSettings.ini'
# Written while the launcher is switched to DX9; holds the line to put back.
$ApiRestoreNote = Join-Path $Setup 'launcher-api-restore.txt'
$SteamGameUrl = 'steam://rungameid/235600'

# Original exe, and the same exe with the Large Address Aware flag set (one header byte, 2026-10-26).
$GameExeHashes = @('158EFC5D0940FCAF3E4B16952C8F8861E16061509D9EB8EB5FF4AE3249BB5A05', 'EF8088D6676310F4B1DA2958C76524EE748B0043DE4B795254ECBBCEB5B77540')
# Camera proxy: verified d3d9.dll SHA-256 (2026-11-16, tools\remix bcf9fb4 WeaponSpotByShape and KeepTrackedLights keys and the ini write-time reload check, built from a clean export of bcf9fb4).
$ProxyHash = '61390EF0AFBAEFABECFD95EBCB55ED608CBA0C65F4A64540DFB2845D748AFF1C'
# Proxy build that sits disabled as d3d9.dll.off when Remix is off.
$RestingProxyHash = '593F0D47E95DDB768EC087636FA78E07FADBB600BD6EBB617D7714D4ADD2C18E'

$GameProcesses = 'Blacklist_game', 'Blacklist_DX11_game', 'Blacklist_Launcher', 'NvRemixBridge'
# Config files this setup owns: game-folder path -> source in the setup folder.
# Remix reads rtx.conf from the working directory: SYSTEM for direct launches, the game root
# when Steam starts Blacklist_Launcher.exe. The proxy ini and bridge.conf are found beside the exe.
$Configs = [ordered]@{
    "$S\d3d9_proxy.ini"    = 'd3d9_proxy.ini'
    "$S\rtx.conf"          = 'rtx.conf'
    "$Root\rtx.conf"       = 'rtx.conf'
    "$S\.trex\bridge.conf" = 'bridge.conf'
}
# Remix PBR + emissive materials mod, linked (junction) into rtx-remix\mods.
# Remix reads mods from the folder of the game exe (src\SYSTEM, see RtxFileSys::init in d3d9_main.cpp).
# The game-root link is kept beside it the same way rtx.conf is installed in both places.
# Legacy BlacklistGlow is retired (all 761 emissive overrides live in tools\remix\materials\mod.usda).
$LegacyGlowMod = "$Root\rtx-remix\mods\BlacklistGlow"
$ModLinks = [ordered]@{
    "$S\rtx-remix\mods\materials"      = Join-Path $RemixDir 'materials'
    "$Root\rtx-remix\mods\materials"   = Join-Path $RemixDir 'materials'
}
function Test-ModLinked($link) {
    $item = Get-Item -LiteralPath $link -ErrorAction SilentlyContinue
    $item -and @($item.Target)[0] -eq $ModLinks[$link]
}
$Managed = @("$S\d3d9.dll", "$S\d3d9.dll.off", "$S\d3d9_remix.dll", "$S\d3d9_remix.dll.off") + @($Configs.Keys)
$LogFiles = @("$S\d3d9_proxy.log") + @(foreach ($dir in "$S\rtx-remix\logs", "$Root\rtx-remix\logs") {
        foreach ($name in 'bridge32.log', 'bridge64.log', 'remix-dxvk.log') { "$dir\$name" } })

# SHA-256, cached per path + size + write time for this script run (the runtime is ~480 MB).
$HashCache = @{}
function Get-Hash($path) {
    $item = Get-Item -LiteralPath $path -ErrorAction SilentlyContinue
    if (-not $item -or $item.PSIsContainer) { return $null }
    $key = "$($item.FullName)|$($item.Length)|$($item.LastWriteTimeUtc.Ticks)"
    if (-not $HashCache.ContainsKey($key)) { $HashCache[$key] = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash }
    $HashCache[$key]
}
# The bridge client staged in pkg\ (installed as d3d9_remix.dll, parked as d3d9_remix.dll.off when Remix is off).
$RemixClientHash = Get-Hash $RemixPkgDll

# Runtime files (relative to .trex) whose game copy differs from pkg. A game file with a sibling "<name>.original"
# equal to pkg's file is a deliberate local upgrade (the newer DLSS DLLs) and is kept.
function Get-RuntimeMismatches {
    if (-not (Test-Path -LiteralPath $RuntimeSource)) { return @('pkg\.trex missing') }
    foreach ($file in Get-ChildItem -LiteralPath $RuntimeSource -Recurse -File) {
        $relative = $file.FullName.Substring($RuntimeSource.Length + 1)
        $target = Join-Path "$S\.trex" $relative
        $want = Get-Hash $file.FullName
        if ((Get-Hash $target) -eq $want) { continue }
        if ((Test-Path -LiteralPath $target) -and (Get-Hash "$target.original") -eq $want) { continue }
        ".trex\$relative"
    }
}

# Copies the differing runtime files from pkg (the replaced game copies go to backups\runtime-previous, which only
# ever holds the last install's), then checks every hash again.
function Install-Runtime {
    $todo = @(Get-RuntimeMismatches)
    if (-not $todo.Count) { return }
    if ($todo -contains 'pkg\.trex missing') { throw 'pkg\.trex is missing: nothing to install the Remix runtime from.' }
    if (Test-Path -LiteralPath $RuntimeBackup) { Remove-Item -LiteralPath $RuntimeBackup -Recurse -Force }
    foreach ($label in $todo) {
        $relative = $label.Substring(6)
        $target = Join-Path "$S\.trex" $relative
        if (Test-Path -LiteralPath $target) {
            $keep = Join-Path $RuntimeBackup $relative
            New-Item -ItemType Directory -Path (Split-Path $keep) -Force | Out-Null
            Copy-Item -LiteralPath $target -Destination $keep
        }
        New-Item -ItemType Directory -Path (Split-Path $target) -Force | Out-Null
        Copy-Item -LiteralPath (Join-Path $RuntimeSource $relative) -Destination $target -Force
    }
}

function Install-ModLinks {
    if (Test-Path -LiteralPath $LegacyGlowMod) { [IO.Directory]::Delete($LegacyGlowMod) }
    foreach ($link in $ModLinks.Keys) {
        if (Test-ModLinked $link) { continue }
        if (-not (Test-Path -LiteralPath (Join-Path $ModLinks[$link] 'mod.usda'))) { throw "Mod source has no mod.usda: $($ModLinks[$link])" }
        # Directory.Delete without recursion removes only a link, never the folder it points to.
        if (Test-Path -LiteralPath $link) { [IO.Directory]::Delete($link) }
        New-Item -ItemType Directory -Path (Split-Path $link) -Force | Out-Null
        New-Item -ItemType Junction -Path $link -Target $ModLinks[$link] | Out-Null
    }
}

function Remove-ModLinks {
    if (Test-Path -LiteralPath $LegacyGlowMod) { [IO.Directory]::Delete($LegacyGlowMod) }
    foreach ($link in $ModLinks.Keys) { if (Test-Path -LiteralPath $link) { [IO.Directory]::Delete($link) } }
}

function Get-RunningGame {
    @(Get-Process -Name $GameProcesses -ErrorAction SilentlyContinue)
}

function Get-LockText {
    if (Test-Path -LiteralPath $Lock) { (Get-Content -LiteralPath $Lock -Raw) } else { $null }
}

function Test-OwnLock {
    $text = Get-LockText
    $text -and $text.StartsWith($LockTag)
}

# Short label for a managed path in backups and messages.
function Get-Label($path) {
    if ($path.StartsWith("$S\")) { return $path.Substring($S.Length + 1) }
    if ($path.StartsWith("$Root\")) { return 'ROOT\' + $path.Substring($Root.Length + 1) }
    Split-Path -Leaf $path
}

# Copies every managed file that exists into backups\<stamp>-<label> before anything changes.
function Backup-GameFiles($label) {
    $dir = Join-Path $BackupRoot ((Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + $label)
    New-Item -ItemType Directory -Path $dir -Force | Out-Null
    foreach ($path in @($Managed) + $VideoSettings) {
        if (Test-Path -LiteralPath $path) {
            Copy-Item -LiteralPath $path -Destination (Join-Path $dir ((Get-Label $path) -replace '[\\:]', '_'))
        }
    }
    $dir
}

# Empty list = the play setup is installed exactly.
function Get-PlayMismatches {
    $bad = @()
    if ((Get-Hash "$S\d3d9.dll") -ne $ProxyHash) { $bad += 'd3d9.dll (camera proxy)' }
    if ((Get-Hash "$S\d3d9_remix.dll") -ne $RemixClientHash) { $bad += 'd3d9_remix.dll (Remix)' }
    foreach ($path in $Configs.Keys) {
        if ((Get-Hash $path) -ne (Get-Hash (Join-Path $Setup $Configs[$path]))) { $bad += (Get-Label $path) }
    }
    foreach ($link in $ModLinks.Keys) { if (-not (Test-ModLinked $link)) { $bad += "mod link $(Get-Label $link)" } }
    $bad += @(Get-RuntimeMismatches)
    foreach ($relative in 'd3d9.dll.off', 'd3d9_remix.dll.off') {
        if (Test-Path -LiteralPath "$S\$relative") { $bad += "$relative still present" }
    }
    $bad
}

# Installs everything Get-PlayMismatches checks (proxy, Remix client and runtime from pkg, configs, mod links),
# after backing up the files it replaces. Returns what still differs afterwards (empty = installed exactly).
function Install-PlaySetup {
    if (-not (Test-Path -LiteralPath $RemixPkgDll)) { throw "pkg\d3d9.dll (the Remix bridge client) is missing." }
    if ((Get-Hash "$Setup\d3d9.dll") -ne $ProxyHash) { throw 'Setup copy of d3d9.dll has an unexpected hash.' }
    $backup = Backup-GameFiles 'before-play'
    if (Test-Path -LiteralPath "$S\d3d9.dll.off") { Remove-Item -LiteralPath "$S\d3d9.dll.off" }
    Copy-Item -LiteralPath "$Setup\d3d9.dll" -Destination "$S\d3d9.dll" -Force
    if (Test-Path -LiteralPath "$S\d3d9_remix.dll.off") { Remove-Item -LiteralPath "$S\d3d9_remix.dll.off" }
    if ((Get-Hash "$S\d3d9_remix.dll") -ne $RemixClientHash) { Copy-Item -LiteralPath $RemixPkgDll -Destination "$S\d3d9_remix.dll" -Force }
    foreach ($path in $Configs.Keys) {
        New-Item -ItemType Directory -Path (Split-Path $path) -Force | Out-Null
        Copy-Item -LiteralPath (Join-Path $Setup $Configs[$path]) -Destination $path -Force
    }
    Install-Runtime
    Install-ModLinks
    Write-Step "Backed up the replaced files to $backup"
    @(Get-PlayMismatches)
}

# The file side of "Turn Remix off": both DLLs parked as .off (the resting proxy build, pkg's bridge client),
# Remix config files and mod links removed. Returns the backup folder. The launcher setting is Turn_Remix_Off's job.
function Uninstall-PlaySetup {
    $backup = Backup-GameFiles 'before-off'
    if (Test-Path -LiteralPath "$S\d3d9.dll") { Remove-Item -LiteralPath "$S\d3d9.dll" }
    if ((Get-Hash "$S\d3d9.dll.off") -ne $RestingProxyHash) {
        Copy-Item -LiteralPath "$Setup\resting\d3d9.dll.off" -Destination "$S\d3d9.dll.off" -Force
    }
    if (Test-Path -LiteralPath "$S\d3d9_remix.dll") {
        if (Test-Path -LiteralPath "$S\d3d9_remix.dll.off") { Remove-Item -LiteralPath "$S\d3d9_remix.dll" }
        else { Rename-Item -LiteralPath "$S\d3d9_remix.dll" -NewName 'd3d9_remix.dll.off' }
    }
    if ((Get-Hash "$S\d3d9_remix.dll.off") -ne $RemixClientHash) {
        Copy-Item -LiteralPath $RemixPkgDll -Destination "$S\d3d9_remix.dll.off" -Force
    }
    foreach ($path in $Configs.Keys) { if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path } }
    Remove-ModLinks
    $backup
}

# Empty list = Remix is fully off (normal game state).
function Get-OffMismatches {
    $bad = @()
    foreach ($path in @("$S\d3d9.dll", "$S\d3d9_remix.dll") + @($Configs.Keys)) {
        if (Test-Path -LiteralPath $path) { $bad += "$(Get-Label $path) present" }
    }
    if ((Get-Hash "$S\d3d9.dll.off") -ne $RestingProxyHash) { $bad += 'd3d9.dll.off missing or different' }
    if ((Get-Hash "$S\d3d9_remix.dll.off") -ne $RemixClientHash) { $bad += 'd3d9_remix.dll.off missing or different' }
    foreach ($link in $ModLinks.Keys) { if (Test-Path -LiteralPath $link) { $bad += "mod link $(Get-Label $link) present" } }
    if (Test-Path -LiteralPath $ApiRestoreNote) { $bad += 'launcher still switched to DX9' }
    $bad
}

function Get-StartupApi {
    if (-not (Test-Path -LiteralPath $VideoSettings)) { return '(videoSettings.ini not found)' }
    $line = Select-String -LiteralPath $VideoSettings -Pattern '^StartupGraphicsApi=' | Select-Object -First 1
    if ($line) { $line.Line } else { '(StartupGraphicsApi not set)' }
}

# Replaces only the StartupGraphicsApi line, keeping every other byte of the file.
function Set-StartupApiLine($newLine) {
    $text = [IO.File]::ReadAllText($VideoSettings, [Text.Encoding]::ASCII)
    $updated = [Regex]::Replace($text, '(?m)^StartupGraphicsApi=[^\r\n]*', $newLine)
    if ($updated -ne $text) { [IO.File]::WriteAllText($VideoSettings, $updated, [Text.Encoding]::ASCII) }
}

# The launcher starts the DX9 exe only for "StartupGraphicsApi=4" (DX11 otherwise).
function Switch-LauncherToDx9 {
    $current = Get-StartupApi
    if ($current -notmatch '^StartupGraphicsApi=') { throw "Cannot switch the launcher to DX9: $current" }
    if (-not (Test-Path -LiteralPath $ApiRestoreNote)) { Set-Content -LiteralPath $ApiRestoreNote -Value $current }
    Set-StartupApiLine 'StartupGraphicsApi=4'
}

# Puts back the line saved by Switch-LauncherToDx9. Returns the restored line, or $null.
function Restore-LauncherApi([switch]$KeepNote) {
    if (-not (Test-Path -LiteralPath $ApiRestoreNote)) { return $null }
    $original = (Get-Content -LiteralPath $ApiRestoreNote -TotalCount 1).Trim()
    if ($original -notmatch '^StartupGraphicsApi=\d+$') { $original = 'StartupGraphicsApi=5' }
    Set-StartupApiLine $original
    if (-not $KeepNote) { Remove-Item -LiteralPath $ApiRestoreNote }
    $original
}

# True while Steam's latest Blacklist launch is still waiting on a click (it shows the CD-key
# window on every launch), read from Steam's own console log.
function Test-SteamLaunchWaiting {
    $log = 'D:\Steam\logs\console_log.txt'
    if (-not (Test-Path -LiteralPath $log)) { return $false }
    $last = Get-Content -LiteralPath $log -Tail 300 | Where-Object { $_ -match 'GameAction \[AppID 235600,' } | Select-Object -Last 1
    [bool]($last -and $last -match 'waiting for user response')
}

# The DX11 lighting mod (ReShade's dxgi.dll in SYSTEM) is parked while Remix plays, so it can't load into the DX9 process.
$Dx11ModDll = "$S\dxgi.dll"; $Dx11ModParked = "$S\dxgi.dll.parked-for-remix"
function Suspend-Dx11Mod { if ((Test-Path -LiteralPath $Dx11ModDll) -and -not (Test-Path -LiteralPath $Dx11ModParked)) { Rename-Item -LiteralPath $Dx11ModDll -NewName (Split-Path -Leaf $Dx11ModParked); return $true }; $false }
function Resume-Dx11Mod { if ((Test-Path -LiteralPath $Dx11ModParked) -and -not (Test-Path -LiteralPath $Dx11ModDll)) { Rename-Item -LiteralPath $Dx11ModParked -NewName 'dxgi.dll'; return $true }; $false }

function Write-Step($text) { Write-Host "  $text" }
