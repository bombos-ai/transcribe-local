#!/bin/bash
# install.sh - set up transcribe-local on macOS and start the menu-bar app.
#
#   macos/install.sh              venv + packages, build the capture helper,
#                                 start the menu-bar app at login (with watchdog)
#   macos/install.sh --no-login   the same, but don't install the login item
#   macos/install.sh --uninstall  remove the login item and watchdog
#
# Safe to re-run, e.g. after a `git pull` or after changing a TRANSCRIPTION_*
# variable: the login item copies the TRANSCRIPTION_* variables set in the
# shell you run this from.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
AGENTS="$HOME/Library/LaunchAgents"
TRAY_LABEL="local.transcribe-local.tray"
DOG_LABEL="local.transcribe-local.watchdog"
DOMAIN="gui/$(id -u)"

unload() {
    for label in "$TRAY_LABEL" "$DOG_LABEL"; do
        launchctl bootout "$DOMAIN/$label" 2>/dev/null || true
    done
}

if [ "${1:-}" = "--uninstall" ]; then
    unload
    rm -f "$AGENTS/$TRAY_LABEL.plist" "$AGENTS/$DOG_LABEL.plist"
    echo "Login item and watchdog removed. (The repo, .venv and your transcripts are untouched.)"
    exit 0
fi

# 1. Python environment
PY="${PYTHON:-python3}"
if [ ! -x "$REPO/.venv/bin/python" ]; then
    echo "Creating $REPO/.venv with $("$PY" --version)…"
    "$PY" -m venv "$REPO/.venv"
fi
"$REPO/.venv/bin/python" -m pip install --quiet --upgrade pip
"$REPO/.venv/bin/python" -m pip install --quiet -r "$REPO/requirements.txt"

# 2. The audio capture helper
"$REPO/macos/build.sh"

[ "${1:-}" = "--no-login" ] && { echo "Done. Start it with: $REPO/.venv/bin/python $REPO/meeting_tray.py"; exit 0; }

# 3. Login item (launchd agent) + watchdog
xml() { sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g' <<<"$1"; }
envxml="    <key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>"
while IFS='=' read -r name value; do
    envxml+=$'\n'"    <key>$name</key><string>$(xml "$value")</string>"
done < <(env | grep -E '^TRANSCRIPTION_[A-Z_]+=' || true)

mkdir -p "$AGENTS"
cat > "$AGENTS/$TRAY_LABEL.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$TRAY_LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$(xml "$REPO/.venv/bin/python")</string>
    <string>$(xml "$REPO/meeting_tray_mac.py")</string>
  </array>
  <key>WorkingDirectory</key><string>$(xml "$REPO")</string>
  <key>EnvironmentVariables</key>
  <dict>
$envxml
  </dict>
  <key>RunAtLoad</key><true/>
  <!-- relaunch after a crash (or a watchdog kill), but not after Quit -->
  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>ProcessType</key><string>Interactive</string>
  <key>StandardErrorPath</key><string>$(xml "$REPO/tray.log")</string>
</dict>
</plist>
EOF

cat > "$AGENTS/$DOG_LABEL.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$DOG_LABEL</string>
  <key>ProgramArguments</key>
  <array><string>/bin/bash</string><string>$(xml "$REPO/macos/watchdog.sh")</string></array>
  <key>StartInterval</key><integer>300</integer>
  <key>ProcessType</key><string>Background</string>
</dict>
</plist>
EOF

unload
launchctl bootstrap "$DOMAIN" "$AGENTS/$TRAY_LABEL.plist"
launchctl bootstrap "$DOMAIN" "$AGENTS/$DOG_LABEL.plist"
echo "Installed. The menu-bar icon starts now and at every login; the watchdog checks it every 5 minutes."
echo "Undo with: $REPO/macos/install.sh --uninstall"
