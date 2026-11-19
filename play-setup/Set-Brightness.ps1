# Switch the brightness look in play-setup\rtx.conf. The Play script copies it into the game at the next launch.
# Switch only while the game is closed.
#   New  Look 1: auto exposure off, local tone map exposure 0.35   <- installed choice
#   Old  the look before Nov 19: auto exposure on, exposure 0.75
# Only those two keys change. The frame-rate option, the debug recorder and the quit fix are left alone.
param([string]$Option = '')

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$conf = Join-Path $here 'rtx.conf'
$options = [ordered]@{
    NEW = @{ auto = 'False'; exposure = '0.35'; label = 'New: auto exposure off, exposure 0.35' }
    OLD = @{ auto = 'True'; exposure = '0.75'; label = 'Old: auto exposure on, exposure 0.75' }
}

if (-not $Option) { $Option = Read-Host 'Brightness look (New or Old)' }
$key = $Option.Trim().ToUpperInvariant()
if (-not $options.Contains($key)) {
    Write-Host "Unknown option '$Option'. Use New or Old. Nothing was changed."
    exit 2
}
if (Get-Process Blacklist_game -ErrorAction SilentlyContinue) {
    Write-Host 'The game is running. Close it first, then switch. Nothing was changed.'
    exit 3
}

$text = [IO.File]::ReadAllText($conf)
$a = [regex]'(?m)^rtx\.autoExposure\.enabled = (True|False)(?=\r?$)'
$e = [regex]'(?m)^rtx\.localtonemap\.exposure = [0-9.]+(?=\r?$)'
if ($a.Matches($text).Count -ne 1 -or $e.Matches($text).Count -ne 1) {
    Write-Host 'rtx.conf does not hold exactly one rtx.autoExposure.enabled line and one rtx.localtonemap.exposure line. Nothing was changed.'
    exit 4
}
$o = $options[$key]
$text = $a.Replace($text, "rtx.autoExposure.enabled = $($o.auto)")
$text = $e.Replace($text, "rtx.localtonemap.exposure = $($o.exposure)")
[IO.File]::WriteAllText($conf, $text, (New-Object Text.UTF8Encoding $false))

Write-Host "Set $($o.label): rtx.autoExposure.enabled = $($o.auto), rtx.localtonemap.exposure = $($o.exposure)"
Write-Host 'Start the game with "Play Blacklist with Remix" as usual; the new look applies from that launch.'
