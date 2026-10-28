#!/usr/bin/env bash
# Build the Linux single-file executable and a release tarball.
#   scripts/build_linux.sh            (uses/creates build/venv-linux)
#   PYTHON=/path/to/python scripts/build_linux.sh
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$PWD"
VERSION="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' bapet/__init__.py)"
ARCH="$(uname -m)"

if [ -z "${PYTHON:-}" ]; then
    if [ ! -x build/venv-linux/bin/python ]; then
        python3 -m venv build/venv-linux
    fi
    PYTHON="$ROOT/build/venv-linux/bin/python"
    "$PYTHON" -m pip install --quiet --upgrade pip
    "$PYTHON" -m pip install --quiet -r requirements-dev.txt
fi

echo "==> tests"
"$PYTHON" -m pytest -q

echo "==> pyinstaller"
"$PYTHON" -m PyInstaller --noconfirm --clean --distpath build/dist-linux --workpath build/pyi-linux bapet.spec
# Unlink before copying so a currently running copy of the old binary is not disturbed.
mkdir -p dist/linux
rm -f dist/linux/BlueArchiveDesktopPet
cp build/dist-linux/BlueArchiveDesktopPet dist/linux/BlueArchiveDesktopPet

PKG="BlueArchiveDesktopPet-$VERSION-linux-$ARCH"
STAGE="build/$PKG"
rm -rf "$STAGE"
mkdir -p "$STAGE"
cp dist/linux/BlueArchiveDesktopPet "$STAGE/"
cp packaging/linux/install.sh packaging/linux/bluearchive-desktop-pet.desktop "$STAGE/"
cp bapet/assets/icon.png "$STAGE/bluearchive-desktop-pet.png"
cp README.md LICENSE "$STAGE/"
tar -C build -czf "dist/$PKG.tar.gz" "$PKG"
echo "==> dist/linux/BlueArchiveDesktopPet"
echo "==> dist/$PKG.tar.gz"
