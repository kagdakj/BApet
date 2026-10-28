#!/usr/bin/env bash
# Cross-build the Windows .exe from Linux using Wine + the official CPython
# NuGet package (no installer needed). Requires: wine (64-bit), curl, unzip.
#   scripts/build_windows_wine.sh
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$PWD"
PY_VERSION="${PY_VERSION:-3.12.7}"
VERSION="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' bapet/__init__.py)"
# Keep the prefix outside the project: its z: drive links to / and would make
# editors/indexers that follow symlinks crawl the whole filesystem.
export WINEPREFIX="${WINEPREFIX:-${XDG_CACHE_HOME:-$HOME/.cache}/bapet-wine-build}"
export WINEARCH=win64
export WINEDEBUG=-all
PYDIR="$WINEPREFIX/drive_c/python"

if [ ! -f "$PYDIR/python.exe" ]; then
    echo "==> setting up Wine prefix and Python $PY_VERSION"
    wineboot -i >/dev/null 2>&1 || true
    tmp="$(mktemp -d)"
    curl -fsSL -o "$tmp/python.zip" "https://www.nuget.org/api/v2/package/python/$PY_VERSION"
    unzip -q "$tmp/python.zip" -d "$tmp/pkg"
    rm -rf "$PYDIR"
    mv "$tmp/pkg/tools" "$PYDIR"
    rm -rf "$tmp"
    wine "$PYDIR/python.exe" -m ensurepip --upgrade
fi
WPY=(wine "$PYDIR/python.exe")
"${WPY[@]}" -m pip install --quiet --disable-pip-version-check -r requirements-dev.txt

echo "==> tests (Windows Python under Wine)"
# Qt 6 on Windows links against the system ICU (icuuc.dll, Windows 10 1903+), which
# Wine does not provide, so only the Qt-free modules can be tested here.
"${WPY[@]}" -m pytest -q -p no:cacheprovider \
    tests/test_gltf.py tests/test_catalog.py tests/test_config.py tests/test_renderer.py

"${WPY[@]}" packaging/write_version_info.py
echo "==> pyinstaller"
"${WPY[@]}" -m PyInstaller --noconfirm --clean --distpath dist/windows --workpath build/pyi-win bapet.spec

OUT="dist/BlueArchiveDesktopPet-$VERSION-windows-x64.exe"
cp dist/windows/BlueArchiveDesktopPet.exe "$OUT"
echo "==> $OUT"
