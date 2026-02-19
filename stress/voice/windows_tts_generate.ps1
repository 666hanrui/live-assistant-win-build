param(
  [string]$CasesPath = "",
  [string]$OutDir = "",
  [ValidateSet("all", "positive", "negative", "quick", "zh", "en")]
  [string]$Profile = "quick",
  [int]$Rate = 0
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
if ([string]::IsNullOrWhiteSpace($CasesPath)) {
  $CasesPath = Join-Path $PSScriptRoot "test_cases.json"
}
if ([string]::IsNullOrWhiteSpace($OutDir)) {
  $OutDir = Join-Path $PSScriptRoot "audio"
}

if (!(Test-Path $CasesPath)) {
  throw "Cases file not found: $CasesPath"
}
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

$json = Get-Content -Raw -Encoding UTF8 $CasesPath | ConvertFrom-Json
$cases = @($json.cases)

switch ($Profile) {
  "positive" { $cases = $cases | Where-Object { $_.category -like "*positive*" } }
  "negative" { $cases = $cases | Where-Object { $_.category -like "*negative*" -or $_.category -like "*risk*" } }
  "quick" {
    $ids = @("P001","P005","P013","P015","L001","L003","L005","L006","N001","N003","N006","M002")
    $cases = $cases | Where-Object { $ids -contains $_.id }
  }
  "zh" { $cases = $cases | Where-Object { [string]$_.lang -like "zh*" } }
  "en" { $cases = $cases | Where-Object { [string]$_.lang -like "en*" } }
  default { }
}

Add-Type -AssemblyName System.Speech
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
$synth.Rate = $Rate

Write-Host "Generating wav files to $OutDir, profile=$Profile ..."
foreach ($c in $cases) {
  $id = [string]$c.id
  $text = [string]$c.text
  if ([string]::IsNullOrWhiteSpace($id) -or [string]::IsNullOrWhiteSpace($text)) { continue }
  $outFile = Join-Path $OutDir "$id.wav"
  $synth.SetOutputToWaveFile($outFile)
  $synth.Speak($text)
  Write-Host "OK $id -> $outFile"
}
$synth.SetOutputToDefaultAudioDevice()
Write-Host "Done."
