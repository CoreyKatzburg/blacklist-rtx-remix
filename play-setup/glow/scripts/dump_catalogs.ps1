# Dump every LeadMaterial / LeadTexture name the Mesh Explorer reader can see, via its private
# Materials.ReadCatalog(string umdPath). Read-only: the reader opens archives with FileAccess.Read.
param(
    [string]$ReaderPath = 'D:\BlacklistOverhaul\tools\tools\BlacklistMeshExplorer\BlacklistMeshExplorer-v0.41.exe',
    [string]$UmdDir = "D:\Steam\steamapps\common\Tom Clancy's Splinter Cell Blacklist\src\SYSTEM\UMDs",
    [Parameter(Mandatory)] [string]$OutJson,
    [int]$Limit = 0
)
$ErrorActionPreference = 'Stop'
$expected = '986EF75907EFEB1EF81C41DCC5429BDCBACD59C9DFC4761A989DAF66911012CA'
if ((Get-FileHash -LiteralPath $ReaderPath -Algorithm SHA256).Hash -ne $expected) { throw 'Unexpected reader build.' }
$asm = [Reflection.Assembly]::LoadFrom($ReaderPath)
$flags = [Reflection.BindingFlags]'Public,NonPublic,Static,Instance'
$mat = $asm.GetType('BlacklistExplorer.Materials')
$read = $mat.GetMethod('ReadCatalog', $flags)
if (-not $read) { throw 'Materials.ReadCatalog not found' }
$fieldsType = $asm.GetType('BlacklistExplorer.Fields')

function Convert-Value($v, [int]$depth) {
    if ($null -eq $v) { return $null }
    if ($v -is [byte[]]) { return @{ t = 'b'; n = $v.Length; x = [Convert]::ToHexString($v, 0, [Math]::Min($v.Length, 256)) } }
    if ($v -is [uint32]) { return @{ t = 'u'; v = $v } }
    if ($v -is [bool]) { return @{ t = 'z'; v = $v } }
    if ($v -is [uint32[]]) { return @{ t = 'ua'; v = @($v) } }
    if ($v.GetType() -eq $fieldsType) { if ($depth -gt 3) { return @{ t = 'f..' } }; return @{ t = 'f'; v = (Convert-Fields $v ($depth + 1)) } }
    if ($v -is [System.Collections.IList]) { if ($depth -gt 3) { return @{ t = 'l..'; n = $v.Count } }; return @{ t = 'l'; v = @($v | ForEach-Object { Convert-Fields $_ ($depth + 1) }) } }
    return @{ t = $v.GetType().Name }
}
function Convert-Fields($f, [int]$depth) {
    $o = [ordered]@{}
    foreach ($k in $f.Keys) { $o.Add(('{0:X8}' -f $k), (Convert-Value $f[$k] $depth)) }
    return $o
}

# Same archive choice as Materials.AllPresets: one UMD per "_Global_" group (ReadCatalog merges the siblings).
$files = Get-ChildItem -LiteralPath $UmdDir -Filter *.umd -File | Where-Object { $_.Extension -ieq '.umd' } |
    Sort-Object @{ e = { if ($_.Name -ieq 'PEC-Main.umd') { 0 } elseif ($_.Name -ieq 'Blacklist.umd') { 1 } else { 2 } } }, @{ e = { $_.Name.ToLowerInvariant() } }
$groups = [ordered]@{}
foreach ($f in $files) {
    $b = $f.BaseName; $i = $b.IndexOf('_Global_', [StringComparison]::OrdinalIgnoreCase)
    $key = if ($i -lt 0) { $b } else { $b.Substring(0, $i + 8) }
    if (-not $groups.Contains($key.ToLowerInvariant())) { $groups[$key.ToLowerInvariant()] = $f.FullName }
}
$paths = @($groups.Values)
if ($Limit -gt 0) { $paths = $paths[0..($Limit - 1)] }
$out = [System.Collections.Generic.List[object]]::new()
$n = 0
foreach ($p in $paths) {
    $n++
    $entry = [ordered]@{ umd = [IO.Path]::GetFileName($p) }
    try {
        $cat = $read.Invoke($null, [object[]]@($p))
        $mats = @()
        foreach ($m in $cat.Materials.Values) {
            $mats += [ordered]@{ hash = ('{0:X8}' -f $m.Hash); name = $m.Name; native = $m.NativeName; cls = $m.NativeClass; data = (Convert-Fields $m.Data 0) }
        }
        $tex = [ordered]@{}
        foreach ($k in $cat.TextureNames.Keys) { $tex.Add(('{0:X8}' -f $k), $cat.TextureNames[$k]) }
        $entry.materials = $mats; $entry.textures = $tex
        $entry.skipped = $cat.SkippedPackages; $entry.diagnostics = @($cat.Diagnostics)
    } catch {
        $e = $_.Exception; while ($e.InnerException) { $e = $e.InnerException }
        $entry.error = $e.GetType().FullName + ': ' + $e.Message
    }
    $out.Add($entry)
    if ($n % 25 -eq 0) { Write-Host "$n / $($paths.Count)" }
}
[IO.File]::WriteAllText($OutJson, ($out | ConvertTo-Json -Depth 30 -Compress), (New-Object Text.UTF8Encoding $false))
Write-Host "wrote $($out.Count) catalogs"
