# Diagnostic for the "Sam's body is missing" bug. Run it while the body is missing, hands off.
# It changes one Remix setting at a time through the GAME folder's d3d9_proxy.ini (re-read live), waits, and takes a
# screenshot named probe_NN_<step>.png, so the pictures show which change brings the body back.
# The ini is put back at the end. Prints one line per step, then PROBE DONE.
#   & "$P\sam_probe.ps1" -Out <folder>
param([Parameter(Mandatory)][string]$Out)
$S = "D:\Steam\steamapps\common\Tom Clancy's Splinter Cell Blacklist\src\SYSTEM"
$ini = "$S\d3d9_proxy.ini"
if (-not (Get-Process Blacklist_game -ErrorAction SilentlyContinue)) { 'PROBE FAILED: Blacklist_game is not running'; exit 1 }
New-Item -ItemType Directory -Force $Out | Out-Null
function SetKey([string]$text, [string]$section, [string]$key, [string]$value) {
    $pattern = '(?m)^' + [regex]::Escape($key) + '\s*=.*$'
    $line = if ($section -eq 'RemixConfig') { "$key = $value" } else { "$key=$value" }
    if ($text -match $pattern) { return [regex]::Replace($text, $pattern, $line) }
    return $text.Replace("[$section]", "[$section]`r`n$line")
}
# name, then 'Section:key=value' items. Each step states every key, so a step also undoes the one before it.
$base = [ordered]@{
    'Lights:AutoRaytracing' = '1'; 'RemixConfig:rtx.enableRaytracing' = 'True'; 'RemixConfig:rtx.enableCulling' = 'True'
    'RemixConfig:rtx.debugView.debugViewIdx' = '0'; 'RemixConfig:rtx.opacityMicromap.enable' = 'True'
    'RemixConfig:rtx.maxPrimsInMergedBLAS' = '50000'; 'RemixConfig:rtx.minPrimsInDynamicBLAS' = '1000'
    'RemixConfig:rtx.uniqueObjectDistance' = '1000'
}
$steps = @(
    @('00_before', @{}),
    @('01_culling_off', @{ 'RemixConfig:rtx.enableCulling' = 'False' }),
    @('02_culling_on', @{}),
    @('03_view_geometry_hash', @{ 'RemixConfig:rtx.debugView.debugViewIdx' = '277' }),
    @('04_view_primitive_index', @{ 'RemixConfig:rtx.debugView.debugViewIdx' = '1' }),
    @('05_view_off', @{}),
    @('06_micromaps_off', @{ 'RemixConfig:rtx.opacityMicromap.enable' = 'False' }),
    @('07_micromaps_on', @{}),
    @('08_object_distance_300', @{ 'RemixConfig:rtx.uniqueObjectDistance' = '300' }),
    @('09_object_distance_1000', @{}),
    @('10_own_blas_per_mesh', @{ 'RemixConfig:rtx.maxPrimsInMergedBLAS' = '99'; 'RemixConfig:rtx.minPrimsInDynamicBLAS' = '100' }),
    @('11_merged_blas_again', @{}),
    @('12_raytracing_off', @{ 'Lights:AutoRaytracing' = '0'; 'RemixConfig:rtx.enableRaytracing' = 'False' }),
    @('13_raytracing_on', @{})
)
$original = Get-Content $ini -Raw
try {
    foreach ($step in $steps) {
        $name = $step[0]; $changes = $step[1]
        $text = Get-Content $ini -Raw
        foreach ($key in $base.Keys) {
            $value = if ($changes.ContainsKey($key)) { $changes[$key] } else { $base[$key] }
            $section, $k = $key.Split(':', 2)
            $text = SetKey $text $section $k $value
        }
        Set-Content $ini $text -NoNewline
        Start-Sleep -Seconds 6
        & 'd:\ffmpeg\bin\ffmpeg.exe' -v error -y -f gdigrab -draw_mouse 0 -i desktop -frames:v 1 "$Out\probe_$name.png"
        "PROBE $name $(Get-Date -Format HH:mm:ss) " + (($changes.Keys | ForEach-Object { "$_=$($changes[$_])" }) -join ' ')
    }
}
finally { Set-Content $ini $original -NoNewline }
'PROBE DONE'
