$ErrorActionPreference = 'Stop'
$src = 'D:\BlacklistOverhaul\tools\remix\tests\verify_runtime.ps1'
$ast = [Management.Automation.Language.Parser]::ParseFile($src, [ref]$null, [ref]$null)
foreach ($fn in $ast.FindAll({ $args[0] -is [Management.Automation.Language.FunctionDefinitionAst] -and $args[0].Name -in 'Get-RunSpots', 'Select-Reference' }, $true)) {
    . ([scriptblock]::Create($fn.Extent.Text))
}
$ResultsRoot = Join-Path ([IO.Path]::GetTempPath()) ("refpick-" + [guid]::NewGuid())
function Make($name, $spots, $finished, $errors = @{}, $black = @()) {
    $d = Join-Path $ResultsRoot $name; New-Item -ItemType Directory $d | Out-Null
    @{ spots = $spots } | ConvertTo-Json | Set-Content (Join-Path $d 'manifest.json')
    if ($finished) { '{}' | Set-Content (Join-Path $d 'result.json') }
    foreach ($s in $spots) { New-Item -ItemType Directory (Join-Path $d "$s\gate") | Out-Null
        @{ name = $s; error = $errors[$s] } | ConvertTo-Json | Set-Content (Join-Path $d "$s\spot.json")
        'keep' | Set-Content (Join-Path $d "$s\data.txt")
        $luma = if ($s -in $black) { 0 } else { 0.12 }   # one black stop is enough to make the spot unusable
        @{ luma = @(0.1, 0.11, 0.12) } | ConvertTo-Json | Set-Content (Join-Path $d "$s\gate\y0_p0_raster.json")
        @{ luma = @($luma, $luma, $luma) } | ConvertTo-Json | Set-Content (Join-Path $d "$s\gate\y45_p0_raster.json") }
    $d
}
$fails = 0
function Check($what, $ok) { if ($ok) { "[PASS] $what" } else { "[FAIL] $what"; $script:fails++ } }
try {
    $old = Make 'reference-20261101-000000' @('a', 'b') $true
    $newBroken = Make 'reference-20261102-000000' @('a', 'b') $true @{ b = 'calibration failed' }
    $unfinished = Make 'reference-20261103-000000' @('a', 'b') $false
    $run = Make '20261104-000000' @('a', 'b') $false
    $got = Select-Reference $run '20261104-000000'
    Check 'mixed sources give the merged folder' ($got -eq (Join-Path $run 'reference-chosen'))
    $m = Get-Content (Join-Path $got 'manifest.json') -Raw | ConvertFrom-Json
    Check 'a from the newest finished reference, not the unfinished one' ($m.chosenFrom.a -eq $newBroken)
    Check 'b skips the newer reference whose b failed' ($m.chosenFrom.b -eq $old)
    Check 'the junction shows the reference data' ((Get-Content (Join-Path $got 'b\data.txt')) -eq 'keep')
    $again = Select-Reference $run '20261104-000000'
    Check 'a second Gate keeps the reference data' ((Test-Path (Join-Path $old 'b\data.txt')) -and (Test-Path (Join-Path $again 'b\data.txt')))
    $run2 = Make '20261105-000000' @('a') $false
    Check 'one source gives that run itself' ((Select-Reference $run2 '20261105-000000') -eq $newBroken)
    $run3 = Make '20261100-000000' @('a') $false
    Check 'no earlier reference gives none' ($null -eq (Select-Reference $run3 '20261100-000000'))
    # Nov 16: the newest no-Remix runs grabbed only black frames and were picked as the reference
    $blackRef = Make 'reference-20261106-000000' @('a', 'b') $true @{} @('a')
    $run4 = Make '20261107-000000' @('a', 'b') $false
    $m4 = Get-Content (Join-Path (Select-Reference $run4 '20261107-000000') 'manifest.json') -Raw | ConvertFrom-Json
    Check 'a skips the newer reference whose pictures are black' ($m4.chosenFrom.a -eq $newBroken)
    Check 'b still takes that newer reference' ($m4.chosenFrom.b -eq $blackRef)
} finally {
    Get-ChildItem $ResultsRoot -Recurse -Directory | Where-Object { $_.LinkType } | ForEach-Object { [IO.Directory]::Delete($_.FullName, $false) }
    Remove-Item $ResultsRoot -Recurse -Force
}
if ($fails) { exit 1 }
