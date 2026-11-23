# Waits until a folder that is being copied into has settled:
# every LRV segment has its INSV twin,
# and sizes and timestamps stayed the same for three checks in a row.
# (An open-for-writing test is logged only: other programs keep files open too.)
param(
    [Parameter(Mandatory)] [string] $Folder,
    [int] $IntervalS = 60,
    [int] $MaxMinutes = 240,
    [string] $Status = ""
)

function Get-Snapshot {
    Get-ChildItem -LiteralPath $Folder -File | Sort-Object Name |
        ForEach-Object { "$($_.Name)|$($_.Length)|$($_.LastWriteTimeUtc.Ticks)" }
}

function Test-Free([string] $path) {
    try {
        $s = [System.IO.File]::Open($path, 'Open', 'Read', 'Read')   # fails while a writer holds it
        $s.Close()
        return $true
    } catch { return $false }
}

$deadline = (Get-Date).AddMinutes($MaxMinutes)
$stable = 0
$last = ""
while ((Get-Date) -lt $deadline) {
    $files = Get-ChildItem -LiteralPath $Folder -File
    $lrv = @($files | Where-Object Extension -eq ".lrv").Count
    $insv = @($files | Where-Object Extension -eq ".insv").Count
    $locked = @($files | Where-Object { -not (Test-Free $_.FullName) }).Count
    $snap = (Get-Snapshot) -join "`n"
    if ($snap -eq $last -and $insv -ge $lrv -and $insv -gt 0) { $stable++ } else { $stable = 0 }
    $last = $snap
    $line = "{0:HH:mm:ss} lrv={1} insv={2} locked={3} stable={4}" -f (Get-Date), $lrv, $insv, $locked, $stable
    if ($Status) { Add-Content -LiteralPath $Status -Value $line }
    if ($stable -ge 3) { Write-Output "COPY DONE $line"; exit 0 }
    Start-Sleep -Seconds $IntervalS
}
Write-Output "TIMEOUT after $MaxMinutes min: $line"
exit 1
