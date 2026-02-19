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

function Invoke-External([string]$Label, [string]$FilePath, [string[]]$Args) {
  & $FilePath @Args
  $code = $LASTEXITCODE
  if ($null -eq $code) {
    $code = 0
  }
  if ($code -ne 0) {
    throw "$Label failed (exit code $code)"
  }
}

function Try-PreinstallWheel([string]$PythonExe, [string]$PackageSpec) {
  Write-Host "Try preinstall wheel: $PackageSpec"
  & $PythonExe -m pip install --only-binary=:all: $PackageSpec
  $code = $LASTEXITCODE
  if ($null -eq $code) {
    $code = 0
  }
  if ($code -ne 0) {
    Write-Warning "Preinstall wheel failed for $PackageSpec, fallback to normal dependency install."
  }
}

$VenvPython = Join-Path $Root ".venv\Scripts\python.exe"
if (!(Test-Path $VenvPython)) {
  Write-Host "[1/6] Creating virtualenv..."
  $PyLauncher = Get-Command py -ErrorAction SilentlyContinue
  $PyExe = Get-Command python -ErrorAction SilentlyContinue
  if ($PyLauncher) {
    Invoke-External "Create virtualenv via py -3" "py" @("-3", "-m", "venv", ".venv")
  } elseif ($PyExe) {
    Invoke-External "Create virtualenv via python" "python" @("-m", "venv", ".venv")
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
Invoke-External "Upgrade pip toolchain" $VenvPython @("-m", "pip", "install", "--upgrade", "pip", "setuptools", "wheel")
Try-PreinstallWheel $VenvPython "PyAudio==0.2.14"
Try-PreinstallWheel $VenvPython "pocketsphinx==5.0.4"
Try-PreinstallWheel $VenvPython "opencv-python==4.10.0.84"
Invoke-External "Install requirements.txt" $VenvPython @("-m", "pip", "install", "-r", "requirements.txt")
if ($IncludeLocalDeps) {
  Invoke-External "Install requirements-local.txt" $VenvPython @("-m", "pip", "install", "-r", "requirements-local.txt")
}

Write-Host "[3/6] Installing build dependencies..."
Invoke-External "Install PyInstaller" $VenvPython @("-m", "pip", "install", "pyinstaller>=6.0")
Invoke-External "Preflight import streamlit" $VenvPython @("-c", "import streamlit, sys; print('streamlit:', streamlit.__version__, 'python:', sys.version)")

if ($Clean) {
  Write-Host "[4/6] Cleaning previous build output..."
  Remove-Item -Recurse -Force "$Root\build" -ErrorAction SilentlyContinue
  Remove-Item -Recurse -Force "$Root\dist" -ErrorAction SilentlyContinue
}

Write-Host "[5/6] Building EXE..."
Invoke-External "Run PyInstaller" $VenvPython @("-m", "PyInstaller", "windows_exe.spec", "--noconfirm", "--clean")

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

$VenvSitePackages = Join-Path $Root ".venv\Lib\site-packages"
$BundleSitePackages = Join-Path $BundleDir "_internal\site-packages"
if (Test-Path $VenvSitePackages) {
  New-Item -ItemType Directory -Force -Path $BundleSitePackages | Out-Null
  $fallbackPatterns = @(
    "streamlit",
    "streamlit-*.dist-info"
  )
  foreach ($pattern in $fallbackPatterns) {
    $items = Get-ChildItem -Path (Join-Path $VenvSitePackages $pattern) -ErrorAction SilentlyContinue
    foreach ($item in $items) {
      $dst = Join-Path $BundleSitePackages $item.Name
      if (Test-Path $dst) {
        Remove-Item -Recurse -Force $dst -ErrorAction SilentlyContinue
      }
      Copy-Item -Path $item.FullName -Destination $dst -Recurse -Force
    }
  }
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

$BuildInfoFile = Join-Path $BundleDir "build_info.txt"
$BuildCommit = ""
try {
  $BuildCommit = (& git rev-parse --short HEAD).Trim()
} catch {
  $BuildCommit = ""
}
$BuildInfo = @(
  "build_time_utc=$(Get-Date -Format 'yyyy-MM-ddTHH:mm:ssZ')",
  "build_commit=$BuildCommit"
)
Set-Content -Path $BuildInfoFile -Value $BuildInfo -Encoding ASCII

$ExePath = Join-Path $BundleDir "AI_Live_Assistant.exe"
Write-Host "[7/7] Validating bundled EXE can import streamlit..."
$env:APP_LAUNCHER_SELF_CHECK = "1"
try {
  Invoke-External "APP_LAUNCHER_SELF_CHECK" $ExePath @()
} finally {
  Remove-Item Env:APP_LAUNCHER_SELF_CHECK -ErrorAction SilentlyContinue
}

Write-Host ""
Write-Host "Build done."
Write-Host "EXE: $BundleDir\AI_Live_Assistant.exe"
Write-Host "Run: $RunBat"
Write-Host "Run (ASCII): $RunBatAscii"
Write-Host "Run (Debug): $RunBatDebug"
Write-Host "Build info: $BuildInfoFile"
