# SPDX-FileCopyrightText: 2026 Crash
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Costruisce FedTermHub per Windows: FedTermHub.exe (PyInstaller, cartella dist\windows\FedTermHub) e il
# programma di installazione dist\packages\FedTermHub-<versione>-Setup.exe (Inno Setup 6).
#   pwsh packaging\windows\build.ps1            (da una macchina Windows 10/11 con Python 3.13)
$ErrorActionPreference = 'Stop'
$root = (Resolve-Path "$PSScriptRoot\..\..").Path
Set-Location $root

Write-Host '-> Dipendenze di build'
python -m pip install --quiet --upgrade pip
python -m pip install --quiet -r packaging\windows\requirements.txt pyinstaller==6.22.3 pillow==13.0.0
if ($LASTEXITCODE) { throw 'pip non riuscito' }

$assets = Join-Path $root 'build\windows'
$version = (python packaging\windows\make_assets.py $assets).Trim()
if ($LASTEXITCODE) { throw 'make_assets non riuscito' }
Write-Host "-> FedTermHub $version"

# Modulo comtypes del componente RDP di Windows (mstscax.dll), generato ora e incluso nell'eseguibile.
python -c "import comtypes.client, os; comtypes.client.GetModule(os.path.join(os.environ['SystemRoot'], 'System32', 'mstscax.dll'))"
if ($LASTEXITCODE) { throw 'comtypes: generazione del modulo di mstscax.dll non riuscita' }

$icon = 'share\icons\hicolor\256x256\apps\io.github.crash_fd01.fedtermhub.png'
Write-Host '-> FedTermHub.exe (PyInstaller)'
# Solo l'interfaccia Qt (fedtermhub\qtui) e i moduli che usa: l'interfaccia GTK resta fuori.
python -m PyInstaller --noconfirm --clean --windowed --name FedTermHub `
    --icon "$assets\fedtermhub.ico" --version-file "$assets\version.txt" `
    --paths $root `
    --collect-submodules fedtermhub.qtui --collect-all winpty --collect-all comtypes `
    --add-data "$root\fedtermhub\qtui\xterm;fedtermhub\qtui\xterm" `
    --add-data "$root\fedtermhub\locale;fedtermhub\locale" `
    --add-data "$root\fedtermhub\icons;fedtermhub\icons" `
    --add-data "$root\$icon;fedtermhub\qtui" `
    --exclude-module tkinter --exclude-module unittest --exclude-module pydoc --exclude-module gi `
    --exclude-module fedtermhub.fedtermhub `
    --distpath dist\windows --workpath build\pyinstaller --specpath build\pyinstaller `
    packaging\windows\fedtermhub_main.py
if ($LASTEXITCODE) { throw 'PyInstaller non riuscito' }

# licenze dei componenti inclusi accanto al programma
$lic = 'dist\windows\FedTermHub\licenses'
New-Item -ItemType Directory -Force $lic | Out-Null
Copy-Item LICENSE, LICENSES\*.txt, fedtermhub\qtui\xterm\LICENSE-xterm.txt $lic
python -m pip list --format=freeze | Out-File -Encoding utf8 "$lic\BUNDLED-COMPONENTS.txt"

Write-Host '-> Programma di installazione (Inno Setup)'
$iscc = (Get-Command iscc.exe -ErrorAction SilentlyContinue).Source
if (-not $iscc) {
    foreach ($p in @("${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe", "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
                     "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe")) {
        if (Test-Path $p) { $iscc = $p; break }
    }
}
if (-not $iscc) {
    Write-Host '   Inno Setup non trovato: lo installo con winget o choco'
    if (Get-Command choco -ErrorAction SilentlyContinue) { choco install innosetup -y --no-progress | Out-Null }
    else { winget install -e --id JRSoftware.InnoSetup --accept-source-agreements --accept-package-agreements }
    $iscc = "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe"
}
New-Item -ItemType Directory -Force dist\packages | Out-Null
& $iscc /Q "/DMyAppVersion=$version" packaging\windows\fedtermhub.iss
if ($LASTEXITCODE) { throw 'Inno Setup non riuscito' }
Get-Item "dist\packages\FedTermHub-$version-Setup.exe" | ForEach-Object { '{0}  {1:N0} MB' -f $_.Name, ($_.Length / 1MB) }
