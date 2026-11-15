# One labelled capture of the running game for root-cause analysis: a screenshot plus the proxy's full frame dump
# (every draw of one frame with render targets, shaders, textures and states, and every game light).
# The game must be running through Play_Blacklist_with_Remix.ps1 and showing gameplay.
#   -Label  name for the files           -Out  folder to copy them to (created)
#   -Rt     on | off | keep (default keep): switch ray tracing first, the same way remix_autotest.ps1 does
#   -Set    ini keys to change first, e.g. -Set 'Proxy:PassFilter=1','Proxy:ForwardScene=0' (the proxy re-reads these live)
# Writes <Out>\<Label>.png, <Label>_draws.tsv, <Label>_lights.tsv and prints one summary line.
param([Parameter(Mandatory)][string]$Label, [Parameter(Mandatory)][string]$Out, [ValidateSet('on', 'off', 'keep')][string]$Rt = 'keep', [string[]]$Set = @())
$ErrorActionPreference = 'Stop'
$S = "D:\Steam\steamapps\common\Tom Clancy's Splinter Cell Blacklist\src\SYSTEM"
$ini = "$S\d3d9_proxy.ini"
$ffmpeg = 'd:\ffmpeg\bin\ffmpeg.exe'
New-Item -ItemType Directory -Force $Out | Out-Null
Add-Type -TypeDefinition @"
using System; using System.Runtime.InteropServices; using System.Threading;
public static class RcaWin {
  [DllImport("user32.dll")] static extern bool SetForegroundWindow(IntPtr h);
  [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
  [DllImport("user32.dll")] static extern bool ShowWindow(IntPtr h, int cmd);
  [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr h, IntPtr pid);
  [DllImport("user32.dll")] static extern bool AttachThreadInput(uint a, uint b, bool attach);
  [DllImport("kernel32.dll")] static extern uint GetCurrentThreadId();
  public static bool Front(IntPtr h) {
    var fg = GetForegroundWindow(); if (fg == h) return true;
    uint a = GetWindowThreadProcessId(fg, IntPtr.Zero), me = GetCurrentThreadId();
    AttachThreadInput(me, a, true); ShowWindow(h, 9); SetForegroundWindow(h); AttachThreadInput(me, a, false);
    Thread.Sleep(200); return GetForegroundWindow() == h; }
}
"@
$game = Get-Process Blacklist_game -ErrorAction Stop | Select-Object -First 1
function SetKey([string]$text, [string]$section, [string]$key, [string]$value) {
    $pattern = '(?m)^' + [regex]::Escape($key) + '\s*=.*$'
    if ($text -match $pattern) { return [regex]::Replace($text, $pattern, "$key=$value") }
    return $text.Replace("[$section]", "[$section]`r`n$key=$value")
}
if ($Rt -ne 'keep') {
    $c = Get-Content $ini -Raw
    $c = [regex]::Replace($c, '(?m)^AutoRaytracing=\d', "AutoRaytracing=$(if ($Rt -eq 'on') { 1 } else { 0 })")
    $c = [regex]::Replace($c, '(?m)^rtx\.enableRaytracing\s*=.*\r?\n', '')
    if ($Rt -eq 'off') { $c = $c.Replace('[RemixConfig]', "[RemixConfig]`r`nrtx.enableRaytracing = False") }
    Set-Content $ini $c -NoNewline
    Start-Sleep -Seconds 5      # ini reload + AutoRaytracing (60 frames) + denoiser settle
}
if ($Set.Count) {
    $c = Get-Content $ini -Raw
    foreach ($item in $Set) {
        if ($item -notmatch '^(\w+):([\w.]+)=(.*)$') { "CAPTURE FAILED label=$Label : bad -Set item '$item'"; exit 1 }
        $c = SetKey $c $Matches[1] $Matches[2] $Matches[3]
    }
    Set-Content $ini $c -NoNewline
    Start-Sleep -Seconds 4      # the proxy re-reads these every 60 frames; let the image settle
}
$front = [RcaWin]::Front($game.MainWindowHandle)
Start-Sleep -Milliseconds 1500
# ddagrab never returns while the monitors are off (Windows sends it no desktop frames), so it gets 8 seconds
# and then gdigrab, which works either way, takes over.
Remove-Item "$Out\$Label.png" -ErrorAction SilentlyContinue
$grabInfo = [Diagnostics.ProcessStartInfo]::new($ffmpeg, "-v error -y -f lavfi -i ddagrab=output_idx=0:framerate=30:draw_mouse=0 -frames:v 1 -vf hwdownload,format=bgra `"$Out\$Label.png`"")
$grabInfo.UseShellExecute = $false; $grabInfo.CreateNoWindow = $true
$grab = [Diagnostics.Process]::Start($grabInfo)
if (-not $grab.WaitForExit(8000)) { $grab.Kill(); $grab.WaitForExit(); Remove-Item "$Out\$Label.png" -ErrorAction SilentlyContinue }
if (-not (Test-Path "$Out\$Label.png")) { & $ffmpeg -v error -y -f gdigrab -draw_mouse 0 -i desktop -frames:v 1 "$Out\$Label.png" }
# Frame dump: the proxy captures the frame after CaptureDraws goes 0 -> 1.
$dumpDir = "$S\framedump"
$before = @(Get-ChildItem $dumpDir -Filter 'f*_draws.tsv' -ErrorAction SilentlyContinue | ForEach-Object Name)
Set-Content $ini (SetKey (Get-Content $ini -Raw) 'Proxy' 'CaptureDraws' '0') -NoNewline
Start-Sleep -Milliseconds 400
Set-Content $ini (SetKey (Get-Content $ini -Raw) 'Proxy' 'CaptureDraws' '1') -NoNewline
$new = $null; $t = [Diagnostics.Stopwatch]::StartNew()
while (-not $new -and $t.Elapsed.TotalSeconds -lt 20) {
    Start-Sleep -Milliseconds 250
    $new = Get-ChildItem $dumpDir -Filter 'f*_draws.tsv' -ErrorAction SilentlyContinue | Where-Object { $_.Name -notin $before } | Select-Object -First 1
}
Set-Content $ini (SetKey (Get-Content $ini -Raw) 'Proxy' 'CaptureDraws' '0') -NoNewline
if (-not $new) { "CAPTURE FAILED label=$Label : no frame dump appeared within 20 s (front=$front, game responding=$($game.Responding))"; exit 1 }
Start-Sleep -Milliseconds 500
Copy-Item $new.FullName "$Out\${Label}_draws.tsv" -Force
$lights = $new.FullName -replace '_draws\.tsv$', '_lights.tsv'
if (Test-Path $lights) { Copy-Item $lights "$Out\${Label}_lights.tsv" -Force }
$head = Get-Content "$Out\${Label}_draws.tsv" -TotalCount 1
$draws = (Select-String -Path "$Out\${Label}_draws.tsv" -Pattern '^draw\t').Count
$lightCount = if (Test-Path "$Out\${Label}_lights.tsv") { (Select-String -Path "$Out\${Label}_lights.tsv" -Pattern '^light\t').Count } else { -1 }
"CAPTURED label=$Label front=$front draws=$draws lights=$lightCount shot=$((Get-Item "$Out\$Label.png").Length) bytes | $head"
Start-Sleep -Seconds 3      # let the ini reload settle before the next capture
