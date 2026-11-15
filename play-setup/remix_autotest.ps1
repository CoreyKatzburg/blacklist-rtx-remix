# Automatic Remix test: what actually reaches the screen (ffmpeg ddagrab, 120 fps, lossless), not game-side timings.
# Needs: the game running through Play_Blacklist_with_Remix.ps1, in a level, ray tracing on (AutoRaytracing), Corey not playing.
#   motion: the same scripted camera route (still, slow pan, fast turns, walk, pan back) with ray tracing ON and then OFF;
#           per moving segment: pictures/s on screen, longest freeze, freezes > 33 / 50 ms, flicker (ON vs OFF).
#   views:  8 headings around the player (360 deg), screenshot with ray tracing ON and OFF from the same spot;
#           flags blocks where the game shows structure but Remix shows nothing (missing geometry).
# Output: D:\BlacklistOverhaul\dx11-lighting\shots\remix-autotest\<time>\ (report.txt, clips, views_sheet.png). Exit 1 on FAIL.
# PASS: on-screen >= 55 pictures/s in every moving segment, no freeze > 50 ms, flicker ON <= 1.5 x OFF, missing blocks <= 3 per view.
param([int]$DxPer360 = 0, [switch]$SkipMotion, [switch]$SkipViews, [switch]$Calibrate, [switch]$Launch, [string[]]$OnConfig = @())
$ErrorActionPreference = 'Stop'
$S = "D:\Steam\steamapps\common\Tom Clancy's Splinter Cell Blacklist\src\SYSTEM"
$ini = "$S\d3d9_proxy.ini"
$py = 'D:\BlacklistOverhaul\tools\pyenv\Scripts\python.exe'
$here = $PSScriptRoot
$out = "D:\BlacklistOverhaul\dx11-lighting\shots\remix-autotest\$(Get-Date -Format yyyyMMdd-HHmmss)"; New-Item -ItemType Directory -Force $out | Out-Null
$calFile = "$here\remix_autotest_dx360.txt"
if (-not $DxPer360 -and (Test-Path $calFile)) { $DxPer360 = [int](Get-Content $calFile) }

Add-Type -TypeDefinition @"
using System; using System.Runtime.InteropServices; using System.Threading;
public static class RInp {
  [StructLayout(LayoutKind.Sequential)] struct MOUSEINPUT { public int dx, dy; public uint data, flags, time; public IntPtr extra; }
  [StructLayout(LayoutKind.Sequential)] struct KEYBDINPUT { public ushort vk, scan; public uint flags, time; public IntPtr extra; public uint pad1, pad2; }
  [StructLayout(LayoutKind.Explicit)] struct INPUT { [FieldOffset(0)] public uint type; [FieldOffset(8)] public MOUSEINPUT mi; [FieldOffset(8)] public KEYBDINPUT ki; }
  [DllImport("user32.dll")] static extern uint SendInput(uint n, INPUT[] inputs, int size);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
  [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int cmd);
  [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr h, IntPtr pid);
  [DllImport("user32.dll")] static extern bool AttachThreadInput(uint a, uint b, bool attach);
  [DllImport("kernel32.dll")] static extern uint GetCurrentThreadId();
  public static bool Front(IntPtr h) {
    var fg = GetForegroundWindow(); if (fg == h) return true;
    uint a = GetWindowThreadProcessId(fg, IntPtr.Zero), me = GetCurrentThreadId();
    AttachThreadInput(me, a, true); ShowWindow(h, 9); SetForegroundWindow(h); AttachThreadInput(me, a, false);
    Thread.Sleep(200); return GetForegroundWindow() == h; }
  static void Send(INPUT i) { SendInput(1, new[] { i }, Marshal.SizeOf(typeof(INPUT))); }
  public static void Key(ushort scan, bool down) { var i = new INPUT(); i.type = 1; i.ki.scan = scan; i.ki.flags = 0x8u | (down ? 0u : 0x2u); Send(i); }
  public static void Move(int dx, int dy) { var i = new INPUT(); i.type = 0; i.mi.dx = dx; i.mi.dy = dy; i.mi.flags = 0x1; Send(i); }
  public static void Pan(int dx, int dy, int ms) { int steps = Math.Max(1, ms / 8); double ax = 0, ay = 0; for (int s = 1; s <= steps; ++s) { double tx = (double)dx * s / steps, ty = (double)dy * s / steps; int mx = (int)Math.Round(tx - ax), my = (int)Math.Round(ty - ay); ax += mx; ay += my; Move(mx, my); Thread.Sleep(8); } }
}
"@
function Front { $p = Get-Process Blacklist_game -ErrorAction Stop; [void][RInp]::Front($p.MainWindowHandle); Start-Sleep -Milliseconds 300 }
function SetRt([bool]$on) {
    $c = Get-Content $ini -Raw
    $c = [regex]::Replace($c, '(?m)^AutoRaytracing=\d', "AutoRaytracing=$(if ($on) { 1 } else { 0 })")
    $c = [regex]::Replace($c, '(?m)^rtx\.enableRaytracing\s*=.*\r?\n', '')
    if (-not $on) { $c = $c.Replace('[RemixConfig]', "[RemixConfig]`r`nrtx.enableRaytracing = False") }
    foreach ($line in $OnConfig) {   # extra live Remix settings to try (A/B against an earlier run)
        $key = ($line -split '=')[0].Trim()
        $c = [regex]::Replace($c, '(?m)^' + [regex]::Escape($key) + '\s*=.*\r?\n', '')
        $c = $c.Replace('[RemixConfig]', "[RemixConfig]`r`n$line")
    }
    Set-Content $ini $c -NoNewline
    Start-Sleep -Seconds 4    # ini reload (~1 s) + AutoRaytracing (60 frames) + denoiser settle
}
# Capture source: desktop duplication (ddagrab, 120 fps) delivers NOTHING while the monitor is asleep (seen 2026-11-09 after Corey
# left: ffmpeg hangs waiting for the first frame). GDI (gdigrab) still works then, but only ~30 fps at 2560x1440 / ~55 fps for a
# 1280x720 region. Test once which one works.
$script:UseDda = $null
function DdaWorks {
    if ($null -ne $script:UseDda) { return $script:UseDda }
    $t = "$env:TEMP\dda_probe.mkv"
    $p = Start-Process ffmpeg -ArgumentList '-v','error','-y','-f','lavfi','-i','ddagrab=output_idx=0:framerate=30:draw_mouse=0','-frames:v','2','-c:v','hevc_nvenc',$t -NoNewWindow -PassThru
    $script:UseDda = $p.WaitForExit(6000); if (-not $script:UseDda) { $p.Kill() }
    $script:UseDda
}
function Shot($file) {
    if (DdaWorks) { & ffmpeg -v error -y -f lavfi -i 'ddagrab=output_idx=0:framerate=30:draw_mouse=0' -frames:v 1 -vf 'hwdownload,format=bgra' $file }
    else { & ffmpeg -v error -y -f gdigrab -draw_mouse 0 -i desktop -frames:v 1 $file }
}
function Record($file, [double]$seconds) {
    if (DdaWorks) {   # full resolution, 120 fps, lossless on the GPU encoder
        Start-Process ffmpeg -ArgumentList '-v','error','-y','-f','lavfi','-i','ddagrab=output_idx=0:framerate=120:draw_mouse=0','-t',"$seconds",'-vf','hwdownload,format=bgra,format=bgr0','-c:v','hevc_nvenc','-tune','lossless','-preset','p1',$file -NoNewWindow -PassThru
    } else {          # monitor asleep: GDI, full resolution, ~30 fps (coarser freeze timing)
        Start-Process ffmpeg -ArgumentList '-v','error','-y','-f','gdigrab','-framerate','60','-draw_mouse','0','-i','desktop','-t',"$seconds",'-c:v','hevc_nvenc','-tune','lossless','-preset','p1',$file -NoNewWindow -PassThru
    }
}
$SC_W = 0x11
# Route timeline (seconds): 0-1 still | 1-4 slow pan right | 4.5-5.3 fast 180 right | 6-6.8 fast 180 back | 7.5-9.5 walk + slight pan | 10-13 slow pan left
$segments = 'slow_pan:1.1:3.9,fast_turn:4.55:5.25,fast_turn_back:6.05:6.75,walk:7.6:9.4,slow_pan_back:10.1:12.9'
function Route {
    $half = [int]($DxPer360 / 2)
    Start-Sleep -Milliseconds 1000
    [RInp]::Pan([int]($DxPer360 / 4), 0, 3000); Start-Sleep -Milliseconds 500
    [RInp]::Pan($half, 0, 800); Start-Sleep -Milliseconds 700
    [RInp]::Pan(-$half, 0, 800); Start-Sleep -Milliseconds 700
    [RInp]::Key($SC_W, $true); [RInp]::Pan([int]($DxPer360 / 16), 0, 2000); [RInp]::Key($SC_W, $false); Start-Sleep -Milliseconds 500
    [RInp]::Pan(-[int]($DxPer360 / 4), 0, 3000)
}

if ($Calibrate -or -not $DxPer360) {
    # Turn in small slow steps; the step whose picture matches the start best (after half a turn) closes 360 deg.
    Front; SetRt $false; New-Item -ItemType Directory -Force "$out\cal" | Out-Null
    Shot "$out\cal\c_000.png"; $step = 200
    for ($i = 1; $i -le 40; ++$i) { [RInp]::Pan($step, 0, 400); Start-Sleep -Milliseconds 300; Shot ("$out\cal\c_{0:D3}.png" -f $i) }
    $best = & $py -I -c @"
import glob, numpy as np
from PIL import Image
fs = sorted(glob.glob(r'$out\cal\c_*.png'))
L = lambda p: np.asarray(Image.open(p).convert('L').resize((320, 180))).astype(np.float32)[20:130]
a = L(fs[0]); sc = [(np.abs(L(f) - a).mean(), i) for i, f in enumerate(fs) if i >= 8]
print(min(sc)[1])
"@
    $DxPer360 = [int]$best * $step; Set-Content $calFile $DxPer360
    [RInp]::Pan(-[int]$best * $step, 0, 3000)   # back to the start heading
    SetRt $true
    "calibration: $DxPer360 mouse counts per 360 deg (saved to $calFile)"
    if ($Calibrate) { exit 0 }
}

if ($Launch) {
    # Start through the Play script (installs the Remix setup) and get into the level: skip intros with Space, Enter at the
    # title and at "Resume at checkpoint", until the light publisher sees game lights and AutoRaytracing switches on.
    $log = "$S\d3d9_proxy.log"; $n0 = @(Select-String $log -Pattern 'AutoRaytracing -> on' -ErrorAction SilentlyContinue).Count
    Get-Process Blacklist_game, NvRemixBridge -ErrorAction SilentlyContinue | Stop-Process -Force; Start-Sleep 6
    Start-Process pwsh -ArgumentList '-NoProfile', '-File', "$here\Play_Blacklist_with_Remix.ps1", '-NoPause' -WindowStyle Hidden
    $t = [Diagnostics.Stopwatch]::StartNew()
    while (-not (Get-Process Blacklist_game -ErrorAction SilentlyContinue) -and $t.Elapsed.TotalSeconds -lt 180) { Start-Sleep 2 }
    $gpid = (Get-Process Blacklist_game).Id
    Start-Sleep 12; $k = 0
    while (-not (Select-String $log -Pattern "\[pid $gpid\] lightpub: AutoRaytracing -> on" -Quiet) -and $t.Elapsed.TotalSeconds -lt 300) {
        Front; $sc = if ($k % 3 -eq 2) { 0x1C } else { 0x39 }   # Space, Space, Enter, ...
        [RInp]::Key($sc, $true); Start-Sleep -Milliseconds 100; [RInp]::Key($sc, $false); Start-Sleep -Milliseconds 1600; ++$k
    }
    Start-Sleep 4
    "in level after $([int]$t.Elapsed.TotalSeconds) s"
}

$report = @("remix autotest $(Get-Date -Format s)  dx/360=$DxPer360"); $fail = $false
try {
    Front
    if (-not $SkipMotion) {
        $res = @{}
        foreach ($mode in 'on', 'off') {
            SetRt ($mode -eq 'on'); Front
            $p = Record "$out\route_$mode.mkv" 13.5; Start-Sleep -Milliseconds 300; Route; $p.WaitForExit(30000) | Out-Null
            # moving windows are found from the recording itself (scripted timings drift)
            $rows = @(& $py -I "$here\remix_analyze.py" motion "$out\route_$mode.mkv" auto | ConvertFrom-Json)
            $secs = ($rows | Measure-Object seconds -Sum).Sum
            $pics = ($rows | ForEach-Object { $_.pictures_per_s * $_.seconds } | Measure-Object -Sum).Sum
            $fl = @($rows | Where-Object flicker | ForEach-Object flicker | Sort-Object)
            $res[$mode] = [pscustomobject]@{ seconds = [math]::Round($secs, 1); pps = $(if ($secs) { [math]::Round($pics / $secs, 1) } else { 0 })
                freeze = ($rows | Measure-Object longest_freeze_ms -Maximum).Maximum; f33 = ($rows | Measure-Object freezes_33 -Sum).Sum
                f50 = ($rows | Measure-Object freezes_50 -Sum).Sum; flicker = $(if ($fl.Count) { $fl[[int]($fl.Count / 2)] } else { $null }) }
            $report += ('RT {0,-3}: moving {1} s, {2} pictures/s on screen, longest freeze {3} ms, freezes >33 ms {4}, >50 ms {5}, flicker {6}' -f `
                $mode, $res[$mode].seconds, $res[$mode].pps, $res[$mode].freeze, $res[$mode].f33, $res[$mode].f50, $res[$mode].flicker)
        }
        $r = $res.on; $ratio = if ($res.off.flicker) { [math]::Round($r.flicker / $res.off.flicker, 2) } else { $null }
        $report += "flicker RT on / RT off = $ratio"
        if ($r.pps -lt 55) { $fail = $true; $report += "  FAIL: under 55 pictures/s on screen while moving" }
        if ($r.f50 -gt 0) { $fail = $true; $report += "  FAIL: screen froze > 50 ms while moving ($($r.f50) times)" }
        if ($ratio -and $ratio -gt 1.5) { $fail = $true; $report += "  FAIL: flicker much higher than the game's own image" }
    }
    if (-not $SkipViews) {
        $v = "$out\views"; New-Item -ItemType Directory -Force $v | Out-Null
        # ON and OFF back to back at each heading (separate full turns drift, so the pairs wouldn't match)
        Front
        for ($k = 0; $k -lt 8; ++$k) {
            SetRt $true; Start-Sleep -Milliseconds 1500; Shot "$v\on_$k.png"
            SetRt $false; Shot "$v\off_$k.png"
            Front; [RInp]::Pan([int]($DxPer360 / 8), 0, 800)
        }
        SetRt $true
        foreach ($r in @(& $py -I "$here\remix_analyze.py" views $v | ConvertFrom-Json)) {
            $report += ('view {0}: missing-geometry blocks {1} of {2}' -f $r.view, $r.missing_blocks, $r.blocks)
            if ($r.missing_blocks -gt 3) { $fail = $true; $report += "  FAIL: Remix is missing geometry the game draws (see views\views_sheet.png)" }
        }
    }
}
finally { SetRt $true }
$report[0] += $(if ($fail) { '  FAIL' } else { '  PASS' })
Set-Content "$out\report.txt" $report
$report; "output: $out"
if ($fail) { exit 1 }
