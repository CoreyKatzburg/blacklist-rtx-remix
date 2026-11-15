# Sends keys and mouse turns to Blacklist_game, optionally while recording the screen (gdigrab: works with monitors off).
# usage (inside PowerShell, with &):
#   & "$P\rca_keys.ps1" -Do 'tap:F','wait:3000'
#   & "$P\rca_keys.ps1" -Do 'hold:W:3000','tap:C','hold:W:3000' -Video "$OUT\walk_rt.mp4" -Seconds 12
# Actions: tap:<key>  hold:<key>:<ms>  wait:<ms>  turn:<mouse counts, + = right; about 0.024 deg per count>
#          move:<x>:<y>  moves the free cursor to screen pixel (x,y) on the primary monitor, no click (for hovering)
#          click:<x>:<y> moves to (x,y), then one left click (down, 50 ms, up); use for world-map markers
# Keys: W A S D C F X Q Space Esc Enter Up Down
param([string[]]$Do = @(), [string]$Video = '', [int]$Seconds = 10)
Add-Type -TypeDefinition @"
using System; using System.Runtime.InteropServices; using System.Threading;
public static class RcaInput {
  [StructLayout(LayoutKind.Sequential)] struct MOUSEINPUT { public int dx, dy; public uint data, flags, time; public IntPtr extra; }
  [StructLayout(LayoutKind.Sequential)] struct KEYBDINPUT { public ushort vk, scan; public uint flags, time; public IntPtr extra; public uint pad1, pad2; }
  [StructLayout(LayoutKind.Explicit)] struct INPUT { [FieldOffset(0)] public uint type; [FieldOffset(8)] public MOUSEINPUT mi; [FieldOffset(8)] public KEYBDINPUT ki; }
  [DllImport("user32.dll")] static extern uint SendInput(uint n, INPUT[] inputs, int size);
  [DllImport("user32.dll")] static extern bool SetForegroundWindow(IntPtr h);
  [DllImport("user32.dll")] static extern IntPtr GetForegroundWindow();
  [DllImport("user32.dll")] static extern bool ShowWindow(IntPtr h, int cmd);
  [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr h, IntPtr pid);
  [DllImport("user32.dll")] static extern bool AttachThreadInput(uint a, uint b, bool attach);
  [DllImport("kernel32.dll")] static extern uint GetCurrentThreadId();
  [DllImport("user32.dll")] static extern int GetSystemMetrics(int n);
  public static bool Front(IntPtr h) {
    var fg = GetForegroundWindow(); if (fg == h) return true;
    uint a = GetWindowThreadProcessId(fg, IntPtr.Zero), me = GetCurrentThreadId();
    AttachThreadInput(me, a, true); ShowWindow(h, 9); SetForegroundWindow(h); AttachThreadInput(me, a, false);
    Thread.Sleep(200); return GetForegroundWindow() == h; }
  static void Send(INPUT i) { SendInput(1, new[] { i }, Marshal.SizeOf(typeof(INPUT))); }
  public static void Key(ushort scan, bool down, bool ext) {
    var i = new INPUT(); i.type = 1; i.ki.scan = scan; i.ki.flags = 0x8u | (down ? 0u : 0x2u) | (ext ? 0x1u : 0u); Send(i); }
  public static void Turn(int counts) {
    int step = counts > 0 ? 20 : -20;
    for (int done = 0; Math.Abs(done) < Math.Abs(counts); done += step) {
      var i = new INPUT(); i.type = 0; i.mi.dx = Math.Abs(counts - done) < 20 ? counts - done : step; i.mi.flags = 0x1; Send(i); Thread.Sleep(10); } }
  // Absolute move to a primary-monitor pixel. Coordinates are normalised to 0..65535 over (size - 1).
  public static void MoveTo(int x, int y) {
    int w = GetSystemMetrics(0), h = GetSystemMetrics(1);
    var i = new INPUT(); i.type = 0;
    i.mi.dx = (int)Math.Round(x * 65535.0 / (w - 1)); i.mi.dy = (int)Math.Round(y * 65535.0 / (h - 1));
    i.mi.flags = 0x8001; // MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE
    Send(i); }
  public static void Click(int x, int y) {
    MoveTo(x, y); Thread.Sleep(50);
    var d = new INPUT(); d.type = 0; d.mi.flags = 0x2; Send(d); // MOUSEEVENTF_LEFTDOWN
    Thread.Sleep(50);
    var u = new INPUT(); u.type = 0; u.mi.flags = 0x4; Send(u); } // MOUSEEVENTF_LEFTUP
}
"@
$scan = @{ W = 0x11; A = 0x1E; S = 0x1F; D = 0x20; C = 0x2E; F = 0x21; X = 0x2D; Q = 0x10; Space = 0x39; Esc = 0x01; Enter = 0x1C; Up = 0x48; Down = 0x50 }
$extended = @('Up', 'Down')
$game = Get-Process Blacklist_game -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $game) { 'KEYS FAILED: Blacklist_game is not running'; exit 1 }
if (-not [RcaInput]::Front($game.MainWindowHandle)) { 'WARNING: game window did not come to the front' }
Start-Sleep -Milliseconds 400
$recorder = $null
if ($Video) {
    $recorder = Start-Process 'd:\ffmpeg\bin\ffmpeg.exe' -PassThru -WindowStyle Hidden -RedirectStandardError "$Video.stderr.txt" `
        -ArgumentList "-v error -y -f gdigrab -framerate 30 -draw_mouse 0 -t $Seconds -i desktop -c:v libx264 -preset ultrafast -pix_fmt yuv420p `"$Video`""
    Start-Sleep -Milliseconds 800
}
foreach ($action in $Do) {
    $part = $action -split ':'
    switch ($part[0]) {
        'tap'  { [RcaInput]::Key($scan[$part[1]], $true, $extended -contains $part[1]); Start-Sleep -Milliseconds 120
                 [RcaInput]::Key($scan[$part[1]], $false, $extended -contains $part[1]) }
        'hold' { [RcaInput]::Key($scan[$part[1]], $true, $extended -contains $part[1]); Start-Sleep -Milliseconds ([int]$part[2])
                 [RcaInput]::Key($scan[$part[1]], $false, $extended -contains $part[1]) }
        'wait' { Start-Sleep -Milliseconds ([int]$part[1]) }
        'turn' { [RcaInput]::Turn([int]$part[1]) }
        'move' { [RcaInput]::MoveTo([int]$part[1], [int]$part[2]) }
        'click' { [RcaInput]::Click([int]$part[1], [int]$part[2]) }
        default { "unknown action $action" }
    }
    "{0:HH:mm:ss.fff} $action" -f (Get-Date)
}
if ($recorder) {
    if (-not $recorder.WaitForExit(($Seconds + 15) * 1000)) { $recorder.Kill(); 'VIDEO TIMED OUT (ffmpeg stopped)' }
    if (Test-Path $Video) { "VIDEO $Video $((Get-Item $Video).Length) bytes" } else { 'VIDEO FAILED: no file' }
}
'KEYS DONE'
