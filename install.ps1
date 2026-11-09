# MixCaps installer - run through install.bat
# Installs into %LOCALAPPDATA%\MixCaps and adds MixCaps to DaVinci Resolve's Workspace > Scripts menu.
param(
    [switch]$NoTelugu,
    [switch]$NoHindi,
    [switch]$Cpu,
    [switch]$Yes
)
$ErrorActionPreference = "Continue"
$ProgressPreference = "SilentlyContinue"
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$App = Join-Path $env:LOCALAPPDATA "MixCaps"
$Venv = Join-Path $App "venv"
$Py = Join-Path $Venv "Scripts\python.exe"
$Models = Join-Path $App "models"
$Bin = Join-Path $App "bin"

function Step($t) { Write-Host ""; Write-Host "==> $t" -ForegroundColor Cyan }
function Ask($q) {
    if ($Yes) { return $true }
    $a = Read-Host "$q [Y/n]"
    return ($a -eq "" -or $a -match "^[yY]")
}

Write-Host "MixCaps installer" -ForegroundColor Magenta
Write-Host "Captions for English, Telugish and Hinglish inside DaVinci Resolve. Everything runs on this PC."

New-Item -ItemType Directory -Force $App, $Models, $Bin | Out-Null

# ---------------------------------------------------------------- existing config (keep model paths)
$CfgJson = Join-Path $App "config.json"
$cfg = @{}
if (Test-Path $CfgJson) {
    $old = Get-Content $CfgJson -Raw | ConvertFrom-Json
    foreach ($p in $old.PSObject.Properties) { $cfg[$p.Name] = $p.Value }
}
if (-not $cfg.ContainsKey("models_dir")) { $cfg["models_dir"] = $Models }
$Models = $cfg["models_dir"]
New-Item -ItemType Directory -Force $Models | Out-Null

# ---------------------------------------------------------------- files
Step "Copying MixCaps files"
foreach ($d in "engine", "assets") {
    $dst = Join-Path $App $d
    New-Item -ItemType Directory -Force $dst | Out-Null
    Copy-Item -Recurse -Force (Join-Path $Here "$d\*") $dst
}
foreach ($f in "custom_words.txt", "replacements.txt") {
    $p = Join-Path $App $f
    if (-not (Test-Path $p)) { Copy-Item (Join-Path $Here "user\$f") $p }
}

# ---------------------------------------------------------------- python (uv fetches its own Python; no system Python needed)
Step "Setting up the speech engine (private Python)"
$Uv = Join-Path $Bin "uv.exe"
if (-not (Test-Path $Uv)) {
    $zip = Join-Path $env:TEMP "mixcaps-uv.zip"
    Invoke-WebRequest "https://github.com/astral-sh/uv/releases/latest/download/uv-x86_64-pc-windows-msvc.zip" -OutFile $zip
    Expand-Archive -Force $zip (Join-Path $env:TEMP "mixcaps-uv")
    Copy-Item -Force (Get-ChildItem -Recurse (Join-Path $env:TEMP "mixcaps-uv") -Filter uv.exe | Select-Object -First 1).FullName $Uv
    Remove-Item -Force $zip
}
$env:UV_PYTHON_INSTALL_DIR = Join-Path $App "python"
$env:UV_CACHE_DIR = Join-Path $App "uv-cache"
if (-not (Test-Path $Py)) { & $Uv venv --python 3.12 --python-preference only-managed $Venv; if ($LASTEXITCODE) { throw "Could not create the Python environment." } }
& $Uv pip install --python $Py "faster-whisper==1.2.1" "av>=11,<16" "numpy<3"
if ($LASTEXITCODE) { throw "Could not install faster-whisper." }

$hasNvidia = $false
try { $null = & nvidia-smi -L 2>$null; $hasNvidia = ($LASTEXITCODE -eq 0) } catch { }
if ($hasNvidia -and -not $Cpu) {
    Step "NVIDIA GPU found - installing GPU libraries (about 1 GB)"
    & $Uv pip install --python $Py "nvidia-cublas-cu12" "nvidia-cudnn-cu12>=9,<10"
    if ($LASTEXITCODE) { Write-Host "GPU libraries failed; MixCaps will use the CPU (slower)." -ForegroundColor Yellow }
} else {
    Write-Host "No NVIDIA GPU (or -Cpu given): MixCaps will run on the CPU. It works, just slower." -ForegroundColor Yellow
}

# ---------------------------------------------------------------- config (engine reads json, Resolve script reads lua)
function Save-Config {
    $cfg | ConvertTo-Json | Set-Content -Encoding UTF8 $CfgJson
    $lua = @"
return {
  python = [[$Py]],
  pythonw = [[$Venv\Scripts\pythonw.exe]],
  engine = [[$App\engine\mixcaps_engine.py]],
  app_py = [[$App\engine\mixcaps_app.py]],
  assets = [[$App\assets]],
  app = [[$App]],
}
"@
    [System.IO.File]::WriteAllText((Join-Path $App "config.lua"), $lua)
}
Save-Config

# ---------------------------------------------------------------- models
Step "Downloading the general speech model (about 1.6 GB, once)"
& $Py (Join-Path $App "engine\mixcaps_engine.py") download --models turbo
if ($LASTEXITCODE) { throw "Model download failed. Check your internet and run install.bat again." }

function Install-IndicModel($kind, $repo, $key, $folder) {
    if ($cfg.ContainsKey($key) -and (Test-Path (Join-Path $cfg[$key] "model.bin"))) {
        Write-Host "$kind model already installed: $($cfg[$key])"; return
    }
    $dst = Join-Path $Models $folder
    if (Test-Path (Join-Path $dst "model.bin")) { $cfg[$key] = $dst; Save-Config; return }
    Step "Installing the $kind model ($repo - download + a few minutes converting)"
    $Conv = Join-Path $App "convert-venv"
    $CPy = Join-Path $Conv "Scripts\python.exe"
    if (-not (Test-Path $CPy)) { & $Uv venv --python 3.12 --python-preference only-managed $Conv }
    & $Uv pip install --python $CPy "ctranslate2>=4.5" "transformers>=4.40,<5" "huggingface_hub" "torch" --index-url https://download.pytorch.org/whl/cpu --extra-index-url https://pypi.org/simple
    if ($LASTEXITCODE) { throw "Could not install the model converter." }
    $src = Join-Path $App "download-$kind"
    & $CPy -c "from huggingface_hub import snapshot_download; snapshot_download('$repo', local_dir=r'$src', allow_patterns=['*.json','*.txt','pytorch_model.bin'])"
    if ($LASTEXITCODE) { throw "Could not download $repo." }
    & (Join-Path $Conv "Scripts\ct2-transformers-converter.exe") --model $src --output_dir $dst --quantization float16 --copy_files tokenizer_config.json preprocessor_config.json --force
    if ($LASTEXITCODE -or -not (Test-Path (Join-Path $dst "model.bin"))) { throw "Converting the $kind model failed." }
    Remove-Item -Recurse -Force $src
    $cfg[$key] = $dst
    Save-Config
}
# Telugu models for Telugish captions, one per speed tier (Hinglish/English use general models that download on first use)
if (-not $NoTelugu) {
    Write-Host ""
    Write-Host "Telugu models (for Telugish captions):" -ForegroundColor Cyan
    Write-Host "  Best     - Telugu Large  (~6 GB download, needs ~5 GB GPU memory)  most accurate"
    Write-Host "  Balanced - Telugu Medium (~3 GB download, ~3 GB GPU memory)"
    Write-Host "  Light    - Telugu Small  (~1 GB download, runs fine on CPU)"
    $hasBig = $hasNvidia -and -not $Cpu
    if (Ask "Install Telugu Large (Best)?$(if (-not $hasBig) { ' (no NVIDIA GPU found: it will be slow)' })") {
        Install-IndicModel "Telugu Large" "vasista22/whisper-telugu-large-v2" "telugu_path" "whisper-telugu-large-v2-ct2" }
    if (Ask "Install Telugu Medium (Balanced)?") {
        Install-IndicModel "Telugu Medium" "vasista22/whisper-telugu-medium" "telugu_medium_path" "whisper-telugu-medium-ct2" }
    if (Ask "Install Telugu Small (Light)?") {
        Install-IndicModel "Telugu Small" "vasista22/whisper-telugu-small" "telugu_small_path" "whisper-telugu-small-ct2" }
}
$conv = Join-Path $App "convert-venv"
if (Test-Path $conv) { Remove-Item -Recurse -Force $conv }

# ---------------------------------------------------------------- font
Step "Installing the Poppins fonts (for your user)"
$fontDir = Join-Path $env:LOCALAPPDATA "Microsoft\Windows\Fonts"
New-Item -ItemType Directory -Force $fontDir | Out-Null
foreach ($f in Get-ChildItem (Join-Path $Here "assets\fonts") -Filter *.ttf) {
    $dst = Join-Path $fontDir $f.Name
    if (-not (Test-Path $dst)) {
        Copy-Item $f.FullName $dst
        $name = [System.IO.Path]::GetFileNameWithoutExtension($f.Name) -replace "-", " "
        New-ItemProperty -Force -Path "HKCU:\Software\Microsoft\Windows NT\CurrentVersion\Fonts" -Name "$name (TrueType)" -Value $dst | Out-Null
    }
}

# ---------------------------------------------------------------- Resolve script
Step "Adding MixCaps to DaVinci Resolve (Workspace > Scripts > MixCaps)"
$scripts = Join-Path $env:APPDATA "Blackmagic Design\DaVinci Resolve\Support\Fusion\Scripts\Utility"
New-Item -ItemType Directory -Force $scripts | Out-Null
Copy-Item -Force (Join-Path $Here "resolve\MixCaps.lua") (Join-Path $scripts "MixCaps.lua")

Step "Checking"
& $Py (Join-Path $App "engine\mixcaps_engine.py") check

Write-Host ""
Write-Host "MixCaps is installed." -ForegroundColor Green
Write-Host "Restart DaVinci Resolve, open a timeline, then Workspace > Scripts > MixCaps."
Write-Host "MixCaps opens in your web browser (it runs on this PC only)."
Write-Host "Your word lists: $App\custom_words.txt and $App\replacements.txt"
