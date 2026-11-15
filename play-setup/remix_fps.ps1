# Frame-rate A/B for Remix settings (game running with the Play script, in a level, camera still).
# Each config is a list of "rtx.option = value" lines put into the GAME folder's d3d9_proxy.ini [RemixConfig]
# (live), then frame times from d3d9_proxy_frames.csv (column 3, ms) are averaged over -Seconds.
# Usage: remix_fps.ps1 -Configs @{ perf = @('rtx.qualityDLSS = 1'); ultra = @('rtx.qualityDLSS = 0') } [-Seconds 8]
param([System.Collections.IDictionary]$Configs, [int]$Seconds = 8)
$S = "D:\Steam\steamapps\common\Tom Clancy's Splinter Cell Blacklist\src\SYSTEM"
$ini = "$S\d3d9_proxy.ini"; $csv = "$S\d3d9_proxy_frames.csv"
$orig = Get-Content $ini -Raw
try {
    foreach ($name in $Configs.Keys) {
        $text = $orig
        foreach ($line in $Configs[$name]) {
            $key = ($line -split '=')[0].Trim()
            $text = [regex]::Replace($text, "(?m)^" + [regex]::Escape($key) + "\s*=.*$", '')
            $text = $text.Replace('[RemixConfig]', "[RemixConfig]`r`n$line")
        }
        Set-Content $ini $text -NoNewline
        Start-Sleep -Seconds 3   # reload + settle
        $start = (Get-Content $csv -Tail 1).Split(',')[0]
        Start-Sleep -Seconds $Seconds
        $rows = Get-Content $csv -Tail 3000 | Where-Object { $_ -match '^\d' -and [long]($_.Split(',')[0]) -gt [long]$start }
        $ms = @($rows | ForEach-Object { [double]($_.Split(',')[2]) } | Where-Object { $_ -gt 0 })
        $s = $ms | Sort-Object
        if ($ms.Count) { '{0,-14} {1,5:N1} fps  avg {2:N2} ms  p99 {3:N1} ms  ({4} frames)' -f $name, (1000 / ($ms | Measure-Object -Average).Average), ($ms | Measure-Object -Average).Average, $s[[int]($s.Count * 0.99)], $ms.Count }
    }
}
finally { Set-Content $ini $orig -NoNewline }
