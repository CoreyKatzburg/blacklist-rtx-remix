# Switch the frame-rate option in play-setup\rtx.conf. The Play script copies it into the game at the next launch.
# Switch only while the game is closed: several picture-size changes in one game session once left the game at 12 FPS.
#   Today  1707x960 Transformer  (rtx.qualityDLSS 3, model 1)  about 29 FPS
#   A      1280x720 CNN          (rtx.qualityDLSS 1, model 0)  about 52 FPS   <- installed choice
#   B      853x480  CNN          (rtx.qualityDLSS 0, model 0)  about 66 FPS, softer picture
#   D      1280x720 Transformer  (rtx.qualityDLSS 1, model 1)  about 41 FPS, sharpest of the fast ones
# Only those two keys change. The debug recorder stays off and the quit fix stays pinned in every option.
param([string]$Option = '')

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$conf = Join-Path $here 'rtx.conf'
$ini = Join-Path $here 'd3d9_proxy.ini'
$options = [ordered]@{
    TODAY = @{ quality = 3; model = 1; label = 'Today: 1707x960, Transformer' }
    A     = @{ quality = 1; model = 0; label = 'A: 1280x720, CNN' }
    B     = @{ quality = 0; model = 0; label = 'B: 853x480, CNN' }
    D     = @{ quality = 1; model = 1; label = 'D: 1280x720, Transformer' }
}

if (-not $Option) { $Option = Read-Host 'Frame-rate option (Today, A, B or D)' }
$key = $Option.Trim().ToUpperInvariant()
if (-not $options.Contains($key)) {
    Write-Host "Unknown option '$Option'. Use one of: Today, A, B, D. Nothing was changed."
    exit 2
}
if (Get-Process Blacklist_game -ErrorAction SilentlyContinue) {
    Write-Host 'The game is running. Close it first, then switch. Nothing was changed.'
    exit 3
}

$text = [IO.File]::ReadAllText($conf)
$q = [regex]'(?m)^rtx\.qualityDLSS = \d+(?=\r?$)'
$m = [regex]'(?m)^rtx\.rayreconstruction\.model = \d+(?=\r?$)'
if ($q.Matches($text).Count -ne 1 -or $m.Matches($text).Count -ne 1) {
    Write-Host 'rtx.conf does not hold exactly one rtx.qualityDLSS line and one rtx.rayreconstruction.model line. Nothing was changed.'
    exit 4
}
$o = $options[$key]
$text = $q.Replace($text, "rtx.qualityDLSS = $($o.quality)")
$text = $m.Replace($text, "rtx.rayreconstruction.model = $($o.model)")
[IO.File]::WriteAllText($conf, $text, (New-Object Text.UTF8Encoding $false))

Write-Host "Set $($o.label): rtx.qualityDLSS = $($o.quality), rtx.rayreconstruction.model = $($o.model)"
$quit = [regex]::IsMatch($text, '(?m)^rtx\.swapchainRecreateWaitIdle = True(?=\r?$)')
$recorderOff = [regex]::IsMatch([IO.File]::ReadAllText($ini), '(?m)^EnableDebuggingTelemetry=0(?=\r?$)')
Write-Host "Quit fix pinned: $(if ($quit) { 'yes' } else { 'NO, tell the team' }). Debug recorder off: $(if ($recorderOff) { 'yes' } else { 'NO, tell the team' })."
Write-Host 'Start the game with "Play Blacklist with Remix" as usual; the new option applies from that launch.'
