#!/usr/bin/env bash
# Baut "Claude Session Browser.app" fuer macOS.
#
#   ./build_mac.sh             -> dist/Claude Session Browser.app
#   ./build_mac.sh --install   -> zusaetzlich nach ~/Applications kopieren
#
# Voraussetzung: Python 3.10 oder neuer (z.B. `brew install python`). Das
# virtuelle Environment in .venv legt das Skript selbst an. Mit PYTHON=...
# laesst sich ein anderer Interpreter waehlen.
set -euo pipefail
cd "$(dirname "$0")"

APP_NAME="Claude Session Browser"
BUNDLE_ID="io.github.juppeee.claude-session-browser"
PYTHON="${PYTHON:-python3}"
INSTALL=0
[ "${1:-}" = "--install" ] && INSTALL=1

echo "[0/5] Python-Umgebung"
[ -x .venv/bin/python ] || "$PYTHON" -m venv .venv
.venv/bin/python -m pip install --quiet --upgrade pip
.venv/bin/python -m pip install --quiet pywebview bleak truststore certifi pyinstaller

echo "[1/5] Uebersetzungen pruefen"
PYTHONIOENCODING=utf-8 .venv/bin/python tools/check_i18n.py

echo "[2/5] App-Symbol"
rm -rf build dist
ICONSET=build/app.iconset
mkdir -p "$ICONSET"
for s in 16 32 128 256; do
  sips -z "$s" "$s" docs/logo.png --out "$ICONSET/icon_${s}x${s}.png" >/dev/null
  d=$((s * 2))
  if [ "$d" -le 256 ]; then
    sips -z "$d" "$d" docs/logo.png --out "$ICONSET/icon_${s}x${s}@2x.png" >/dev/null
  fi
done
iconutil -c icns "$ICONSET" -o build/app.icns

echo "[3/5] PyInstaller"
VERSION=$(.venv/bin/python -c 'import re; print(re.search(r"^VERSION = \"([^\"]+)\"", open("claude_sessions.py", encoding="utf-8").read(), re.M).group(1))')
.venv/bin/pyinstaller --noconfirm --clean --windowed --log-level WARN \
  --name "$APP_NAME" \
  --icon build/app.icns \
  --osx-bundle-identifier "$BUNDLE_ID" \
  --hidden-import macos_support \
  --hidden-import clawdmeter \
  --hidden-import clawd_sprites \
  --collect-submodules bleak \
  --exclude-module tkinter --exclude-module _tkinter \
  claude_sessions.py

echo "[4/5] Info.plist und Signatur"
APP="dist/$APP_NAME.app"
PLIST="$APP/Contents/Info.plist"
plist_set() {  # Schluessel, Typ, Wert - legt an oder ueberschreibt
  /usr/libexec/PlistBuddy -c "Set :$1 $3" "$PLIST" 2>/dev/null \
    || /usr/libexec/PlistBuddy -c "Add :$1 $2 $3" "$PLIST"
}
plist_set CFBundleShortVersionString string "$VERSION"
plist_set CFBundleVersion string "$VERSION"
plist_set LSMinimumSystemVersion string 11.0
plist_set NSHighResolutionCapable bool true
# Ohne diesen Text beendet macOS die App beim ersten Bluetooth-Zugriff
# (Clawdmeter), statt nachzufragen.
plist_set NSBluetoothAlwaysUsageDescription string \
  "'Der Session Browser schickt deine Claude-Auslastung an ein Clawdmeter-Geraet.'"
# Die Aenderungen an der Info.plist brechen die Signatur - neu, ad hoc.
codesign --force --deep --sign - "$APP" >/dev/null

echo "[5/5] Fertig: $APP (v$VERSION)"
if [ "$INSTALL" = "1" ]; then
  mkdir -p "$HOME/Applications"
  TARGET="$HOME/Applications/$APP_NAME.app"
  rm -rf "$TARGET"
  ditto "$APP" "$TARGET"
  echo "      installiert: $TARGET"
fi
