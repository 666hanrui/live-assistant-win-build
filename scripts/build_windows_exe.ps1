param(
  [switch]$Clean = $true,
  [switch]$IncludeLocalDeps = $false,
  [string]$EmbeddingModelDir = "",
  [string]$WhisperModelDir = "",
  [string[]]$ExtraModelDirs = @()
)

$ErrorActionPreference = "Stop"

if ($env:OS -ne "Windows_NT") {
  Write-Error "This script must run on Windows."
  exit 2
}

$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

function Resolve-DirPath([string]$RawPath) {
  $s = [string]($RawPath)
  if ([string]::IsNullOrWhiteSpace($s)) {
    return ""
  }
  $s = $s.Trim().Trim('"').Trim("'")
  $expanded = [Environment]::ExpandEnvironmentVariables($s)
  if (-not [System.IO.Path]::IsPathRooted($expanded)) {
    $expanded = Join-Path $Root $expanded
  }
  try {
    $resolved = (Resolve-Path -LiteralPath $expanded -ErrorAction Stop).Path
    if (Test-Path $resolved -PathType Container) {
      return $resolved
    }
  } catch {
    return ""
  }
  return ""
}

function Test-PathUnder([string]$Path, [string]$Base) {
  try {
    $fullPath = [System.IO.Path]::GetFullPath($Path).TrimEnd('\') + "\"
    $fullBase = [System.IO.Path]::GetFullPath($Base).TrimEnd('\') + "\"
    return $fullPath.StartsWith($fullBase, [System.StringComparison]::OrdinalIgnoreCase)
  } catch {
    return $false
  }
}

function To-RelativeWindowsPath([string]$Path, [string]$Base, [string]$Prefix) {
  $fullPath = [System.IO.Path]::GetFullPath($Path).TrimEnd('\')
  $fullBase = [System.IO.Path]::GetFullPath($Base).TrimEnd('\')
  if ($fullPath.Length -le $fullBase.Length) {
    return $Prefix
  }
  $suffix = $fullPath.Substring($fullBase.Length).TrimStart('\')
  if ([string]::IsNullOrWhiteSpace($suffix)) {
    return $Prefix
  }
  return "$Prefix\$suffix"
}

function Get-EnvValue([string]$EnvFile, [string]$Key) {
  if (!(Test-Path $EnvFile)) {
    return ""
  }
  foreach ($raw in Get-Content -Path $EnvFile -Encoding UTF8) {
    $line = [string]$raw
    if ([string]::IsNullOrWhiteSpace($line)) { continue }
    $trim = $line.Trim()
    if ($trim.StartsWith("#")) { continue }
    $idx = $trim.IndexOf("=")
    if ($idx -lt 1) { continue }
    $k = $trim.Substring(0, $idx).Trim()
    if ($k -ne $Key) { continue }
    $v = $trim.Substring($idx + 1).Trim().Trim('"').Trim("'")
    return $v
  }
  return ""
}

function Set-EnvValue([string]$EnvFile, [string]$Key, [string]$Value) {
  $lineOut = "$Key=$Value"
  $lines = @()
  if (Test-Path $EnvFile) {
    $lines = @(Get-Content -Path $EnvFile -Encoding UTF8)
  }
  $updated = $false
  $pattern = "^\s*" + [Regex]::Escape($Key) + "\s*="
  $out = @()
  foreach ($line in $lines) {
    if (-not $updated -and ([Regex]::IsMatch($line, $pattern))) {
      $out += $lineOut
      $updated = $true
    } else {
      $out += $line
    }
  }
  if (-not $updated) {
    $out += $lineOut
  }
  Set-Content -Path $EnvFile -Value $out -Encoding UTF8
}

function Normalize-Name([string]$Name, [string]$Fallback) {
  $clean = [Regex]::Replace([string]$Name, "[^A-Za-z0-9._-]", "_")
  if ([string]::IsNullOrWhiteSpace($clean)) {
    return $Fallback
  }
  return $clean
}

$VenvPython = Join-Path $Root ".venv\Scripts\python.exe"
if (!(Test-Path $VenvPython)) {
  Write-Host "[1/6] Creating virtualenv..."
  $PyLauncher = Get-Command py -ErrorAction SilentlyContinue
  $PyExe = Get-Command python -ErrorAction SilentlyContinue
  if ($PyLauncher) {
    & py -3 -m venv .venv
  } elseif ($PyExe) {
    & python -m venv .venv
  } else {
    Write-Error "Cannot find Python runtime. Install Python 3.9+ and ensure py/python is in PATH."
    exit 2
  }
}

$VenvPython = Join-Path $Root ".venv\Scripts\python.exe"
if (!(Test-Path $VenvPython)) {
  Write-Error "Cannot find .venv Python: $VenvPython"
  exit 2
}

Write-Host "[2/6] Installing runtime dependencies..."
& $VenvPython -m pip install --upgrade pip setuptools wheel
& $VenvPython -m pip install -r requirements.txt
if ($IncludeLocalDeps) {
  & $VenvPython -m pip install -r requirements-local.txt
}

Write-Host "[3/6] Installing build dependencies..."
& $VenvPython -m pip install "pyinstaller>=6.0"
& $VenvPython -c "import streamlit, sys; print('streamlit:', streamlit.__version__, 'python:', sys.version)"

if ($Clean) {
  Write-Host "[4/6] Cleaning previous build output..."
  Remove-Item -Recurse -Force "$Root\build" -ErrorAction SilentlyContinue
  Remove-Item -Recurse -Force "$Root\dist" -ErrorAction SilentlyContinue
}

Write-Host "[5/6] Building EXE..."
& $VenvPython -m PyInstaller windows_exe.spec --noconfirm --clean

$BundleDir = Join-Path $Root "dist\AI_Live_Assistant"
if (!(Test-Path $BundleDir)) {
  Write-Error "Build output not found: $BundleDir"
  exit 2
}

Write-Host "[6/6] Copying config and model files..."
$DashboardSource = Join-Path $Root "dashboard.py"
$DashboardInBundle = @(
  (Join-Path $BundleDir "_internal\dashboard.py"),
  (Join-Path $BundleDir "dashboard.py")
)
$HasDashboard = $false
foreach ($dash in $DashboardInBundle) {
  if (Test-Path $dash) {
    $HasDashboard = $true
    break
  }
}
if ((-not $HasDashboard) -and (Test-Path $DashboardSource)) {
  $InternalDir = Join-Path $BundleDir "_internal"
  New-Item -ItemType Directory -Force -Path $InternalDir | Out-Null
  Copy-Item $DashboardSource (Join-Path $InternalDir "dashboard.py") -Force
  Copy-Item $DashboardSource (Join-Path $BundleDir "dashboard.py") -Force
  Write-Host "Injected missing dashboard.py into bundle."
}

$EnvSource = Join-Path $Root ".env"
if (!(Test-Path $EnvSource)) {
  $EnvSource = Join-Path $Root ".env.example"
}
$EnvTarget = Join-Path $BundleDir ".env"
if (Test-Path $EnvSource) {
  Copy-Item $EnvSource $EnvTarget -Force
}

$ModelsDir = Join-Path $BundleDir "models"
New-Item -ItemType Directory -Force -Path $ModelsDir | Out-Null

$CopiedBySource = @{}

function Ensure-ModelInBundle([string]$RawPath, [string]$DefaultName, [string]$EnvKey) {
  $resolved = Resolve-DirPath $RawPath
  if ([string]::IsNullOrWhiteSpace($resolved)) {
    return ""
  }

  $DataRoot = Join-Path $Root "data"
  $ModelRoot = Join-Path $Root "models"
  if (Test-PathUnder $resolved $DataRoot) {
    $rel = To-RelativeWindowsPath $resolved $DataRoot ".\data"
    if ($EnvKey) { Set-EnvValue $EnvTarget $EnvKey $rel }
    Write-Host "Using bundled data path for $EnvKey -> $rel"
    return $rel
  }
  if (Test-PathUnder $resolved $ModelRoot) {
    $rel = To-RelativeWindowsPath $resolved $ModelRoot ".\models"
    if ($EnvKey) { Set-EnvValue $EnvTarget $EnvKey $rel }
    Write-Host "Using bundled models path for $EnvKey -> $rel"
    return $rel
  }

  $srcKey = [System.IO.Path]::GetFullPath($resolved).ToLowerInvariant()
  if ($CopiedBySource.ContainsKey($srcKey)) {
    $relPath = $CopiedBySource[$srcKey]
    if ($EnvKey) { Set-EnvValue $EnvTarget $EnvKey $relPath }
    return $relPath
  }

  $leaf = Split-Path -Path $resolved -Leaf
  $safeName = Normalize-Name $leaf $DefaultName
  if ([string]::IsNullOrWhiteSpace($safeName)) {
    $safeName = $DefaultName
  }
  $target = Join-Path $ModelsDir $safeName
  if (Test-Path $target) {
    Remove-Item -Recurse -Force $target -ErrorAction SilentlyContinue
  }
  Copy-Item -Path $resolved -Destination $target -Recurse -Force

  $relative = ".\models\$safeName"
  $CopiedBySource[$srcKey] = $relative
  if ($EnvKey) { Set-EnvValue $EnvTarget $EnvKey $relative }
  Write-Host "Included model folder: $resolved -> $relative"
  return $relative
}

if ($EmbeddingModelDir) {
  Ensure-ModelInBundle $EmbeddingModelDir "embedding_model" "EMBEDDING_MODEL_NAME" | Out-Null
}
if ($WhisperModelDir) {
  Ensure-ModelInBundle $WhisperModelDir "whisper_cache" "VOICE_WHISPER_DOWNLOAD_ROOT" | Out-Null
}

$envEmbeddingModel = Get-EnvValue $EnvTarget "EMBEDDING_MODEL_NAME"
$envEmbeddingCache = Get-EnvValue $EnvTarget "EMBEDDING_CACHE_DIR"
$envWhisperRoot = Get-EnvValue $EnvTarget "VOICE_WHISPER_DOWNLOAD_ROOT"

if ($envEmbeddingModel) {
  Ensure-ModelInBundle $envEmbeddingModel "embedding_model" "EMBEDDING_MODEL_NAME" | Out-Null
}
if ($envEmbeddingCache) {
  Ensure-ModelInBundle $envEmbeddingCache "embedding_cache" "EMBEDDING_CACHE_DIR" | Out-Null
}
if ($envWhisperRoot) {
  Ensure-ModelInBundle $envWhisperRoot "whisper_cache" "VOICE_WHISPER_DOWNLOAD_ROOT" | Out-Null
}

$idx = 0
foreach ($extraDir in $ExtraModelDirs) {
  $idx += 1
  Ensure-ModelInBundle $extraDir ("extra_model_$idx") "" | Out-Null
}

$RunBat = Join-Path $BundleDir "启动助手.bat"
$RunBatContent = @"
@echo off
cd /d "%~dp0"
echo Starting AI Live Assistant...
AI_Live_Assistant.exe
pause
"@
Set-Content -Path $RunBat -Value $RunBatContent -Encoding ASCII

$RunBatAscii = Join-Path $BundleDir "run_assistant.bat"
$RunBatAsciiContent = @"
@echo off
cd /d "%~dp0"
if "%DASHBOARD_PORT%"=="" set DASHBOARD_PORT=8511
echo Starting AI Live Assistant on port %DASHBOARD_PORT% ...
AI_Live_Assistant.exe
pause
"@
Set-Content -Path $RunBatAscii -Value $RunBatAsciiContent -Encoding ASCII

$RunBatDebug = Join-Path $BundleDir "run_assistant_debug.bat"
$RunBatDebugContent = @"
@echo off
cd /d "%~dp0"
if "%DASHBOARD_PORT%"=="" set DASHBOARD_PORT=8511
echo Starting AI Live Assistant (debug) on port %DASHBOARD_PORT% ...
AI_Live_Assistant.exe 1> exe_boot.log 2>&1
echo ExitCode: %ERRORLEVEL%
echo.
echo ==== exe_boot.log ====
type exe_boot.log
echo.
echo ==== launcher_boot.log (runtime) ====
if exist ".\logs\launcher_boot.log" type ".\logs\launcher_boot.log"
if exist "%USERPROFILE%\AI_Live_Assistant\logs\launcher_boot.log" type "%USERPROFILE%\AI_Live_Assistant\logs\launcher_boot.log"
pause
"@
Set-Content -Path $RunBatDebug -Value $RunBatDebugContent -Encoding ASCII

Write-Host ""
Write-Host "Build done."
Write-Host "EXE: $BundleDir\AI_Live_Assistant.exe"
Write-Host "Run: $RunBat"
Write-Host "Run (ASCII): $RunBatAscii"
Write-Host "Run (Debug): $RunBatDebug"
