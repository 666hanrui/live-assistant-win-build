param(
  [string]$AudioDir = "",
  [string]$CasesPath = "",
  [int]$GapSeconds = 2,
  [int]$Rounds = 1
)

$ErrorActionPreference = "Stop"
if ([string]::IsNullOrWhiteSpace($AudioDir)) {
  $AudioDir = Join-Path $PSScriptRoot "audio"
}
if ([string]::IsNullOrWhiteSpace($CasesPath)) {
  $CasesPath = Join-Path $PSScriptRoot "test_cases.json"
}
if (!(Test-Path $AudioDir)) {
  throw "Audio dir not found: $AudioDir"
}
if (!(Test-Path $CasesPath)) {
  throw "Cases file not found: $CasesPath"
}

$json = Get-Content -Raw -Encoding UTF8 $CasesPath | ConvertFrom-Json
$caseMap = @{}
foreach ($c in $json.cases) {
  $caseMap[[string]$c.id] = [string]$c.text
}

$files = Get-ChildItem -Path $AudioDir -Filter "*.wav" | Sort-Object Name
if ($files.Count -eq 0) {
  throw "No wav files found in $AudioDir"
}

Add-Type -AssemblyName System.Media
$player = New-Object System.Media.SoundPlayer

for ($r = 1; $r -le $Rounds; $r++) {
  Write-Host "Round $r / $Rounds"
  foreach ($f in $files) {
    $id = [System.IO.Path]::GetFileNameWithoutExtension($f.Name)
    $txt = $caseMap[$id]
    Write-Host ("Play [{0}] {1}" -f $id, $txt)
    $player.SoundLocation = $f.FullName
    $player.Load()
    $player.PlaySync()
    Start-Sleep -Seconds $GapSeconds
  }
}

Write-Host "Playback done."
