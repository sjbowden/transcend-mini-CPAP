# Transcend miniCPAP raw event-log collector.
# Drives the FTDI serial port, downloads the compliance event log, writes raw hex to -OutFile.
param(
  [string]$Port = 'COM3',
  [string]$OutFile = 'dump.txt'
)

function Invoke-PAP($p, $cmd, $timeoutSec = 8) {
  Start-Sleep -Milliseconds 60
  $p.ReadExisting() | Out-Null
  foreach ($ch in $cmd.ToCharArray()) { $p.Write([string]$ch); Start-Sleep -Milliseconds 12 }
  $p.Write("`r")
  $sb = New-Object System.Text.StringBuilder
  $deadline = (Get-Date).AddSeconds($timeoutSec)
  $lastData = Get-Date
  while ((Get-Date) -lt $deadline) {
    $chunk = $p.ReadExisting()
    if ($chunk.Length -gt 0) { [void]$sb.Append($chunk); $lastData = Get-Date }
    $s = $sb.ToString()
    $crs = ($s.ToCharArray() | Where-Object { $_ -eq "`r" }).Count
    if ($crs -ge 2) { break }
    Start-Sleep -Milliseconds 20
  }
  $s = $sb.ToString()
  $i = $s.IndexOf("`r")
  if ($i -lt 0) { return "" }
  $resp = $s.Substring($i).Trim("`r")
  return $resp   # includes 3-char response code + args
}

$p = New-Object System.IO.Ports.SerialPort $Port,38400,'None',8,'One'
$p.RtsEnable = $false; $p.DtrEnable = $false; $p.DiscardNull = $true
$p.ReadTimeout = 10000; $p.WriteTimeout = 10000
$p.Open()

$out = New-Object System.Collections.Generic.List[string]

# Header
$hdr = Invoke-PAP $p 'Tbd'
$out.Add("HEADER " + $hdr)
$dev = Invoke-PAP $p 'Tff'
$out.Add("DEVICE " + $dev)

# Event data address (Ra8 + 4 hex chars)
$addrResp = Invoke-PAP $p 'Ta8'
$out.Add("ADDR " + $addrResp)
$address = [Convert]::ToInt32($addrResp.Substring(3), 16)

# Prime read of 50-byte header region (mirrors official client)
$null = Invoke-PAP $p ('Ta9' + ('{0:X4}' -f $address) + ('{0:X4}' -f 50))

# Block loop
$nextStart = $address + 50
$readSize  = 1000
$recordsPerFullBlock = [int]($readSize / 5)
$block = 0
while ($true) {
  $cmd = 'Ta9' + ('{0:X4}' -f $nextStart) + ('{0:X4}' -f $readSize)
  $resp = Invoke-PAP $p $cmd 12
  if ($resp.Length -lt 3) { $out.Add("BLOCKERR $cmd -> '$resp'"); break }
  $comp = $resp.Substring(3)   # strip Ra9
  $out.Add("BLOCK $nextStart $comp")
  # count non-ff 5-byte (10 hex) records
  $valid = 0
  for ($i = 0; $i + 10 -le $comp.Length; $i += 10) {
    $rec = $comp.Substring($i, 10)
    if (-not ($rec.ToLower() -match '^f{10}$')) { $valid++ }
  }
  $block++
  Write-Host ("block $block @"+$nextStart+": got "+([int]($comp.Length/10))+" records, $valid valid")
  if ($valid -eq $recordsPerFullBlock) { $nextStart += $readSize } else { break }
  if ($block -gt 200) { $out.Add("ABORT too many blocks"); break }
}

$p.Close(); $p.Dispose()
Set-Content -Path $OutFile -Value $out -Encoding ASCII
Write-Host ("Wrote " + $out.Count + " lines to " + $OutFile)
