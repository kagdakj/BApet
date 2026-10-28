# Build the Windows single-file executable natively.
#   powershell -ExecutionPolicy Bypass -File scripts\build_windows.ps1
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")
$version = (Select-String -Path bapet\__init__.py -Pattern '__version__ = "(.*)"').Matches[0].Groups[1].Value

if (-not (Test-Path build\venv-win\Scripts\python.exe)) {
    py -3.12 -m venv build\venv-win
}
$py = "build\venv-win\Scripts\python.exe"
& $py -m pip install --quiet --upgrade pip
& $py -m pip install --quiet -r requirements-dev.txt
& $py -m pytest -q
& $py packaging\write_version_info.py
& $py -m PyInstaller --noconfirm --clean --distpath dist\windows --workpath build\pyi-win bapet.spec

$out = "dist\BlueArchiveDesktopPet-$version-windows-x64.exe"
Copy-Item dist\windows\BlueArchiveDesktopPet.exe $out -Force
Write-Host "==> $out"
