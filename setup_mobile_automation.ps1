$ErrorActionPreference = "Stop"

Write-Host "=== Setup Mobile Automation (Appium + Python) ===" -ForegroundColor Cyan

# 1) Vérifier prérequis de base
if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    throw "Python non trouvé. Installe Python 3.10+ puis relance."
}
if (-not (Get-Command node -ErrorAction SilentlyContinue)) {
    throw "Node.js non trouvé. Installe Node.js puis relance."
}
if (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
    throw "npm non trouvé. Installe Node.js/npm puis relance."
}

Write-Host "Python version:" -ForegroundColor Yellow
python --version
Write-Host "Node version:" -ForegroundColor Yellow
node --version
Write-Host "npm version:" -ForegroundColor Yellow
npm --version

# 2) Installer Appium + driver Android
Write-Host "`n[1/5] Installation Appium global..." -ForegroundColor Green
npm install -g appium

Write-Host "`n[2/5] Installation driver UiAutomator2..." -ForegroundColor Green
appium driver install uiautomator2

# 3) Installer platform-tools (ADB) via winget
Write-Host "`n[3/5] Installation Android platform-tools..." -ForegroundColor Green
winget install --id Google.PlatformTools -e --accept-package-agreements --accept-source-agreements

# Localiser le dossier platform-tools installé par winget
$pkgRoot = Join-Path $env:LOCALAPPDATA "Microsoft\WinGet\Packages"
$ptDir = Get-ChildItem -Path $pkgRoot -Directory |
    Where-Object { $_.Name -like "Google.PlatformTools*" } |
    Select-Object -First 1

if (-not $ptDir) {
    throw "Dossier Google.PlatformTools introuvable après installation."
}

$androidHome = $ptDir.FullName
$platformTools = Join-Path $androidHome "platform-tools"
$adbExe = Join-Path $platformTools "adb.exe"

if (-not (Test-Path $adbExe)) {
    throw "adb.exe introuvable dans $platformTools"
}

# 4) Définir variables d'environnement utilisateur
Write-Host "`n[4/5] Configuration variables d'environnement utilisateur..." -ForegroundColor Green
[Environment]::SetEnvironmentVariable("ANDROID_HOME", $androidHome, "User")
[Environment]::SetEnvironmentVariable("ANDROID_SDK_ROOT", $androidHome, "User")

$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if (-not $userPath) { $userPath = "" }
if ($userPath -notlike "*$platformTools*") {
    $newPath = ($userPath.TrimEnd(';') + ";" + $platformTools).Trim(';')
    [Environment]::SetEnvironmentVariable("Path", $newPath, "User")
}

# Appliquer aussi à la session en cours
$env:ANDROID_HOME = $androidHome
$env:ANDROID_SDK_ROOT = $androidHome
if ($env:Path -notlike "*$platformTools*") {
    $env:Path += ";$platformTools"
}

# 5) Installer dépendances Python
Write-Host "`n[5/5] Installation dépendances Python..." -ForegroundColor Green
python -m pip install --upgrade pip
python -m pip install -r ".\requirements_mobile_automation.txt"

Write-Host "`n=== Vérifications ===" -ForegroundColor Cyan
Write-Host "Appium version:" -ForegroundColor Yellow
appium -v
Write-Host "Drivers Appium installés:" -ForegroundColor Yellow
appium driver list --installed
Write-Host "ADB version:" -ForegroundColor Yellow
& $adbExe version

Write-Host "`nSetup terminé ✅" -ForegroundColor Green
Write-Host "Ouvre un nouveau terminal puis lance:"
Write-Host "  adb devices"
Write-Host "  appium"
