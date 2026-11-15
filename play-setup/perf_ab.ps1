# Frame-time A/B of proxy and Remix switches in the running game (started with the Play script, in gameplay, hands off).
# For each case: write the keys into the GAME folder's d3d9_proxy.ini (re-read live), wait, take a screenshot, measure
# -Seconds with the camera still, then -Seconds while the camera pans right and back left (rca_keys.ps1 turn).
# Frame times come from d3d9_proxy_frames.csv (column 3, ms), call times from d3d9_proxy_calls.csv.
# The ini is put back as it was at the end. Prints one line per case and measurement; ends with PERF DONE.
#   & "$P\perf_ab.ps1" -Out $OUT [-Seconds 10] [-Cases base,lighting0,base2,omm0,omm1] [-StillOnly]
# Every case starts from the same defaults ($defaults) and changes only the keys it names, so the order does not matter.
param([Parameter(Mandatory)][string]$Out, [int]$Seconds = 10, [string[]]$Cases = @('base', 'lighting0', 'base2', 'omm0', 'omm1'), [switch]$StillOnly)
$S = "D:\Steam\steamapps\common\Tom Clancy's Splinter Cell Blacklist\src\SYSTEM"
$ini = "$S\d3d9_proxy.ini"; $frames = "$S\d3d9_proxy_frames.csv"; $calls = "$S\d3d9_proxy_calls.csv"
$keys = Join-Path $PSScriptRoot 'rca_keys.ps1'
# 'Section:key' = value. A [RemixConfig] value stays in Remix once sent, so every case writes all of them.
$defaults = [ordered]@{
    'Proxy:SkipGameLighting' = '1'; 'Proxy:SkipGamePost' = '1'; 'Lights:AutoRaytracing' = '1'
    'RemixConfig:rtx.maxPrimsInMergedBLAS' = '50000'; 'RemixConfig:rtx.minPrimsInDynamicBLAS' = '1000'
    'RemixConfig:rtx.enableRaytracing' = 'True'; 'RemixConfig:rtx.opacityMicromap.enable' = 'True'
    'RemixConfig:rtx.qualityDLSS' = '0'; 'RemixConfig:rtx.upscalerType' = '1'; 'RemixConfig:rtx.resolutionScale' = '0.333'
    'RemixConfig:rtx.enableRayReconstruction' = 'True'; 'RemixConfig:rtx.integrateIndirectMode' = '2'
    'RemixConfig:rtx.useRTXDI' = 'True'; 'RemixConfig:rtx.volumetrics.enable' = 'True'
    'RemixConfig:rtx.enableSecondaryBounces' = 'True'; 'RemixConfig:rtx.pathMaxBounces' = '2'
}
$known = @{
    base = @{}; base2 = @{}; base3 = @{}
    lighting0 = @{ 'Proxy:SkipGameLighting' = '0' }
    omm0      = @{ 'RemixConfig:rtx.opacityMicromap.enable' = 'False' }
    omm1      = @{}
    dlssperf  = @{ 'RemixConfig:rtx.qualityDLSS' = '1' }
    dlssultra = @{}
    # Ray tracing off: the game's own picture (the proxy stops skipping the lighting by itself).
    rtoff        = @{ 'Lights:AutoRaytracing' = '0'; 'RemixConfig:rtx.enableRaytracing' = 'False' }
    rtoff_light0 = @{ 'Lights:AutoRaytracing' = '0'; 'RemixConfig:rtx.enableRaytracing' = 'False'; 'Proxy:SkipGameLighting' = '0' }
    bounce1   = @{ 'RemixConfig:rtx.pathMaxBounces' = '1' }
    nobounce  = @{ 'RemixConfig:rtx.enableSecondaryBounces' = 'False' }
    novol     = @{ 'RemixConfig:rtx.volumetrics.enable' = 'False' }
    nortxdi   = @{ 'RemixConfig:rtx.useRTXDI' = 'False' }
    restirgi  = @{ 'RemixConfig:rtx.integrateIndirectMode' = '1' }      # ReSTIR GI instead of the neural radiance cache
    post0     = @{ 'Proxy:SkipGamePost' = '0' }                          # draw the game's full-size post passes again
    # Every mesh of 1000+ (or 100+) triangles gets its own acceleration structure instead of the merged one.
    dynblas    = @{ 'RemixConfig:rtx.maxPrimsInMergedBLAS' = '999' }
    dynblas100 = @{ 'RemixConfig:rtx.maxPrimsInMergedBLAS' = '99'; 'RemixConfig:rtx.minPrimsInDynamicBLAS' = '100' }
    rroff     = @{ 'RemixConfig:rtx.enableRayReconstruction' = 'False' } # DLSS upscaling + the classic denoisers
    taau      = @{ 'RemixConfig:rtx.upscalerType' = '3' }                # no DLSS at all: TAA upscale from the same 853x480
}
if (-not (Get-Process Blacklist_game -ErrorAction SilentlyContinue)) { 'PERF FAILED: Blacklist_game is not running'; exit 1 }
New-Item -ItemType Directory -Force $Out | Out-Null
function SetKey([string]$text, [string]$section, [string]$key, [string]$value) {
    $pattern = '(?m)^' + [regex]::Escape($key) + '\s*=.*$'
    $line = if ($section -eq 'RemixConfig') { "$key = $value" } else { "$key=$value" }
    if ($text -match $pattern) { return [regex]::Replace($text, $pattern, $line) }
    return $text.Replace("[$section]", "[$section]`r`n$line")
}
function LastFrame { [long]((Get-Content $frames -Tail 1).Split(',')[0]) }
function Measure-Frames([string]$name, [long]$from, [long]$to) {
    $ms = @(); $draws = @()
    foreach ($row in (Get-Content $frames -Tail 6000)) {
        $c = $row.Split(',', 6)
        if ($c.Count -lt 5 -or $c[0] -notmatch '^\d+$') { continue }
        $n = [long]$c[0]
        if ($n -le $from -or $n -gt $to) { continue }
        $ms += [double]$c[2]; $draws += [double]$c[4]
    }
    $sum = @{}
    foreach ($row in (Get-Content $calls -Tail 6000)) {
        $c = $row.Split(',')
        if ($c[0] -notmatch '^\d+$') { continue }
        $n = [long]$c[0]
        if ($n -le $from -or $n -gt $to) { continue }
        foreach ($item in $c[1..($c.Count - 1)]) {
            if ($item -match '^(\w+)=\d+:([\d.]+)$') { $sum[$Matches[1]] += [double]$Matches[2] }
        }
    }
    if (-not $ms.Count) { "$name  NO FRAMES"; return }
    $sorted = $ms | Sort-Object
    $avg = ($ms | Measure-Object -Average).Average
    $callText = ($sum.Keys | Sort-Object | ForEach-Object { '{0} {1:N2}' -f $_, ($sum[$_] / $ms.Count) }) -join ', '
    '{0,-18} {1,5:N1} fps  avg {2,6:N2} ms  p50 {3,6:N2}  p99 {4,6:N1}  max {5,6:N1}  frames {6,4}  draws {7,5:N0}  | per-frame call ms: {8}' -f `
        $name, (1000 / $avg), $avg, $sorted[[int]($sorted.Count * 0.5)], $sorted[[Math]::Min($sorted.Count - 1, [int]($sorted.Count * 0.99))],
        $sorted[-1], $ms.Count, ($draws | Measure-Object -Average).Average, $callText
}
$original = Get-Content $ini -Raw
try {
    foreach ($case in $Cases) {
        if (-not $known.ContainsKey($case)) { "PERF: unknown case '$case' skipped"; continue }
        $text = Get-Content $ini -Raw
        foreach ($key in $defaults.Keys) {
            $value = if ($known[$case].ContainsKey($key)) { $known[$case][$key] } else { $defaults[$key] }
            $section, $name = $key.Split(':', 2)
            $text = SetKey $text $section $name $value
        }
        Set-Content $ini $text -NoNewline
        Start-Sleep -Seconds 7      # ini reload (lights are recreated) and the picture settles
        & 'd:\ffmpeg\bin\ffmpeg.exe' -v error -y -f gdigrab -draw_mouse 0 -i desktop -frames:v 1 "$Out\perf_$case.png"
        $from = LastFrame; Start-Sleep -Seconds $Seconds; $to = LastFrame
        Measure-Frames "$case still" $from $to
        if ($StillOnly) { continue }
        $from = LastFrame
        $turns = @(); for ($i = 0; $i -lt [Math]::Max(1, [int]($Seconds / 3.2)); $i++) { $turns += 'turn:3000', 'turn:-3000' }
        & $keys -Do $turns | Out-Null
        $to = LastFrame
        Measure-Frames "$case panning" $from $to
    }
}
finally {
    # Send the defaults once more, so Remix is not left on the last case's values, then put the file back.
    $text = Get-Content $ini -Raw
    foreach ($key in $defaults.Keys) { $section, $name = $key.Split(':', 2); $text = SetKey $text $section $name $defaults[$key] }
    Set-Content $ini $text -NoNewline
    Start-Sleep -Seconds 4
    Set-Content $ini $original -NoNewline
}
'PERF DONE'
