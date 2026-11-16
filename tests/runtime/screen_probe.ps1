# Black-screen probe (coordinator, Nov 16): the no-Remix reference runs 045456 and 054905 grabbed only black while the
# game drew normally. The game window covers the only monitor, so no desktop shows beside it. This floats a small
# window of one known colour over the bottom-right corner, never focused (the game keeps its input), grabs the whole
# desktop once with the same gdigrab drive.py uses, and closes it. drive.py reads the raw grab: the marker colour there
# and the game black means the game picture is lost; both black means the display or session delivers nothing.
param([Parameter(Mandatory)][string]$Out, [int]$Size = 160, [string]$Ffmpeg = 'd:\ffmpeg\bin\ffmpeg.exe')
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms, System.Drawing
Add-Type -ReferencedAssemblies System.Windows.Forms, System.Windows.Forms.Primitives, System.Drawing, `
    System.Drawing.Primitives, System.ComponentModel.Primitives -TypeDefinition @'
using System.Drawing; using System.Windows.Forms;
public class ScreenProbeMarker : Form {
    public ScreenProbeMarker(Rectangle r) {
        FormBorderStyle = FormBorderStyle.None; ShowInTaskbar = false; TopMost = true;
        StartPosition = FormStartPosition.Manual; Bounds = r; BackColor = Color.FromArgb(200, 40, 160);
    }
    protected override bool ShowWithoutActivation { get { return true; } }
    protected override CreateParams CreateParams {   // WS_EX_NOACTIVATE | WS_EX_TOPMOST | WS_EX_TOOLWINDOW
        get { CreateParams cp = base.CreateParams; cp.ExStyle |= 0x08000000 | 0x00000008 | 0x00000080; return cp; } }
}
'@
$screen = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds
$rect = [System.Drawing.Rectangle]::new($screen.Right - $Size - 40, $screen.Bottom - $Size - 80, $Size, $Size)
$marker = [ScreenProbeMarker]::new($rect)
try {
    $marker.Show()
    $until = (Get-Date).AddMilliseconds(800)
    while ((Get-Date) -lt $until) { [System.Windows.Forms.Application]::DoEvents(); Start-Sleep -Milliseconds 20 }
    & $Ffmpeg -v error -y -f gdigrab -framerate 10 -draw_mouse 0 -offset_x $screen.X -offset_y $screen.Y `
        -video_size "$($screen.Width)x$($screen.Height)" -i desktop -frames:v 1 -pix_fmt rgb24 -f rawvideo $Out
    if ($LASTEXITCODE) { throw "ffmpeg exited $LASTEXITCODE" }
} finally { $marker.Close(); $marker.Dispose() }
# one line of JSON for drive.py: the grab's size and where the marker was, in grab pixels
@{ width = $screen.Width; height = $screen.Height; marker = @(($rect.X - $screen.X), ($rect.Y - $screen.Y), $Size, $Size)
   colour = @(200, 40, 160) } | ConvertTo-Json -Compress
