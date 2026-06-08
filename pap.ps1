# Transcend miniCPAP serial transport.
# Runs one or more PAP commands over the serial port and prints each response on its own
# line, in order. Used by settings.py (and mirrors the transport in collect.ps1).
#
#   powershell -File pap.ps1 -Port COM3 -Command Tbd,Tab
#
# Each command is sent char-by-char (the device echoes input), terminated with CR; the
# response is the text between the two CR markers (3-char response code + arguments).
param(
  [string]$Port = 'COM3',
  [string[]]$Command,
  [int]$TimeoutSec = 10
)

function Invoke-PAP($p, $cmd, $timeoutSec = 10) {
  Start-Sleep -Milliseconds 60
  $p.ReadExisting() | Out-Null
  foreach ($ch in $cmd.ToCharArray()) { $p.Write([string]$ch); Start-Sleep -Milliseconds 12 }
  $p.Write("`r")
  $sb = New-Object System.Text.StringBuilder
  $deadline = (Get-Date).AddSeconds($timeoutSec)
  while ((Get-Date) -lt $deadline) {
    $chunk = $p.ReadExisting()
    if ($chunk.Length -gt 0) { [void]$sb.Append($chunk) }
    $crs = (($sb.ToString()).ToCharArray() | Where-Object { $_ -eq "`r" }).Count
    if ($crs -ge 2) { break }
    Start-Sleep -Milliseconds 20
  }
  $s = $sb.ToString()
  $i = $s.IndexOf("`r")
  if ($i -lt 0) { return "" }
  return $s.Substring($i).Trim("`r")
}

if (-not $Command -or $Command.Count -eq 0) { Write-Error "No -Command given"; exit 2 }

$p = New-Object System.IO.Ports.SerialPort $Port,38400,'None',8,'One'
$p.RtsEnable = $false; $p.DtrEnable = $false; $p.DiscardNull = $true
$p.ReadTimeout = 10000; $p.WriteTimeout = 10000
try {
  $p.Open()
  foreach ($c in $Command) { Write-Output (Invoke-PAP $p $c $TimeoutSec) }
}
finally {
  if ($p.IsOpen) { $p.Close() }
  $p.Dispose()
}
