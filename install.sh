#!/usr/bin/env bash
set -euo pipefail

SOURCE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
TARGET_DIR="$HOME/.local/opt/genshin-macro"
DESKTOP_DIR="$HOME/.local/share/applications"

mkdir -p "$TARGET_DIR" "$DESKTOP_DIR"
install -m 0644 "$SOURCE_DIR/main.py" "$TARGET_DIR/main.py"
install -m 0755 "$SOURCE_DIR/run.sh" "$TARGET_DIR/run.sh"

cat > "$DESKTOP_DIR/genshin-macro.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Genshin Macro
Comment=Tray macros for Genshin Impact
Exec=$TARGET_DIR/run.sh
Icon=applications-games
Terminal=false
Categories=Game;Utility;
StartupNotify=false
EOF

chmod 0644 "$DESKTOP_DIR/genshin-macro.desktop"
update-desktop-database "$DESKTOP_DIR" >/dev/null 2>&1 || true

echo "Installed. Launch 'Genshin Macro' from KDE's Application Launcher."
echo "This does not enable autostart."
