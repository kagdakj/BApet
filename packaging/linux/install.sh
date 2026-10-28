#!/usr/bin/env sh
# Install the Linux build for the current user (no root needed).
#   ./install.sh            install
#   ./install.sh --uninstall
set -eu
HERE="$(cd "$(dirname "$0")" && pwd)"
BIN_DIR="${XDG_BIN_HOME:-$HOME/.local/bin}"
APP_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
ICON_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor/256x256/apps"

if [ "${1:-}" = "--uninstall" ]; then
    rm -f "$BIN_DIR/BlueArchiveDesktopPet" "$APP_DIR/bluearchive-desktop-pet.desktop" \
          "$ICON_DIR/bluearchive-desktop-pet.png"
    echo "Uninstalled. Settings and downloaded models are kept in ~/.config/bluearchive-desktop-pet and ~/.local/share/bluearchive-desktop-pet."
    exit 0
fi

mkdir -p "$BIN_DIR" "$APP_DIR" "$ICON_DIR"
install -m 755 "$HERE/BlueArchiveDesktopPet" "$BIN_DIR/BlueArchiveDesktopPet"
install -m 644 "$HERE/bluearchive-desktop-pet.png" "$ICON_DIR/bluearchive-desktop-pet.png"
sed "s|^Exec=.*|Exec=$BIN_DIR/BlueArchiveDesktopPet|" "$HERE/bluearchive-desktop-pet.desktop" \
    > "$APP_DIR/bluearchive-desktop-pet.desktop"
command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$APP_DIR" || true
echo "Installed to $BIN_DIR/BlueArchiveDesktopPet — find 'Blue Archive Desktop Pet' in your app menu."
