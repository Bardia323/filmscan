# Logs when the film scanner appears on / disappears from USB. Reads Windows' device list only; never touches the scanner.
param([int]$WaitMinutes = 4, [int]$WatchMinutes = 9, [string]$Log = "$PSScriptRoot\..\out\watch.log")
function Present { $d = Get-PnpDevice -Class Image -ErrorAction SilentlyContinue | Where-Object FriendlyName -like '*Film Scanner*'; return [bool]($d -and $d.Present) }
function Say($m) { $line = (Get-Date -Format 'HH:mm:ss') + ' ' + $m; Add-Content -Path $Log -Value $line -Encoding utf8; Write-Output $line }
Set-Content -Path $Log -Value '' -Encoding utf8
$deadline = (Get-Date).AddMinutes($WaitMinutes)
while (-not (Present)) { if ((Get-Date) -gt $deadline) { Say 'scanner never appeared; nothing to watch'; exit 2 }; Start-Sleep 5 }
Say 'scanner is on USB; watching'
$end = (Get-Date).AddMinutes($WatchMinutes)
while ((Get-Date) -lt $end) {
    Start-Sleep 15
    if (-not (Present)) { Say 'SCANNER DROPPED OFF USB'; exit 1 }
}
Say "still on USB after $WatchMinutes minutes"
